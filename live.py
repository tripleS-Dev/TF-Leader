from __future__ import annotations

import argparse
import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from tf_leader import (
    DataIntegrityError,
    PlayerHistoryPoint,
    PlayerRecord,
    TFLeaderboard,
)
from tf_leader.charts import ChartKind, render_history_png


PROJECT_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("tf_leader.live")


@dataclass(frozen=True, slots=True)
class LiveSettings:
    db_path: Path = PROJECT_ROOT / "data" / "leaderboard.sqlite3"
    season: str = "s11"
    port: int = 3000
    refresh_seconds: float = 20 * 60
    retry_seconds: float = 2 * 60
    log_path: Path = PROJECT_ROOT / "logs" / "live.log"
    scheduler_enabled: bool = True

    def __post_init__(self) -> None:
        season = self.season.strip().lower()
        if not season.startswith("s") or not season[1:].isdigit():
            raise ValueError("season은 's11' 같은 형식이어야 합니다.")
        if not 1 <= self.port <= 65_535:
            raise ValueError("port는 1 이상 65535 이하여야 합니다.")
        if self.refresh_seconds <= 0 or self.retry_seconds <= 0:
            raise ValueError("갱신 및 재시도 주기는 0보다 커야 합니다.")
        object.__setattr__(self, "season", season)
        object.__setattr__(self, "db_path", self.db_path.expanduser().resolve())
        object.__setattr__(self, "log_path", self.log_path.expanduser().resolve())


@dataclass(slots=True)
class RuntimeState:
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_refresh_at: datetime | None = None
    last_error: str | None = None


class LiveRuntime:
    def __init__(self, settings: LiveSettings, leaderboard: TFLeaderboard) -> None:
        self.settings = settings
        self.leaderboard = leaderboard
        self.state = RuntimeState()
        self.refresh_lock = asyncio.Lock()
        self.graph_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event.clear()
        self.state.next_refresh_at = _utcnow()
        self.task = asyncio.create_task(self._run(), name="leaderboard-refresh")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await self.task
        self.task = None

    async def refresh_once(self) -> bool:
        if self.refresh_lock.locked():
            LOGGER.warning("이미 리더보드 갱신이 실행 중이므로 중복 요청을 건너뜁니다.")
            return False

        async with self.refresh_lock:
            self.state.last_attempt_at = _utcnow()
            try:
                result = await asyncio.to_thread(
                    self.leaderboard.sync, self.settings.season
                )
            except Exception as exc:
                self.state.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.error("리더보드 갱신 실패", exc_info=True)
                return False

            self.state.last_success_at = _utcnow()
            self.state.last_error = None
            LOGGER.info(
                "리더보드 갱신 성공: snapshot=%s entries=%s changed=%s "
                "removed=%s rank_change_events=%s order_events=%s "
                "order_corrections=%s created=%s updated=%s",
                result.snapshot_id,
                result.entries_saved,
                result.changed_entries,
                result.removed_entries,
                result.rank_change_events,
                result.order_events,
                result.order_corrections,
                result.created,
                result.source_updated_at.isoformat(),
            )
            return True

    async def _run(self) -> None:
        while not self.stop_event.is_set():
            succeeded = await self.refresh_once()
            delay = (
                self.settings.refresh_seconds
                if succeeded
                else self.settings.retry_seconds
            )
            self.state.next_refresh_at = _utcnow() + timedelta(seconds=delay)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue


def create_app(
    settings: LiveSettings | None = None,
    leaderboard: TFLeaderboard | None = None,
) -> FastAPI:
    settings = settings or LiveSettings()
    leaderboard = leaderboard or TFLeaderboard(settings.db_path)
    runtime = LiveRuntime(settings, leaderboard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        if settings.scheduler_enabled:
            await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="TF-Leader Live API",
        version="0.4.0",
        description="THE FINALS 리더보드 수집·검색·이력·그래프 localhost API",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.exception_handler(DataIntegrityError)
    async def data_integrity_error_handler(
        request: Request, exc: DataIntegrityError
    ) -> JSONResponse:
        LOGGER.error(
            "데이터 무결성 검증 실패: %s %s: %s", request.method, request.url.path, exc
        )
        return JSONResponse(
            {"detail": "리더보드 무결성 검증에 실패했습니다.", "error": str(exc)},
            status_code=503,
        )

    app.add_middleware(GZipMiddleware, minimum_size=1_000)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            LOGGER.exception("API 요청 실패: %s %s", request.method, request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1_000
        LOGGER.info(
            "%s %s -> %s %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.get("/leaderboard", summary="Clubweb 호환 최신 리더보드")
    async def get_leaderboard(response: Response) -> dict[str, Any]:
        records = await asyncio.to_thread(
            leaderboard.latest_leaderboard,
            settings.season,
            limit=10_000,
            offset=0,
        )
        if not records:
            raise HTTPException(503, "리더보드 데이터가 아직 준비되지 않았습니다.")

        _set_cache_headers(response, runtime)
        updated_at = records[0].source_updated_at
        last_check = runtime.state.last_attempt_at or updated_at
        source = (
            "local-stale-fallback"
            if runtime.state.last_error is not None
            else "local-live"
        )
        return {
            "data": [_clubweb_player(record) for record in records],
            "timestamp": int(updated_at.timestamp()),
            "lastCheck": int(last_check.timestamp()),
            "source": source,
        }

    @app.get("/api/users/search", summary="최신 스냅샷 유저 검색")
    async def search_users(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        season: Annotated[str, Query(pattern=r"^s\d+$")] = settings.season,
        exact: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        metadata = await asyncio.to_thread(leaderboard.latest_snapshot, season)
        if metadata is None:
            raise HTTPException(503, "리더보드 데이터가 아직 준비되지 않았습니다.")
        records = await asyncio.to_thread(
            leaderboard.search_user,
            q,
            season=season,
            exact=exact,
            limit=limit,
        )
        return {
            "season": season,
            "snapshotId": metadata.snapshot_id,
            "updatedAt": metadata.source_updated_at.isoformat(),
            "count": len(records),
            "data": [_api_player(record) for record in records],
        }

    @app.get("/api/users/history", summary="유저 점수·순위 세션 이력")
    async def user_history(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        season: Annotated[str, Query(pattern=r"^s\d+$")] = settings.season,
        session: Annotated[int, Query(ge=1)] = 1,
    ) -> dict[str, Any]:
        metadata = await asyncio.to_thread(leaderboard.latest_snapshot, season)
        if metadata is None:
            raise HTTPException(503, "리더보드 데이터가 아직 준비되지 않았습니다.")
        result = await asyncio.to_thread(
            leaderboard.user_history_session,
            q,
            season=season,
            session=session,
        )
        return {
            "query": q,
            "season": season,
            "session": result.session,
            "totalSessions": result.total_sessions,
            "count": len(result.points),
            "data": [_history_point(point) for point in result.points],
        }

    async def graph_response(q: str, season: str, kind: ChartKind) -> Response:
        metadata = await asyncio.to_thread(leaderboard.latest_snapshot, season)
        if metadata is None:
            raise HTTPException(503, "리더보드 데이터가 아직 준비되지 않았습니다.")
        history = await asyncio.to_thread(leaderboard.user_history, q, season=season)
        if not history:
            raise HTTPException(404, "해당 유저의 이력을 찾지 못했습니다.")
        async with runtime.graph_lock:
            image = await asyncio.to_thread(render_history_png, history, kind=kind)
        return Response(
            content=image,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/graphs/score.png", summary="유저 점수 이력 PNG")
    async def score_graph(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        season: Annotated[str, Query(pattern=r"^s\d+$")] = settings.season,
    ) -> Response:
        return await graph_response(q, season, "score")

    @app.get("/api/graphs/rank.png", summary="유저 순위 이력 PNG")
    async def rank_graph(
        q: Annotated[str, Query(min_length=1, max_length=100)],
        season: Annotated[str, Query(pattern=r"^s\d+$")] = settings.season,
    ) -> Response:
        return await graph_response(q, season, "rank")

    @app.get("/health", summary="수집기 및 데이터 상태")
    async def health() -> JSONResponse:
        metadata, snapshot_count = await asyncio.gather(
            asyncio.to_thread(leaderboard.latest_snapshot, settings.season),
            asyncio.to_thread(leaderboard.store.snapshot_count, settings.season),
        )
        state = runtime.state
        if metadata is None:
            status = "unavailable" if state.last_error else "starting"
        elif not metadata.integrity_verified:
            status = "degraded"
        elif state.last_attempt_at is None:
            status = "starting"
        elif state.last_error:
            status = "degraded"
        else:
            status = "healthy"

        payload = {
            "status": status,
            "season": settings.season,
            "serverTime": _utcnow().isoformat(),
            "snapshotCount": snapshot_count,
            "entryCount": metadata.entry_count if metadata else 0,
            "contentHash": metadata.content_hash if metadata else None,
            "changedEntryCount": metadata.changed_entry_count if metadata else 0,
            "removedEntryCount": metadata.removed_entry_count if metadata else 0,
            "rankChangeEventCount": (
                metadata.rank_change_event_count if metadata else 0
            ),
            "orderEventCount": metadata.order_event_count if metadata else 0,
            "orderCorrectionCount": (
                metadata.order_correction_count if metadata else 0
            ),
            "integrityVerified": metadata.integrity_verified if metadata else False,
            "latestDataAt": _iso(metadata.source_updated_at if metadata else None),
            "lastAttemptAt": _iso(state.last_attempt_at),
            "lastSuccessAt": _iso(state.last_success_at),
            "nextRefreshAt": _iso(state.next_refresh_at),
            "lastError": state.last_error,
        }
        return JSONResponse(payload, status_code=503 if metadata is None else 200)

    return app


def _clubweb_player(record: PlayerRecord) -> dict[str, Any]:
    player = record.player
    return {
        "rank": player.rank,
        "change": player.rank_change_24h,
        "name": player.display_name,
        "steamName": player.steam_name or None,
        "psnName": player.psn_name or None,
        "xboxName": player.xbox_name or None,
        "clubTag": player.club_tag or None,
        "clubId": player.club_id or None,
        "leagueNumber": player.league,
        "rankScore": player.score,
    }


def _api_player(record: PlayerRecord) -> dict[str, Any]:
    payload = _clubweb_player(record)
    payload.update(
        {
            "snapshotId": record.snapshot_id,
            "season": record.season,
            "updatedAt": record.source_updated_at.isoformat(),
            "league": record.player.league_name,
        }
    )
    return payload


def _history_point(point: PlayerHistoryPoint) -> dict[str, Any]:
    return {
        "snapshotId": point.snapshot_id,
        "season": point.season,
        "updatedAt": point.source_updated_at.isoformat(),
        "rank": point.rank,
        "change": point.rank_change_24h,
        "score": point.score,
        "leagueNumber": point.league,
        "league": point.league_name,
        "name": point.display_name,
    }


def _set_cache_headers(response: Response, runtime: LiveRuntime) -> None:
    now = _utcnow()
    expires_at = runtime.state.next_refresh_at or (
        now + timedelta(seconds=runtime.settings.refresh_seconds)
    )
    max_age = max(1, int((expires_at - now).total_seconds()))
    response.headers["Cache-Control"] = f"public, max-age={max_age}"
    response.headers["Expires"] = format_datetime(expires_at, usegmt=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    rotating = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[console, rotating],
        force=True,
    )


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 값이어야 합니다.")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TF-Leader 24/7 localhost 서버")
    parser.add_argument("--db", type=Path, default=LiveSettings().db_path)
    parser.add_argument("--season", default="s11")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--refresh-seconds", type=_positive_float, default=1_200)
    parser.add_argument("--retry-seconds", type=_positive_float, default=120)
    parser.add_argument("--log-file", type=Path, default=LiveSettings().log_path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = LiveSettings(
        db_path=args.db,
        season=args.season,
        port=args.port,
        refresh_seconds=args.refresh_seconds,
        retry_seconds=args.retry_seconds,
        log_path=args.log_file,
    )
    configure_logging(settings.log_path)
    LOGGER.info(
        "TF-Leader Live 시작: http://127.0.0.1:%s season=%s db=%s",
        settings.port,
        settings.season,
        settings.db_path,
    )
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()

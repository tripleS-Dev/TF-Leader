from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from live import LiveRuntime, LiveSettings, create_app
from tf_leader import LeaderboardSnapshot, PlayerEntry, SyncResult, TFLeaderboard


class SequenceLeaderboard:
    def __init__(self) -> None:
        self.calls = 0

    def sync(self, season: str) -> SyncResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary failure")
        now = datetime.now(timezone.utc)
        return SyncResult(
            snapshot_id=self.calls,
            season=season,
            source_updated_at=now,
            entries_saved=1,
            created=True,
        )


def _snapshot(updated_at: datetime, *, rank: int, score: int) -> LeaderboardSnapshot:
    return LeaderboardSnapshot(
        season="s11",
        name="Season 11",
        source_url="https://example.test/leaderboard",
        source_updated_at=updated_at,
        fetched_at=updated_at,
        entries=(
            PlayerEntry(
                rank=rank,
                rank_change_24h=1,
                display_name="Player#1234",
                league=20,
                score=score,
                steam_name="SteamPlayer",
                psn_name="ConsolePlayer",
                club_tag="TST",
                club_id="club-id",
            ),
        ),
    )


def _service_with_history(tmp_path) -> tuple[TFLeaderboard, datetime]:
    service = TFLeaderboard(tmp_path / "leaderboard.sqlite3")
    first = datetime(2026, 7, 17, 0, tzinfo=timezone.utc)
    second = first + timedelta(minutes=20)
    service.store.save_snapshot(_snapshot(first, rank=1, score=50_000))
    service.store.save_snapshot(_snapshot(second, rank=1, score=52_000))
    return service, second


def test_runtime_retries_then_uses_success_interval(tmp_path) -> None:
    async def scenario() -> tuple[int, LiveRuntime]:
        settings = LiveSettings(
            db_path=tmp_path / "scheduler.sqlite3",
            log_path=tmp_path / "live.log",
            refresh_seconds=0.02,
            retry_seconds=0.01,
        )
        service = SequenceLeaderboard()
        runtime = LiveRuntime(settings, service)  # type: ignore[arg-type]
        await runtime.start()
        deadline = asyncio.get_running_loop().time() + 1
        while service.calls < 3 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
        await runtime.stop()
        return service.calls, runtime

    calls, runtime = asyncio.run(scenario())

    assert calls >= 3
    assert runtime.state.last_attempt_at is not None
    assert runtime.state.last_success_at is not None
    assert runtime.state.next_refresh_at is not None
    assert runtime.state.last_error is None


def test_live_api_is_clubweb_compatible_and_serves_extended_routes(tmp_path) -> None:
    service, updated_at = _service_with_history(tmp_path)
    settings = LiveSettings(
        db_path=service.store.db_path,
        log_path=tmp_path / "live.log",
        scheduler_enabled=False,
    )
    app = create_app(settings, service)

    with TestClient(app) as client:
        leaderboard = client.get("/leaderboard")
        assert leaderboard.status_code == 200
        payload = leaderboard.json()
        assert payload["timestamp"] == int(updated_at.timestamp())
        assert payload["lastCheck"] == int(updated_at.timestamp())
        assert payload["source"] == "local-live"
        assert payload["data"] == [
            {
                "rank": 1,
                "change": 1,
                "name": "Player#1234",
                "steamName": "SteamPlayer",
                "psnName": "ConsolePlayer",
                "xboxName": None,
                "clubTag": "TST",
                "clubId": "club-id",
                "leagueNumber": 20,
                "rankScore": 52_000,
            }
        ]
        assert "max-age=" in leaderboard.headers["cache-control"]
        assert "expires" in leaderboard.headers

        search = client.get("/api/users/search", params={"q": "SteamPlayer"})
        assert search.status_code == 200
        assert search.json()["data"][0]["name"] == "Player#1234"

        history = client.get("/api/users/history", params={"q": "Player#1234"})
        assert history.status_code == 200
        history_payload = history.json()
        assert history_payload["session"] == 1
        assert history_payload["totalSessions"] == 1
        assert history_payload["count"] == 2
        assert [point["rank"] for point in history_payload["data"]] == [1, 1]

        previous = client.get(
            "/api/users/history",
            params={"q": "Player#1234", "session": 2},
        )
        assert previous.status_code == 200
        assert previous.json()["totalSessions"] == 1
        assert previous.json()["data"] == []
        assert (
            client.get(
                "/api/users/history",
                params={"q": "Player#1234", "session": 0},
            ).status_code
            == 422
        )


        score_graph = client.get("/api/graphs/score.png", params={"q": "Player#1234"})
        assert score_graph.status_code == 200
        assert score_graph.headers["content-type"] == "image/png"
        assert score_graph.content.startswith(b"\x89PNG\r\n\x1a\n")

        rank_graph = client.get("/api/graphs/rank.png", params={"q": "Player#1234"})
        assert rank_graph.status_code == 200
        assert rank_graph.content.startswith(b"\x89PNG\r\n\x1a\n")

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "starting"
        assert health.json()["snapshotCount"] == 2
        assert health.json()["integrityVerified"] is True

        cors = client.options(
            "/leaderboard",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert cors.status_code == 200
        assert cors.headers["access-control-allow-origin"] == "http://localhost:5173"

        runtime = app.state.runtime
        runtime.state.last_attempt_at = datetime.now(timezone.utc)
        runtime.state.last_error = "RuntimeError: Embark unavailable"
        assert client.get("/leaderboard").json()["source"] == "local-stale-fallback"
        degraded = client.get("/health")
        assert degraded.status_code == 200
        assert degraded.json()["status"] == "degraded"


def test_live_api_returns_503_without_snapshot(tmp_path) -> None:
    service = TFLeaderboard(tmp_path / "empty.sqlite3")
    settings = LiveSettings(
        db_path=service.store.db_path,
        log_path=tmp_path / "live.log",
        scheduler_enabled=False,
    )
    app = create_app(settings, service)

    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 503
        assert health.json()["status"] == "starting"
        assert client.get("/leaderboard").status_code == 503
        assert (
            client.get("/api/users/search", params={"q": "Player"}).status_code == 503
        )

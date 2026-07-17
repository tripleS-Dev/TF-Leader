from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .service import TFLeaderboard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="THE FINALS 리더보드 백엔드")
    parser.add_argument(
        "--db",
        default="data/leaderboard.sqlite3",
        help="SQLite 파일 경로 (기본값: data/leaderboard.sqlite3)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Embark에서 10,000명 데이터 동기화")
    sync.add_argument("--season", default="s11")

    search = subparsers.add_parser("search", help="최신 데이터에서 유저 검색")
    search.add_argument("query")
    search.add_argument("--season", default="s11")
    search.add_argument("--exact", action="store_true")
    search.add_argument("--limit", type=int, default=20)

    history = subparsers.add_parser("history", help="유저 점수/순위 이력 조회")
    history.add_argument("query")
    history.add_argument("--season", default="s11")

    graph = subparsers.add_parser("graph", help="유저 이력 PNG 그래프 생성")
    graph.add_argument("query")
    graph.add_argument("--season", default="s11")
    graph.add_argument("--kind", choices=("score", "rank"), required=True)
    graph.add_argument("--output", type=Path)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = TFLeaderboard(args.db)

    if args.command == "sync":
        result = app.sync(args.season)
        _print_json(asdict(result))
        return 0

    if args.command == "search":
        records = app.search_user(
            args.query,
            season=args.season,
            exact=args.exact,
            limit=args.limit,
        )
        payload = []
        for record in records:
            item = asdict(record)
            item["player"]["league_name"] = record.player.league_name
            payload.append(item)
        _print_json(payload)
        return 0

    if args.command == "history":
        points = app.user_history(args.query, season=args.season)
        payload = []
        for point in points:
            item = asdict(point)
            item["league_name"] = point.league_name
            payload.append(item)
        _print_json(payload)
        return 0

    output = args.output or Path(f"outputs/{args.kind}_history.png")
    if args.kind == "score":
        path = app.score_graph(args.query, season=args.season, output=output)
    else:
        path = app.rank_graph(args.query, season=args.season, output=output)
    print(path)
    return 0


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=_json_default))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())

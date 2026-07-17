from __future__ import annotations

from pathlib import Path

from .charts import plot_history
from .client import LeaderboardClient
from .models import PlayerHistoryPoint, PlayerRecord, SnapshotMetadata, SyncResult
from .repository import LeaderboardStore


class TFLeaderboard:
    """High-level facade for sync, user search, history, and chart generation."""

    def __init__(
        self,
        db_path: str | Path = "data/leaderboard.sqlite3",
        *,
        client: LeaderboardClient | None = None,
        expected_entry_count: int | None = 10_000,
    ) -> None:
        self.client = client or LeaderboardClient()
        self.store = LeaderboardStore(db_path)
        self.expected_entry_count = expected_entry_count

    def sync(self, season: str = "s11") -> SyncResult:
        snapshot = self.client.fetch(season)
        snapshot_id, created = self.store.save_snapshot(
            snapshot,
            expected_entry_count=self.expected_entry_count,
        )
        metadata = self.store.latest_snapshot(snapshot.season)
        if metadata is None:
            raise RuntimeError("저장 직후 스냅샷 메타데이터를 찾지 못했습니다.")
        return SyncResult(
            snapshot_id=snapshot_id,
            season=snapshot.season,
            source_updated_at=snapshot.source_updated_at,
            entries_saved=len(snapshot.entries) if created else 0,
            created=created,
            changed_entries=metadata.changed_entry_count if created else 0,
            removed_entries=metadata.removed_entry_count if created else 0,
            rank_change_events=metadata.rank_change_event_count if created else 0,
            order_events=metadata.order_event_count if created else 0,
            order_corrections=metadata.order_correction_count if created else 0,
        )

    def search_user(
        self,
        query: str,
        *,
        season: str = "s11",
        exact: bool = False,
        limit: int = 20,
    ) -> list[PlayerRecord]:
        return self.store.search(query, season=season, exact=exact, limit=limit)

    def latest_snapshot(self, season: str = "s11") -> SnapshotMetadata | None:
        return self.store.latest_snapshot(season)

    def latest_leaderboard(
        self,
        season: str = "s11",
        *,
        limit: int = 10_000,
        offset: int = 0,
    ) -> list[PlayerRecord]:
        return self.store.latest_entries(season, limit=limit, offset=offset)

    def user_history(
        self,
        query: str,
        *,
        season: str = "s11",
    ) -> list[PlayerHistoryPoint]:
        return self.store.history(query, season=season)

    def score_graph(
        self,
        query: str,
        *,
        season: str = "s11",
        output: str | Path = "outputs/score_history.png",
    ) -> Path:
        history = self.user_history(query, season=season)
        return plot_history(history, kind="score", output=output)

    def rank_graph(
        self,
        query: str,
        *,
        season: str = "s11",
        output: str | Path = "outputs/rank_history.png",
    ) -> Path:
        history = self.user_history(query, season=season)
        return plot_history(history, kind="rank", output=output)

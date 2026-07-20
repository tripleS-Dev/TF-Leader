from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_left
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .models import (
    LeaderboardSnapshot,
    PlayerEntry,
    PlayerHistoryPoint,
    PlayerHistorySession,
    PlayerRecord,
    SnapshotMetadata,
)


SCHEMA_VERSION = 4
HISTORY_CHECKPOINT_INTERVAL = 12
SESSION_INACTIVITY_FETCHES = 5


@dataclass(frozen=True, slots=True)
class _PlayerState:
    display_name: str
    league: int
    score: int
    steam_name: str = ""
    psn_name: str = ""
    xbox_name: str = ""
    club_tag: str = ""
    club_id: str = ""


@dataclass(frozen=True, slots=True)
class _HistorySessionRange:
    player_key: str
    start_snapshot_id: int
    end_snapshot_id: int


class DataIntegrityError(RuntimeError):
    """Raised when a snapshot cannot be stored without risking data loss."""


class LeaderboardStore:
    """SQLite storage using verified deltas plus a materialized latest state."""

    def __init__(self, db_path: str | Path = "data/leaderboard.sqlite3") -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._needs_compaction = False
        self._create_migration_backup_if_needed()
        self.initialize()
        if self._needs_compaction:
            self._compact_database()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _create_migration_backup_if_needed(self) -> None:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return

        with sqlite3.connect(self.db_path, timeout=5.0) as source:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            has_snapshots = source.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'snapshots'
                """
            ).fetchone()
            if version >= 3 or has_snapshots is None:
                return
            snapshot_count = int(
                source.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )
            if snapshot_count == 0:
                return

            label = "pre-reconstruction-v2" if version >= 2 else "pre-delta-v1"
            backup_path = self.db_path.with_name(f"{self.db_path.name}-{label}.bak")
            if backup_path.exists():
                with sqlite3.connect(backup_path) as backup:
                    result = backup.execute("PRAGMA integrity_check").fetchone()[0]
                    if result != "ok":
                        raise DataIntegrityError(
                            f"기존 마이그레이션 백업 무결성 검사 실패: {result}"
                        )
                return

            try:
                with sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
                    result = backup.execute("PRAGMA integrity_check").fetchone()[0]
                    if result != "ok":
                        raise DataIntegrityError(
                            f"마이그레이션 백업 무결성 검사 실패: {result}"
                        )
            except Exception:
                backup_path.unlink(missing_ok=True)
                raise

    def _compact_database(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30.0) as connection:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise DataIntegrityError(f"v3 압축 후 무결성 검사 실패: {result}")

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    season TEXT NOT NULL,
                    leaderboard_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_updated_at_ms INTEGER NOT NULL,
                    fetched_at_ms INTEGER NOT NULL,
                    entry_count INTEGER NOT NULL,
                    UNIQUE (season, source_updated_at_ms)
                );

                -- v1 full snapshots are retained as a read-only migration backup.
                CREATE TABLE IF NOT EXISTS entries (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    rank_change_24h INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    league INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    steam_name TEXT NOT NULL DEFAULT '',
                    psn_name TEXT NOT NULL DEFAULT '',
                    xbox_name TEXT NOT NULL DEFAULT '',
                    club_tag TEXT NOT NULL DEFAULT '',
                    club_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (snapshot_id, rank)
                );

                CREATE TABLE IF NOT EXISTS entry_changes (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    season TEXT NOT NULL,
                    player_key TEXT NOT NULL,
                    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
                    rank INTEGER NOT NULL,
                    rank_change_24h INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    league INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    steam_name TEXT NOT NULL DEFAULT '',
                    psn_name TEXT NOT NULL DEFAULT '',
                    xbox_name TEXT NOT NULL DEFAULT '',
                    club_tag TEXT NOT NULL DEFAULT '',
                    club_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (snapshot_id, player_key)
                );

                CREATE TABLE IF NOT EXISTS current_entries (
                    season TEXT NOT NULL,
                    player_key TEXT NOT NULL,
                    last_changed_snapshot_id INTEGER NOT NULL
                        REFERENCES snapshots(id) ON DELETE CASCADE,
                    rank INTEGER NOT NULL,
                    rank_change_24h INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    league INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    steam_name TEXT NOT NULL DEFAULT '',
                    psn_name TEXT NOT NULL DEFAULT '',
                    xbox_name TEXT NOT NULL DEFAULT '',
                    club_tag TEXT NOT NULL DEFAULT '',
                    club_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (season, player_key)
                );

                CREATE TABLE IF NOT EXISTS snapshot_integrity (
                    snapshot_id INTEGER PRIMARY KEY
                        REFERENCES snapshots(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    changed_entry_count INTEGER NOT NULL,
                    removed_entry_count INTEGER NOT NULL,
                    reconstructed_entry_count INTEGER NOT NULL,
                    verified_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS player_state_events (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    season TEXT NOT NULL,
                    player_key TEXT NOT NULL,
                    is_present INTEGER NOT NULL CHECK (is_present IN (0, 1)),
                    display_name TEXT NOT NULL,
                    league INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    steam_name TEXT NOT NULL DEFAULT '',
                    psn_name TEXT NOT NULL DEFAULT '',
                    xbox_name TEXT NOT NULL DEFAULT '',
                    club_tag TEXT NOT NULL DEFAULT '',
                    club_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (snapshot_id, player_key)
                );

                CREATE TABLE IF NOT EXISTS rank_change_events (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    season TEXT NOT NULL,
                    player_key TEXT NOT NULL,
                    rank_change_24h INTEGER NOT NULL,
                    PRIMARY KEY (snapshot_id, player_key)
                );

                CREATE TABLE IF NOT EXISTS order_events (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    season TEXT NOT NULL,
                    player_key TEXT NOT NULL,
                    target_rank INTEGER NOT NULL,
                    is_correction INTEGER NOT NULL CHECK (is_correction IN (0, 1)),
                    PRIMARY KEY (snapshot_id, player_key),
                    UNIQUE (snapshot_id, target_rank)
                );

                CREATE TABLE IF NOT EXISTS snapshot_reconstruction (
                    snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    state_event_count INTEGER NOT NULL,
                    removed_entry_count INTEGER NOT NULL,
                    rank_change_event_count INTEGER NOT NULL,
                    order_event_count INTEGER NOT NULL,
                    order_correction_count INTEGER NOT NULL,
                    reconstructed_entry_count INTEGER NOT NULL,
                    verified_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_checkpoints (
                    snapshot_id INTEGER PRIMARY KEY REFERENCES snapshots(id) ON DELETE CASCADE,
                    season TEXT NOT NULL,
                    entry_count INTEGER NOT NULL,
                    content_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_checkpoint_entries (
                    snapshot_id INTEGER NOT NULL
                        REFERENCES history_checkpoints(snapshot_id) ON DELETE CASCADE,
                    player_key TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    rank_change_24h INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    league INTEGER NOT NULL,
                    score INTEGER NOT NULL,
                    steam_name TEXT NOT NULL DEFAULT '',
                    psn_name TEXT NOT NULL DEFAULT '',
                    xbox_name TEXT NOT NULL DEFAULT '',
                    club_tag TEXT NOT NULL DEFAULT '',
                    club_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (snapshot_id, player_key),
                    UNIQUE (snapshot_id, rank)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_latest
                    ON snapshots (season, source_updated_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_entries_display_name
                    ON entries (display_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_entries_steam_name
                    ON entries (steam_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_entries_psn_name
                    ON entries (psn_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_entries_xbox_name
                    ON entries (xbox_name COLLATE NOCASE);

                CREATE INDEX IF NOT EXISTS idx_changes_player_history
                    ON entry_changes (season, player_key, snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_changes_display_name
                    ON entry_changes (season, display_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_changes_steam_name
                    ON entry_changes (season, steam_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_changes_psn_name
                    ON entry_changes (season, psn_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_changes_xbox_name
                    ON entry_changes (season, xbox_name COLLATE NOCASE);

                CREATE INDEX IF NOT EXISTS idx_current_rank
                    ON current_entries (season, rank);
                CREATE INDEX IF NOT EXISTS idx_current_display_name
                    ON current_entries (season, display_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_current_steam_name
                    ON current_entries (season, steam_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_current_psn_name
                    ON current_entries (season, psn_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_current_xbox_name
                    ON current_entries (season, xbox_name COLLATE NOCASE);

                CREATE INDEX IF NOT EXISTS idx_state_events_player_history
                    ON player_state_events (season, player_key, snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_state_events_display_name
                    ON player_state_events (season, display_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_state_events_steam_name
                    ON player_state_events (season, steam_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_state_events_psn_name
                    ON player_state_events (season, psn_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_state_events_xbox_name
                    ON player_state_events (season, xbox_name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_rank_change_player_history
                    ON rank_change_events (season, player_key, snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_order_events_snapshot
                    ON order_events (season, snapshot_id, target_rank);
                CREATE INDEX IF NOT EXISTS idx_history_checkpoints_season
                    ON history_checkpoints (season, snapshot_id);
                """
            )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version < 2:
                self._migrate_legacy_snapshots(connection)
            if version < 3:
                self._migrate_to_reconstruction(connection)
            if version < 4:
                self._migrate_to_history_checkpoints(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_legacy_snapshots(self, connection: sqlite3.Connection) -> None:
        snapshots = connection.execute(
            "SELECT id, season, entry_count FROM snapshots ORDER BY id ASC"
        ).fetchall()
        if not snapshots:
            return

        connection.execute("DELETE FROM current_entries")
        connection.execute("DELETE FROM entry_changes")
        connection.execute("DELETE FROM snapshot_integrity")

        previous_by_season: dict[str, dict[str, PlayerEntry]] = {}
        changed_at_by_season: dict[str, dict[str, int]] = {}

        for snapshot_row in snapshots:
            snapshot_id = int(snapshot_row["id"])
            season = str(snapshot_row["season"])
            expected_count = int(snapshot_row["entry_count"])
            legacy_rows = connection.execute(
                """
                SELECT rank, rank_change_24h, display_name, league, score,
                       steam_name, psn_name, xbox_name, club_tag, club_id
                FROM entries
                WHERE snapshot_id = ?
                ORDER BY rank ASC
                """,
                (snapshot_id,),
            ).fetchall()
            if len(legacy_rows) != expected_count:
                raise DataIntegrityError(
                    f"기존 snapshot {snapshot_id} 행 수 불일치: "
                    f"metadata={expected_count}, entries={len(legacy_rows)}"
                )

            entries = tuple(_row_to_player(row) for row in legacy_rows)
            current = _validated_state(entries, expected_count=expected_count)
            previous = previous_by_season.get(season, {})
            changed_at = changed_at_by_season.get(season, {})

            upserts = {
                key: entry
                for key, entry in current.items()
                if previous.get(key) != entry
            }
            removed = {key: previous[key] for key in previous.keys() - current.keys()}
            self._insert_changes(connection, snapshot_id, season, upserts, removed)
            for key in upserts:
                changed_at[key] = snapshot_id
            for key in removed:
                changed_at.pop(key, None)

            content_hash = _state_hash(current.values())
            self._insert_integrity(
                connection,
                snapshot_id=snapshot_id,
                content_hash=content_hash,
                changed_count=len(upserts),
                removed_count=len(removed),
                reconstructed_count=len(current),
            )
            previous_by_season[season] = current
            changed_at_by_season[season] = changed_at

        for season, state in previous_by_season.items():
            self._replace_current_state(
                connection,
                season,
                state,
                changed_at_by_season[season],
            )
            self._verify_current_state(connection, season, state)

    def _migrate_to_reconstruction(self, connection: sqlite3.Connection) -> None:
        """Convert verified v2 deltas into compact, exactly replayable v3 events."""
        snapshots = connection.execute(
            """
            SELECT id, season, entry_count
            FROM snapshots
            ORDER BY id ASC
            """
        ).fetchall()
        if not snapshots:
            return

        connection.execute("DELETE FROM player_state_events")
        connection.execute("DELETE FROM rank_change_events")
        connection.execute("DELETE FROM order_events")
        connection.execute("DELETE FROM snapshot_reconstruction")

        reconstructed_by_season: dict[str, dict[str, PlayerEntry]] = {}
        latest_snapshot_by_season: dict[str, int] = {}

        for snapshot_row in snapshots:
            snapshot_id = int(snapshot_row["id"])
            season = str(snapshot_row["season"])
            expected_count = int(snapshot_row["entry_count"])
            previous = reconstructed_by_season.get(season, {})
            incoming = dict(previous)

            changes = connection.execute(
                """
                SELECT player_key, is_present,
                       rank, rank_change_24h, display_name, league, score,
                       steam_name, psn_name, xbox_name, club_tag, club_id
                FROM entry_changes
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()
            for row in changes:
                key = str(row["player_key"])
                if int(row["is_present"]):
                    incoming[key] = _row_to_player(row)
                else:
                    incoming.pop(key, None)

            ordered = tuple(sorted(incoming.values(), key=lambda entry: entry.rank))
            incoming = _validated_state(ordered, expected_count=expected_count)
            legacy_integrity = connection.execute(
                "SELECT content_hash FROM snapshot_integrity WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if legacy_integrity is not None:
                legacy_hash = str(legacy_integrity["content_hash"])
                if _state_hash(incoming.values()) != legacy_hash:
                    raise DataIntegrityError(
                        f"v2 snapshot {snapshot_id} 복원 해시가 일치하지 않습니다."
                    )

            self._encode_reconstruction_snapshot(
                connection,
                snapshot_id=snapshot_id,
                season=season,
                previous=previous,
                incoming=incoming,
            )
            reconstructed_by_season[season] = incoming
            latest_snapshot_by_season[season] = snapshot_id

        for season, state in reconstructed_by_season.items():
            latest_id = latest_snapshot_by_season[season]
            self._replace_current_state(
                connection,
                season,
                state,
                {key: latest_id for key in state},
            )
            self._verify_current_state(connection, season, state)

        # The verified pre-migration backup remains the lossless source copy.
        # Retiring duplicate v1/v2 rows keeps the operational database compact.
        connection.execute("DELETE FROM entries")
        connection.execute("DELETE FROM entry_changes")
        connection.execute("DELETE FROM snapshot_integrity")
        self._needs_compaction = True

    def _migrate_to_history_checkpoints(
        self, connection: sqlite3.Connection
    ) -> None:
        """Build periodic verified states so history only replays a bounded prefix."""
        connection.execute("DELETE FROM history_checkpoint_entries")
        connection.execute("DELETE FROM history_checkpoints")
        snapshots = connection.execute(
            """
            SELECT s.id, s.season, s.entry_count, r.content_hash
            FROM snapshots s
            LEFT JOIN snapshot_reconstruction r ON r.snapshot_id = s.id
            ORDER BY s.season ASC, s.id ASC
            """
        ).fetchall()
        if not snapshots:
            return

        current_by_season: dict[str, dict[str, PlayerEntry]] = {}
        snapshot_count_by_season: dict[str, int] = {}
        for snapshot in snapshots:
            snapshot_id = int(snapshot["id"])
            season = str(snapshot["season"])
            previous = current_by_season.get(season, {})
            state_rows = connection.execute(
                """
                SELECT player_key, is_present, display_name, league, score,
                       steam_name, psn_name, xbox_name, club_tag, club_id
                FROM player_state_events
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()
            rank_rows = connection.execute(
                """
                SELECT player_key, rank_change_24h
                FROM rank_change_events
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()
            order_rows = connection.execute(
                """
                SELECT player_key, target_rank
                FROM order_events
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchall()

            upserts: dict[str, PlayerEntry] = {}
            removed: list[str] = []
            for row in state_rows:
                key = str(row["player_key"])
                if int(row["is_present"]):
                    upserts[key] = _state_row_to_player(row)
                else:
                    removed.append(key)
            current = _reconstruct_snapshot(
                previous,
                state_upserts=upserts,
                removed=removed,
                rank_change_updates={
                    str(row["player_key"]): int(row["rank_change_24h"])
                    for row in rank_rows
                },
                order_updates={
                    str(row["player_key"]): int(row["target_rank"])
                    for row in order_rows
                },
                expected_count=int(snapshot["entry_count"]),
            )
            content_hash = _state_hash(current.values())
            stored_content_hash = snapshot["content_hash"]
            if stored_content_hash is None or content_hash != str(stored_content_hash):
                raise DataIntegrityError(
                    f"checkpoint migration snapshot {snapshot_id} "
                    "복원 해시가 일치하지 않습니다."
                )

            ordinal = snapshot_count_by_season.get(season, 0) + 1
            snapshot_count_by_season[season] = ordinal
            if (ordinal - 1) % HISTORY_CHECKPOINT_INTERVAL == 0:
                self._insert_history_checkpoint(
                    connection, snapshot_id, season, current
                )
            current_by_season[season] = current

    def _encode_reconstruction_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: int,
        season: str,
        previous: dict[str, PlayerEntry],
        incoming: dict[str, PlayerEntry],
    ) -> None:
        previous_keys = previous.keys()
        incoming_keys = incoming.keys()
        state_upserts = {
            key: entry
            for key, entry in incoming.items()
            if key not in previous
            or _player_state(previous[key]) != _player_state(entry)
        }
        removed = {key: previous[key] for key in previous_keys - incoming_keys}
        rank_change_updates = {
            key: entry.rank_change_24h
            for key, entry in incoming.items()
            if key not in previous
            or previous[key].rank_change_24h != entry.rank_change_24h
        }
        mandatory_movers = {
            key
            for key, entry in incoming.items()
            if key not in previous or previous[key].score != entry.score
        }
        corrections = _order_corrections(previous, incoming, mandatory_movers)
        movers = mandatory_movers | corrections
        order_updates = {key: incoming[key].rank for key in movers}

        self._insert_state_events(
            connection,
            snapshot_id,
            season,
            state_upserts,
            removed,
        )
        if rank_change_updates:
            connection.executemany(
                """
                INSERT INTO rank_change_events (
                    snapshot_id, season, player_key, rank_change_24h
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (snapshot_id, season, key, value)
                    for key, value in rank_change_updates.items()
                ),
            )
        if order_updates:
            connection.executemany(
                """
                INSERT INTO order_events (
                    snapshot_id, season, player_key, target_rank, is_correction
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (
                        snapshot_id,
                        season,
                        key,
                        rank,
                        int(key in corrections),
                    )
                    for key, rank in order_updates.items()
                ),
            )

        reconstructed = _reconstruct_snapshot(
            previous,
            state_upserts=state_upserts,
            removed=removed.keys(),
            rank_change_updates=rank_change_updates,
            order_updates=order_updates,
            expected_count=len(incoming),
        )
        if reconstructed != incoming:
            raise DataIntegrityError(
                f"snapshot {snapshot_id} 순서 복원이 원본과 일치하지 않습니다."
            )
        content_hash = _state_hash(incoming.values())
        if _state_hash(reconstructed.values()) != content_hash:
            raise DataIntegrityError(
                f"snapshot {snapshot_id} 순서 복원 SHA-256 검증에 실패했습니다."
            )
        connection.execute(
            """
            INSERT INTO snapshot_reconstruction (
                snapshot_id, content_hash, state_event_count,
                removed_entry_count, rank_change_event_count,
                order_event_count, order_correction_count,
                reconstructed_entry_count, verified_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                content_hash,
                len(state_upserts),
                len(removed),
                len(rank_change_updates),
                len(order_updates),
                len(corrections),
                len(reconstructed),
                _to_epoch_ms(datetime.now(timezone.utc)),
            ),
        )

    def _insert_state_events(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        season: str,
        upserts: dict[str, PlayerEntry],
        removed: dict[str, PlayerEntry],
    ) -> None:
        rows = [
            _state_event_values(snapshot_id, season, key, True, entry)
            for key, entry in upserts.items()
        ]
        rows.extend(
            _state_event_values(snapshot_id, season, key, False, entry)
            for key, entry in removed.items()
        )
        if rows:
            connection.executemany(
                """
                INSERT INTO player_state_events (
                    snapshot_id, season, player_key, is_present,
                    display_name, league, score, steam_name, psn_name,
                    xbox_name, club_tag, club_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _insert_history_checkpoint(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        season: str,
        state: dict[str, PlayerEntry],
    ) -> None:
        content_hash = _state_hash(state.values())
        connection.execute(
            """
            INSERT INTO history_checkpoints (
                snapshot_id, season, entry_count, content_hash
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, season, len(state), content_hash),
        )
        connection.executemany(
            """
            INSERT INTO history_checkpoint_entries (
                snapshot_id, player_key, rank, rank_change_24h,
                display_name, league, score, steam_name, psn_name,
                xbox_name, club_tag, club_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (snapshot_id, key, *_player_values(entry))
                for key, entry in state.items()
            ),
        )
        saved_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM history_checkpoint_entries
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()[0]
        )
        if saved_count != len(state):
            raise DataIntegrityError(
                f"checkpoint {snapshot_id} 행 수 불일치: "
                f"expected={len(state)}, actual={saved_count}"
            )

    def save_snapshot(
        self,
        snapshot: LeaderboardSnapshot,
        *,
        expected_entry_count: int | None = None,
    ) -> tuple[int, bool]:
        incoming = _validated_state(
            snapshot.entries,
            expected_count=expected_entry_count,
        )
        incoming_hash = _state_hash(incoming.values())
        updated_ms = _to_epoch_ms(snapshot.source_updated_at)
        fetched_ms = _to_epoch_ms(snapshot.fetched_at)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT s.id, r.content_hash
                FROM snapshots s
                LEFT JOIN snapshot_reconstruction r ON r.snapshot_id = s.id
                WHERE s.season = ? AND s.source_updated_at_ms = ?
                """,
                (snapshot.season, updated_ms),
            ).fetchone()
            if existing is not None:
                stored_hash = str(existing["content_hash"] or "")
                if stored_hash != incoming_hash:
                    raise DataIntegrityError(
                        "동일한 원본 갱신 시각에 서로 다른 내용이 감지되었습니다. "
                        "기존 스냅샷을 유지합니다."
                    )
                return int(existing["id"]), False

            latest = connection.execute(
                """
                SELECT id, source_updated_at_ms
                FROM snapshots
                WHERE season = ?
                ORDER BY source_updated_at_ms DESC
                LIMIT 1
                """,
                (snapshot.season,),
            ).fetchone()
            if latest is not None and updated_ms < int(latest["source_updated_at_ms"]):
                raise DataIntegrityError(
                    "최신 데이터보다 오래된 스냅샷은 현재 상태를 훼손할 수 있어 "
                    f"저장하지 않습니다: incoming={updated_ms}, "
                    f"latest={int(latest['source_updated_at_ms'])}"
                )

            previous, _ = self._load_current_state(connection, snapshot.season)
            current_updates = {
                key: entry
                for key, entry in incoming.items()
                if previous.get(key) != entry
            }

            cursor = connection.execute(
                """
                INSERT INTO snapshots (
                    season, leaderboard_name, source_url,
                    source_updated_at_ms, fetched_at_ms, entry_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.season,
                    snapshot.name,
                    snapshot.source_url,
                    updated_ms,
                    fetched_ms,
                    len(incoming),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            self._encode_reconstruction_snapshot(
                connection,
                snapshot_id=snapshot_id,
                season=snapshot.season,
                previous=previous,
                incoming=incoming,
            )

            season_snapshot_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM snapshots WHERE season = ?",
                    (snapshot.season,),
                ).fetchone()[0]
            )
            if (season_snapshot_count - 1) % HISTORY_CHECKPOINT_INTERVAL == 0:
                self._insert_history_checkpoint(
                    connection, snapshot_id, snapshot.season, incoming
                )

            removed_keys = previous.keys() - incoming.keys()
            if removed_keys:
                connection.executemany(
                    "DELETE FROM current_entries WHERE season = ? AND player_key = ?",
                    ((snapshot.season, key) for key in removed_keys),
                )
            if current_updates:
                connection.executemany(
                    """
                    INSERT INTO current_entries (
                        season, player_key, last_changed_snapshot_id,
                        rank, rank_change_24h, display_name, league, score,
                        steam_name, psn_name, xbox_name, club_tag, club_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(season, player_key) DO UPDATE SET
                        last_changed_snapshot_id = excluded.last_changed_snapshot_id,
                        rank = excluded.rank,
                        rank_change_24h = excluded.rank_change_24h,
                        display_name = excluded.display_name,
                        league = excluded.league,
                        score = excluded.score,
                        steam_name = excluded.steam_name,
                        psn_name = excluded.psn_name,
                        xbox_name = excluded.xbox_name,
                        club_tag = excluded.club_tag,
                        club_id = excluded.club_id
                    """,
                    (
                        _current_values(snapshot.season, key, snapshot_id, entry)
                        for key, entry in current_updates.items()
                    ),
                )

            self._verify_current_state(connection, snapshot.season, incoming)
            return snapshot_id, True

    def _insert_changes(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        season: str,
        upserts: dict[str, PlayerEntry],
        removed: dict[str, PlayerEntry],
    ) -> None:
        rows = [
            _change_values(snapshot_id, season, key, True, entry)
            for key, entry in upserts.items()
        ]
        rows.extend(
            _change_values(snapshot_id, season, key, False, entry)
            for key, entry in removed.items()
        )
        if rows:
            connection.executemany(
                """
                INSERT INTO entry_changes (
                    snapshot_id, season, player_key, is_present,
                    rank, rank_change_24h, display_name, league, score,
                    steam_name, psn_name, xbox_name, club_tag, club_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def _insert_integrity(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: int,
        content_hash: str,
        changed_count: int,
        removed_count: int,
        reconstructed_count: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO snapshot_integrity (
                snapshot_id, content_hash, changed_entry_count,
                removed_entry_count, reconstructed_entry_count, verified_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                content_hash,
                changed_count,
                removed_count,
                reconstructed_count,
                _to_epoch_ms(datetime.now(timezone.utc)),
            ),
        )

    def _load_current_state(
        self, connection: sqlite3.Connection, season: str
    ) -> tuple[dict[str, PlayerEntry], dict[str, int]]:
        rows = connection.execute(
            """
            SELECT player_key, last_changed_snapshot_id,
                   rank, rank_change_24h, display_name, league, score,
                   steam_name, psn_name, xbox_name, club_tag, club_id
            FROM current_entries
            WHERE season = ?
            """,
            (season,),
        ).fetchall()
        state = {str(row["player_key"]): _row_to_player(row) for row in rows}
        changed_at = {
            str(row["player_key"]): int(row["last_changed_snapshot_id"]) for row in rows
        }
        return state, changed_at

    def _replace_current_state(
        self,
        connection: sqlite3.Connection,
        season: str,
        state: dict[str, PlayerEntry],
        changed_at: dict[str, int],
    ) -> None:
        connection.execute("DELETE FROM current_entries WHERE season = ?", (season,))
        connection.executemany(
            """
            INSERT INTO current_entries (
                season, player_key, last_changed_snapshot_id,
                rank, rank_change_24h, display_name, league, score,
                steam_name, psn_name, xbox_name, club_tag, club_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _current_values(season, key, changed_at[key], entry)
                for key, entry in state.items()
            ),
        )

    def _verify_current_state(
        self,
        connection: sqlite3.Connection,
        season: str,
        expected: dict[str, PlayerEntry],
    ) -> None:
        actual, _ = self._load_current_state(connection, season)
        if len(actual) != len(expected):
            raise DataIntegrityError(
                f"현재 상태 행 수 검증 실패: expected={len(expected)}, actual={len(actual)}"
            )
        if actual != expected:
            raise DataIntegrityError(
                "현재 상태가 수집된 원본 스냅샷과 일치하지 않습니다."
            )
        if _state_hash(actual.values()) != _state_hash(expected.values()):
            raise DataIntegrityError("현재 상태 SHA-256 검증에 실패했습니다.")

    def latest_snapshot_id(self, season: str = "s11") -> int | None:
        metadata = self.latest_snapshot(season)
        return metadata.snapshot_id if metadata else None

    def latest_snapshot(self, season: str = "s11") -> SnapshotMetadata | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    s.id, s.season, s.leaderboard_name, s.source_url,
                    s.source_updated_at_ms, s.fetched_at_ms, s.entry_count,
                    r.content_hash, r.state_event_count,
                    r.removed_entry_count, r.rank_change_event_count,
                    r.order_event_count, r.order_correction_count,
                    r.reconstructed_entry_count,
                    (
                        SELECT COUNT(*)
                        FROM current_entries ce
                        WHERE ce.season = s.season
                    ) AS current_entry_count
                FROM snapshots s
                LEFT JOIN snapshot_reconstruction r ON r.snapshot_id = s.id
                WHERE s.season = ?
                ORDER BY s.source_updated_at_ms DESC
                LIMIT 1
                """,
                (season,),
            ).fetchone()
        if row is None:
            return None
        reconstructed = row["reconstructed_entry_count"]
        return SnapshotMetadata(
            snapshot_id=int(row["id"]),
            season=str(row["season"]),
            leaderboard_name=str(row["leaderboard_name"]),
            source_url=str(row["source_url"]),
            source_updated_at=_from_epoch_ms(row["source_updated_at_ms"]),
            fetched_at=_from_epoch_ms(row["fetched_at_ms"]),
            entry_count=int(row["entry_count"]),
            content_hash=str(row["content_hash"] or ""),
            changed_entry_count=int(row["state_event_count"] or 0),
            removed_entry_count=int(row["removed_entry_count"] or 0),
            rank_change_event_count=int(row["rank_change_event_count"] or 0),
            order_event_count=int(row["order_event_count"] or 0),
            order_correction_count=int(row["order_correction_count"] or 0),
            integrity_verified=(
                reconstructed is not None
                and int(reconstructed) == int(row["entry_count"])
                and int(row["current_entry_count"]) == int(row["entry_count"])
            ),
        )

    def latest_entries(
        self,
        season: str = "s11",
        *,
        limit: int = 10_000,
        offset: int = 0,
    ) -> list[PlayerRecord]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit은 1 이상 10,000 이하여야 합니다.")
        if offset < 0:
            raise ValueError("offset은 0 이상이어야 합니다.")

        metadata = self.latest_snapshot(season)
        if metadata is None:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ? AS snapshot_id, ? AS season, ? AS source_updated_at_ms,
                       rank, rank_change_24h, display_name, league, score,
                       steam_name, psn_name, xbox_name, club_tag, club_id
                FROM current_entries
                WHERE season = ?
                ORDER BY rank ASC
                LIMIT ? OFFSET ?
                """,
                (
                    metadata.snapshot_id,
                    metadata.season,
                    _to_epoch_ms(metadata.source_updated_at),
                    season,
                    limit,
                    offset,
                ),
            ).fetchall()
        records = [_row_to_record(row) for row in rows]
        if offset == 0 and limit >= metadata.entry_count:
            if len(records) != metadata.entry_count:
                raise DataIntegrityError(
                    "최신 리더보드 행 수가 검증된 스냅샷과 일치하지 않습니다."
                )
            actual_hash = _state_hash(record.player for record in records)
            if not metadata.content_hash or actual_hash != metadata.content_hash:
                raise DataIntegrityError(
                    "최신 리더보드 내용이 검증된 스냅샷 해시와 일치하지 않습니다."
                )
        return records

    def search(
        self,
        query: str,
        *,
        season: str = "s11",
        exact: bool = False,
        limit: int = 20,
    ) -> list[PlayerRecord]:
        query = query.strip()
        if not query:
            return []
        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")

        metadata = self.latest_snapshot(season)
        if metadata is None:
            return []
        operator = "=" if exact else "LIKE"
        value = query if exact else f"%{_escape_like(query)}%"
        sql = f"""
            SELECT ? AS snapshot_id, ? AS season, ? AS source_updated_at_ms,
                   rank, rank_change_24h, display_name, league, score,
                   steam_name, psn_name, xbox_name, club_tag, club_id
            FROM current_entries
            WHERE season = ? AND (
                display_name {operator} ? COLLATE NOCASE OR
                steam_name {operator} ? COLLATE NOCASE OR
                psn_name {operator} ? COLLATE NOCASE OR
                xbox_name {operator} ? COLLATE NOCASE
            )
            ORDER BY rank ASC
            LIMIT ?
        """
        if operator == "LIKE":
            sql = sql.replace(" COLLATE NOCASE OR", " ESCAPE '\\' COLLATE NOCASE OR")
            sql = sql.replace(" COLLATE NOCASE\n", " ESCAPE '\\' COLLATE NOCASE\n")
        with self._connect() as connection:
            rows = connection.execute(
                sql,
                (
                    metadata.snapshot_id,
                    metadata.season,
                    _to_epoch_ms(metadata.source_updated_at),
                    season,
                    value,
                    value,
                    value,
                    value,
                    limit,
                ),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _find_history_target_keys(
        self,
        connection: sqlite3.Connection,
        query: str,
        season: str,
    ) -> set[str]:
        rows = connection.execute(
            """
            SELECT player_key
            FROM player_state_events
            WHERE season = ? AND display_name = ? COLLATE NOCASE
            UNION
            SELECT player_key
            FROM player_state_events
            WHERE season = ? AND steam_name = ? COLLATE NOCASE
            UNION
            SELECT player_key
            FROM player_state_events
            WHERE season = ? AND psn_name = ? COLLATE NOCASE
            UNION
            SELECT player_key
            FROM player_state_events
            WHERE season = ? AND xbox_name = ? COLLATE NOCASE
            """,
            (
                season,
                query,
                season,
                query,
                season,
                query,
                season,
                query,
            ),
        ).fetchall()
        return {str(row["player_key"]) for row in rows}

    def history_session(
        self,
        query: str,
        *,
        season: str = "s11",
        session: int = 1,
    ) -> PlayerHistorySession:
        if session < 1:
            raise ValueError("session은 1 이상이어야 합니다.")
        query = query.strip()
        if not query:
            return PlayerHistorySession(
                session=session, total_sessions=0, points=()
            )

        with self._connect() as connection:
            target_keys = self._find_history_target_keys(
                connection, query, season
            )
            if not target_keys:
                return PlayerHistorySession(
                    session=session, total_sessions=0, points=()
                )

            snapshot_rows = connection.execute(
                """
                SELECT id
                FROM snapshots
                WHERE season = ?
                ORDER BY id ASC
                """,
                (season,),
            ).fetchall()
            placeholders = ",".join("?" for _ in target_keys)
            target_event_rows = connection.execute(
                f"""
                SELECT snapshot_id, player_key, is_present, score
                FROM player_state_events
                WHERE season = ? AND player_key IN ({placeholders})
                ORDER BY snapshot_id ASC
                """,
                (season, *sorted(target_keys)),
            ).fetchall()
            ranges = _history_session_ranges(
                [int(row["id"]) for row in snapshot_rows],
                target_keys,
                target_event_rows,
            )
            ranges.sort(
                key=lambda item: (
                    item.end_snapshot_id,
                    item.start_snapshot_id,
                    item.player_key,
                )
            )
            total_sessions = len(ranges)
            if session > total_sessions:
                return PlayerHistorySession(
                    session=session,
                    total_sessions=total_sessions,
                    points=(),
                )
            selected = ranges[-session]
            points = self._history_points_for_session(
                connection,
                season=season,
                session_range=selected,
            )

        return PlayerHistorySession(
            session=session,
            total_sessions=total_sessions,
            points=tuple(points),
        )

    def _history_points_for_session(
        self,
        connection: sqlite3.Connection,
        *,
        season: str,
        session_range: _HistorySessionRange,
    ) -> list[PlayerHistoryPoint]:
        checkpoint = connection.execute(
            """
            SELECT snapshot_id, entry_count, content_hash
            FROM history_checkpoints
            WHERE season = ? AND snapshot_id <= ?
            ORDER BY snapshot_id DESC
            LIMIT 1
            """,
            (season, session_range.start_snapshot_id),
        ).fetchone()
        if checkpoint is None:
            checkpoint_id = 0
            current: dict[str, PlayerEntry] = {}
        else:
            checkpoint_id = int(checkpoint["snapshot_id"])
            current = self._load_history_checkpoint(connection, checkpoint)

        snapshots = connection.execute(
            """
            SELECT id, source_updated_at_ms, entry_count
            FROM snapshots
            WHERE season = ? AND id >= ? AND id <= ?
            ORDER BY id ASC
            """,
            (
                season,
                checkpoint_id if checkpoint_id else 0,
                session_range.end_snapshot_id,
            ),
        ).fetchall()
        state_rows = connection.execute(
            """
            SELECT snapshot_id, player_key, is_present,
                   display_name, league, score, steam_name, psn_name,
                   xbox_name, club_tag, club_id
            FROM player_state_events
            WHERE season = ? AND snapshot_id > ? AND snapshot_id <= ?
            ORDER BY snapshot_id ASC
            """,
            (season, checkpoint_id, session_range.end_snapshot_id),
        ).fetchall()
        rank_rows = connection.execute(
            """
            SELECT snapshot_id, player_key, rank_change_24h
            FROM rank_change_events
            WHERE season = ? AND snapshot_id > ? AND snapshot_id <= ?
            ORDER BY snapshot_id ASC
            """,
            (season, checkpoint_id, session_range.end_snapshot_id),
        ).fetchall()
        order_rows = connection.execute(
            """
            SELECT snapshot_id, player_key, target_rank
            FROM order_events
            WHERE season = ? AND snapshot_id > ? AND snapshot_id <= ?
            ORDER BY snapshot_id ASC, target_rank ASC
            """,
            (season, checkpoint_id, session_range.end_snapshot_id),
        ).fetchall()

        state_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        rank_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        order_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        for row in state_rows:
            state_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)
        for row in rank_rows:
            rank_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)
        for row in order_rows:
            order_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)

        points: list[PlayerHistoryPoint] = []
        for snapshot in snapshots:
            snapshot_id = int(snapshot["id"])
            if snapshot_id != checkpoint_id:
                upserts: dict[str, PlayerEntry] = {}
                removed: list[str] = []
                for row in state_by_snapshot.get(snapshot_id, []):
                    key = str(row["player_key"])
                    if int(row["is_present"]):
                        upserts[key] = _state_row_to_player(row)
                    else:
                        removed.append(key)
                current = _reconstruct_snapshot(
                    current,
                    state_upserts=upserts,
                    removed=removed,
                    rank_change_updates={
                        str(row["player_key"]): int(row["rank_change_24h"])
                        for row in rank_by_snapshot.get(snapshot_id, [])
                    },
                    order_updates={
                        str(row["player_key"]): int(row["target_rank"])
                        for row in order_by_snapshot.get(snapshot_id, [])
                    },
                    expected_count=int(snapshot["entry_count"]),
                )

            if snapshot_id < session_range.start_snapshot_id:
                continue
            player = current.get(session_range.player_key)
            if player is None:
                continue
            points.append(
                PlayerHistoryPoint(
                    snapshot_id=snapshot_id,
                    season=season,
                    source_updated_at=_from_epoch_ms(
                        snapshot["source_updated_at_ms"]
                    ),
                    rank=player.rank,
                    rank_change_24h=player.rank_change_24h,
                    score=player.score,
                    league=player.league,
                    display_name=player.display_name,
                )
            )
        return points

    def _load_history_checkpoint(
        self,
        connection: sqlite3.Connection,
        checkpoint: sqlite3.Row,
    ) -> dict[str, PlayerEntry]:
        snapshot_id = int(checkpoint["snapshot_id"])
        rows = connection.execute(
            """
            SELECT player_key, rank, rank_change_24h, display_name, league,
                   score, steam_name, psn_name, xbox_name, club_tag, club_id
            FROM history_checkpoint_entries
            WHERE snapshot_id = ?
            ORDER BY rank ASC
            """,
            (snapshot_id,),
        ).fetchall()
        expected_count = int(checkpoint["entry_count"])
        if len(rows) != expected_count:
            raise DataIntegrityError(
                f"checkpoint {snapshot_id} 행 수 불일치: "
                f"expected={expected_count}, actual={len(rows)}"
            )
        state = {
            str(row["player_key"]): _row_to_player(row)
            for row in rows
        }
        if _state_hash(state.values()) != str(checkpoint["content_hash"]):
            raise DataIntegrityError(
                f"checkpoint {snapshot_id} SHA-256 검증에 실패했습니다."
            )
        return state

    def history(
        self,
        query: str,
        *,
        season: str = "s11",
    ) -> list[PlayerHistoryPoint]:
        query = query.strip()
        if not query:
            return []
        with self._connect() as connection:
            target_rows = connection.execute(
                """
                SELECT DISTINCT player_key
                FROM player_state_events
                WHERE season = ? AND (
                    display_name = ? COLLATE NOCASE OR
                    steam_name = ? COLLATE NOCASE OR
                    psn_name = ? COLLATE NOCASE OR
                    xbox_name = ? COLLATE NOCASE
                )
                """,
                (season, query, query, query, query),
            ).fetchall()
            target_keys = {str(row["player_key"]) for row in target_rows}
            if not target_keys:
                return []

            snapshots = connection.execute(
                """
                SELECT id, source_updated_at_ms, entry_count
                FROM snapshots
                WHERE season = ?
                ORDER BY id ASC
                """,
                (season,),
            ).fetchall()
            state_rows = connection.execute(
                """
                SELECT snapshot_id, player_key, is_present,
                       display_name, league, score, steam_name, psn_name,
                       xbox_name, club_tag, club_id
                FROM player_state_events
                WHERE season = ?
                ORDER BY snapshot_id ASC
                """,
                (season,),
            ).fetchall()
            rank_rows = connection.execute(
                """
                SELECT snapshot_id, player_key, rank_change_24h
                FROM rank_change_events
                WHERE season = ?
                ORDER BY snapshot_id ASC
                """,
                (season,),
            ).fetchall()
            order_rows = connection.execute(
                """
                SELECT snapshot_id, player_key, target_rank
                FROM order_events
                WHERE season = ?
                ORDER BY snapshot_id ASC, target_rank ASC
                """,
                (season,),
            ).fetchall()

        state_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        rank_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        order_by_snapshot: dict[int, list[sqlite3.Row]] = {}
        for row in state_rows:
            state_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)
        for row in rank_rows:
            rank_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)
        for row in order_rows:
            order_by_snapshot.setdefault(int(row["snapshot_id"]), []).append(row)

        current: dict[str, PlayerEntry] = {}
        history: list[PlayerHistoryPoint] = []
        for snapshot in snapshots:
            snapshot_id = int(snapshot["id"])
            upserts: dict[str, PlayerEntry] = {}
            removed: list[str] = []
            for row in state_by_snapshot.get(snapshot_id, []):
                key = str(row["player_key"])
                if int(row["is_present"]):
                    upserts[key] = _state_row_to_player(row)
                else:
                    removed.append(key)
            rank_updates = {
                str(row["player_key"]): int(row["rank_change_24h"])
                for row in rank_by_snapshot.get(snapshot_id, [])
            }
            order_updates = {
                str(row["player_key"]): int(row["target_rank"])
                for row in order_by_snapshot.get(snapshot_id, [])
            }
            current = _reconstruct_snapshot(
                current,
                state_upserts=upserts,
                removed=removed,
                rank_change_updates=rank_updates,
                order_updates=order_updates,
                expected_count=int(snapshot["entry_count"]),
            )
            for key in target_keys:
                player = current.get(key)
                if player is None:
                    continue
                history.append(
                    PlayerHistoryPoint(
                        snapshot_id=snapshot_id,
                        season=season,
                        source_updated_at=_from_epoch_ms(
                            snapshot["source_updated_at_ms"]
                        ),
                        rank=player.rank,
                        rank_change_24h=player.rank_change_24h,
                        score=player.score,
                        league=player.league,
                        display_name=player.display_name,
                    )
                )
        history.sort(key=lambda point: (point.source_updated_at, point.rank))
        return history

    def snapshot_count(self, season: str = "s11") -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM snapshots WHERE season = ?",
                (season,),
            ).fetchone()
        return int(row["count"])


def _history_session_ranges(
    snapshot_ids: Sequence[int],
    target_keys: set[str],
    event_rows: Sequence[sqlite3.Row],
) -> list[_HistorySessionRange]:
    events = {
        (int(row["snapshot_id"]), str(row["player_key"])): row
        for row in event_rows
    }
    sessions: list[_HistorySessionRange] = []

    for player_key in sorted(target_keys):
        present = False
        ever_seen = False
        awaiting_score_change = False
        current_score: int | None = None
        closed_score: int | None = None
        active_start: int | None = None
        last_present: int | None = None
        unchanged_fetches = 0

        for snapshot_id in snapshot_ids:
            event = events.get((snapshot_id, player_key))
            reappeared = False
            score_changed = False
            if event is not None:
                if not int(event["is_present"]):
                    if active_start is not None and last_present is not None:
                        sessions.append(
                            _HistorySessionRange(
                                player_key=player_key,
                                start_snapshot_id=active_start,
                                end_snapshot_id=last_present,
                            )
                        )
                    present = False
                    active_start = None
                    last_present = None
                    unchanged_fetches = 0
                    awaiting_score_change = True
                    closed_score = current_score
                    current_score = None
                    continue

                new_score = int(event["score"])
                reappeared = ever_seen and not present
                score_changed = present and new_score != current_score
                present = True
                ever_seen = True
                current_score = new_score
            elif not present:
                continue

            if active_start is None:
                should_start = (
                    not awaiting_score_change
                    or reappeared
                    or (
                        closed_score is not None
                        and current_score != closed_score
                    )
                )
                if not should_start:
                    continue
                active_start = snapshot_id
                unchanged_fetches = 0
                awaiting_score_change = False
            elif score_changed:
                unchanged_fetches = 0
            else:
                unchanged_fetches += 1

            last_present = snapshot_id
            if unchanged_fetches >= SESSION_INACTIVITY_FETCHES:
                sessions.append(
                    _HistorySessionRange(
                        player_key=player_key,
                        start_snapshot_id=active_start,
                        end_snapshot_id=snapshot_id,
                    )
                )
                active_start = None
                last_present = None
                unchanged_fetches = 0
                awaiting_score_change = True
                closed_score = current_score

        if active_start is not None and last_present is not None:
            sessions.append(
                _HistorySessionRange(
                    player_key=player_key,
                    start_snapshot_id=active_start,
                    end_snapshot_id=last_present,
                )
            )

    return sessions


def _player_state(entry: PlayerEntry) -> _PlayerState:
    return _PlayerState(
        display_name=entry.display_name,
        league=entry.league,
        score=entry.score,
        steam_name=entry.steam_name,
        psn_name=entry.psn_name,
        xbox_name=entry.xbox_name,
        club_tag=entry.club_tag,
        club_id=entry.club_id,
    )


def _state_row_to_player(row: sqlite3.Row) -> PlayerEntry:
    return PlayerEntry(
        rank=0,
        rank_change_24h=0,
        display_name=str(row["display_name"]),
        league=int(row["league"]),
        score=int(row["score"]),
        steam_name=str(row["steam_name"]),
        psn_name=str(row["psn_name"]),
        xbox_name=str(row["xbox_name"]),
        club_tag=str(row["club_tag"]),
        club_id=str(row["club_id"]),
    )


def _state_event_values(
    snapshot_id: int,
    season: str,
    player_key: str,
    is_present: bool,
    entry: PlayerEntry,
) -> tuple[object, ...]:
    state = _player_state(entry)
    return (
        snapshot_id,
        season,
        player_key,
        int(is_present),
        state.display_name,
        state.league,
        state.score,
        state.steam_name,
        state.psn_name,
        state.xbox_name,
        state.club_tag,
        state.club_id,
    )


def _order_corrections(
    previous: dict[str, PlayerEntry],
    incoming: dict[str, PlayerEntry],
    mandatory_movers: set[str],
) -> set[str]:
    previous_base = [
        key
        for key, _ in sorted(previous.items(), key=lambda item: item[1].rank)
        if key in incoming and key not in mandatory_movers
    ]
    incoming_base = [
        key
        for key, _ in sorted(incoming.items(), key=lambda item: item[1].rank)
        if key not in mandatory_movers
    ]
    if set(previous_base) != set(incoming_base):
        raise DataIntegrityError("순서 복원 기준 유저 집합이 일치하지 않습니다.")
    if previous_base == incoming_base:
        return set()

    incoming_positions = {key: index for index, key in enumerate(incoming_base)}
    position_sequence = [incoming_positions[key] for key in previous_base]
    keep_indices = _longest_increasing_subsequence_indices(position_sequence)
    return {key for index, key in enumerate(previous_base) if index not in keep_indices}


def _longest_increasing_subsequence_indices(values: Sequence[int]) -> set[int]:
    if not values:
        return set()
    tails: list[int] = []
    tail_indices: list[int] = []
    predecessors = [-1] * len(values)
    for index, value in enumerate(values):
        position = bisect_left(tails, value)
        if position == len(tails):
            tails.append(value)
            tail_indices.append(index)
        else:
            tails[position] = value
            tail_indices[position] = index
        if position > 0:
            predecessors[index] = tail_indices[position - 1]

    result: set[int] = set()
    cursor = tail_indices[-1]
    while cursor >= 0:
        result.add(cursor)
        cursor = predecessors[cursor]
    return result


def _reconstruct_snapshot(
    previous: dict[str, PlayerEntry],
    *,
    state_upserts: dict[str, PlayerEntry],
    removed: Iterable[str],
    rank_change_updates: dict[str, int],
    order_updates: dict[str, int],
    expected_count: int,
) -> dict[str, PlayerEntry]:
    previous_order = [
        key for key, _ in sorted(previous.items(), key=lambda item: item[1].rank)
    ]
    states = {key: _player_state(entry) for key, entry in previous.items()}
    rank_changes = {key: entry.rank_change_24h for key, entry in previous.items()}
    for key in removed:
        states.pop(key, None)
        rank_changes.pop(key, None)
    for key, entry in state_upserts.items():
        states[key] = _player_state(entry)
    rank_changes.update(rank_change_updates)

    if len(states) != expected_count:
        raise DataIntegrityError(
            f"복원 상태 행 수 불일치: expected={expected_count}, actual={len(states)}"
        )
    missing_rank_change = states.keys() - rank_changes.keys()
    if missing_rank_change:
        raise DataIntegrityError(
            f"복원에 필요한 24시간 순위 변화가 없습니다: {sorted(missing_rank_change)[:3]}"
        )

    movers = set(order_updates)
    slots: list[str | None] = [None] * expected_count
    for key, target_rank in order_updates.items():
        if key not in states:
            raise DataIntegrityError(f"순서 이벤트 유저가 현재 상태에 없습니다: {key}")
        if not 1 <= target_rank <= expected_count:
            raise DataIntegrityError(
                f"순서 이벤트 순위 범위 오류: key={key}, rank={target_rank}"
            )
        if slots[target_rank - 1] is not None:
            raise DataIntegrityError(f"순서 이벤트 순위 충돌: rank={target_rank}")
        slots[target_rank - 1] = key

    base = [key for key in previous_order if key in states and key not in movers]
    base_iterator = iter(base)
    for index, key in enumerate(slots):
        if key is None:
            slots[index] = next(base_iterator, None)
    if next(base_iterator, None) is not None or any(key is None for key in slots):
        raise DataIntegrityError("순서 이벤트로 전체 순위를 복원할 수 없습니다.")
    ordered_keys = [key for key in slots if key is not None]
    if len(set(ordered_keys)) != expected_count or set(ordered_keys) != set(states):
        raise DataIntegrityError("복원된 순서에 유저 누락 또는 중복이 있습니다.")

    return {
        key: PlayerEntry(
            rank=rank,
            rank_change_24h=rank_changes[key],
            display_name=states[key].display_name,
            league=states[key].league,
            score=states[key].score,
            steam_name=states[key].steam_name,
            psn_name=states[key].psn_name,
            xbox_name=states[key].xbox_name,
            club_tag=states[key].club_tag,
            club_id=states[key].club_id,
        )
        for rank, key in enumerate(ordered_keys, start=1)
    }


def _validated_state(
    entries: Sequence[PlayerEntry],
    *,
    expected_count: int | None,
) -> dict[str, PlayerEntry]:
    if expected_count is not None and len(entries) != expected_count:
        raise DataIntegrityError(
            f"원본 행 수 불일치: expected={expected_count}, actual={len(entries)}"
        )
    if not entries:
        raise DataIntegrityError("빈 리더보드 스냅샷은 저장할 수 없습니다.")

    ranks = {entry.rank for entry in entries}
    expected_ranks = set(range(1, len(entries) + 1))
    if ranks != expected_ranks:
        missing = sorted(expected_ranks - ranks)[:10]
        unexpected = sorted(ranks - expected_ranks)[:10]
        raise DataIntegrityError(
            f"순위 연속성 검증 실패: missing={missing}, unexpected={unexpected}"
        )

    state: dict[str, PlayerEntry] = {}
    for entry in entries:
        if not entry.display_name.strip():
            raise DataIntegrityError(f"순위 {entry.rank}의 표시 이름이 비어 있습니다.")
        key = _player_key(entry.display_name)
        if key in state:
            raise DataIntegrityError(f"중복 유저 식별자 발견: {entry.display_name!r}")
        state[key] = entry
    return state


def _state_hash(entries: Sequence[PlayerEntry] | Iterator[PlayerEntry]) -> str:
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.rank):
        payload = json.dumps(
            [
                entry.rank,
                entry.rank_change_24h,
                entry.display_name,
                entry.league,
                entry.score,
                entry.steam_name,
                entry.psn_name,
                entry.xbox_name,
                entry.club_tag,
                entry.club_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(4, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _player_key(display_name: str) -> str:
    return display_name.casefold()


def _change_values(
    snapshot_id: int,
    season: str,
    player_key: str,
    is_present: bool,
    entry: PlayerEntry,
) -> tuple[object, ...]:
    return (
        snapshot_id,
        season,
        player_key,
        int(is_present),
        *_player_values(entry),
    )


def _current_values(
    season: str,
    player_key: str,
    snapshot_id: int,
    entry: PlayerEntry,
) -> tuple[object, ...]:
    return (season, player_key, snapshot_id, *_player_values(entry))


def _player_values(entry: PlayerEntry) -> tuple[object, ...]:
    return (
        entry.rank,
        entry.rank_change_24h,
        entry.display_name,
        entry.league,
        entry.score,
        entry.steam_name,
        entry.psn_name,
        entry.xbox_name,
        entry.club_tag,
        entry.club_id,
    )


def _row_to_player(row: sqlite3.Row) -> PlayerEntry:
    return PlayerEntry(
        rank=int(row["rank"]),
        rank_change_24h=int(row["rank_change_24h"]),
        display_name=str(row["display_name"]),
        league=int(row["league"]),
        score=int(row["score"]),
        steam_name=str(row["steam_name"]),
        psn_name=str(row["psn_name"]),
        xbox_name=str(row["xbox_name"]),
        club_tag=str(row["club_tag"]),
        club_id=str(row["club_id"]),
    )


def _row_to_record(row: sqlite3.Row) -> PlayerRecord:
    return PlayerRecord(
        snapshot_id=int(row["snapshot_id"]),
        season=str(row["season"]),
        source_updated_at=_from_epoch_ms(row["source_updated_at_ms"]),
        player=_row_to_player(row),
    )


def _to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


def _from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

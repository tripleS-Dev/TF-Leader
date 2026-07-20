from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tf_leader import (
    DataIntegrityError,
    LeaderboardClient,
    LeaderboardSnapshot,
    LeaderboardStore,
    PlayerEntry,
)
from tf_leader.charts import plot_history, render_history_png


def _snapshot(
    updated_at: datetime,
    *entries: PlayerEntry,
) -> LeaderboardSnapshot:
    return LeaderboardSnapshot(
        season="s11",
        name="Season 11",
        source_url="https://example.test/leaderboard",
        source_updated_at=updated_at,
        fetched_at=updated_at,
        entries=entries,
    )


def _player(
    name: str,
    rank: int,
    score: int,
    *,
    steam_name: str = "",
) -> PlayerEntry:
    return PlayerEntry(
        rank=rank,
        rank_change_24h=2,
        display_name=name,
        league=20,
        score=score,
        steam_name=steam_name,
        club_tag="TST",
    )


def test_client_parses_next_data() -> None:
    payload = {
        "props": {
            "pageProps": {
                "metadata": {"slug": "s11", "name": "Season 11"},
                "lastUpdatedAt": 1_700_000_000_000,
                "entries": [
                    {
                        "1": 1,
                        "2": "",
                        "3": "Player#1234",
                        "4": 20,
                        "5": 55_000,
                        "6": "SteamPlayer",
                        "12": "TST",
                        "13": "club-id",
                    }
                ],
            }
        }
    }
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    )

    snapshot = LeaderboardClient.parse_page(html, source_url="https://example.test")

    assert snapshot.season == "s11"
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].display_name == "Player#1234"
    assert snapshot.entries[0].rank_change_24h == 0
    assert snapshot.entries[0].league_name == "Diamond 1"


def test_delta_store_tracks_rank_only_changes_and_reconstructs_history(
    tmp_path,
) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    first_time = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    first = _snapshot(
        first_time,
        _player("Player#1234", 1, 50_000, steam_name="SteamPlayer"),
        _player("Rival#5678", 2, 49_000),
        _player("Third#9999", 3, 48_000),
    )
    second = _snapshot(
        first_time + timedelta(hours=1),
        _player("Rival#5678", 1, 51_000),
        _player("Player#1234", 2, 50_000, steam_name="SteamPlayer"),
        _player("Third#9999", 3, 48_000),
    )
    unchanged = _snapshot(
        first_time + timedelta(hours=2),
        *second.entries,
    )

    first_id, first_created = store.save_snapshot(first)
    duplicate_id, duplicate_created = store.save_snapshot(first)
    second_id, _ = store.save_snapshot(second)
    third_id, _ = store.save_snapshot(unchanged)

    assert first_created is True
    assert duplicate_created is False
    assert duplicate_id == first_id
    assert store.snapshot_count("s11") == 3

    matches = store.search("SteamPlayer", season="s11")
    assert len(matches) == 1
    assert matches[0].player.rank == 2

    history = store.history("Player#1234", season="s11")
    assert [point.rank for point in history] == [1, 2, 2]
    assert [point.score for point in history] == [50_000, 50_000, 50_000]

    metadata = store.latest_snapshot("s11")
    assert metadata is not None
    assert metadata.snapshot_id == third_id
    assert metadata.entry_count == 3
    assert metadata.changed_entry_count == 0
    assert metadata.removed_entry_count == 0
    assert metadata.integrity_verified is True
    latest = store.latest_entries("s11")
    assert len(latest) == 3
    assert latest[1].player.display_name == "Player#1234"

    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM player_state_events WHERE snapshot_id = ?",
                (first_id,),
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM player_state_events WHERE snapshot_id = ?",
                (second_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM player_state_events WHERE snapshot_id = ?",
                (third_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM order_events
            WHERE snapshot_id = ? AND player_key = 'player#1234'
            """,
                (second_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM order_events WHERE snapshot_id = ?",
                (second_id,),
            ).fetchone()[0]
            == 1
        )

    writer = sqlite3.connect(store.db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE snapshots SET fetched_at_ms = fetched_at_ms + 1 WHERE id = ?",
            (metadata.snapshot_id,),
        )
        assert store.latest_entries("s11")[1].player.rank == 2
    finally:
        writer.rollback()
        writer.close()

    output = plot_history(history, kind="rank", output=tmp_path / "rank.png")
    assert output.exists()
    assert output.stat().st_size > 0

    image = render_history_png(history, kind="score")
    assert image.startswith(b"\x89PNG\r\n\x1a\n")


def test_history_sessions_use_five_unchanged_fetches_and_checkpoints(
    tmp_path,
) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    first_time = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    scores = [100, 110, *([110] * 11), 120, *([120] * 5)]
    snapshot_ids: list[int] = []
    for index, score in enumerate(scores):
        snapshot_id, _ = store.save_snapshot(
            _snapshot(
                first_time + timedelta(minutes=20 * index),
                _player("Player#1234", 1, score),
            )
        )
        snapshot_ids.append(snapshot_id)

    latest = store.history_session("Player#1234")
    assert latest.session == 1
    assert latest.total_sessions == 2
    assert [point.snapshot_id for point in latest.points] == snapshot_ids[13:19]
    assert [point.score for point in latest.points] == [120] * 6

    previous = store.history_session("Player#1234", session=2)
    assert previous.total_sessions == 2
    assert [point.snapshot_id for point in previous.points] == snapshot_ids[:7]
    assert [point.score for point in previous.points] == [
        100,
        110,
        110,
        110,
        110,
        110,
        110,
    ]

    missing = store.history_session("Player#1234", session=3)
    assert missing.total_sessions == 2
    assert missing.points == ()

    with sqlite3.connect(store.db_path) as connection:
        checkpoints = connection.execute(
            """
            SELECT snapshot_id
            FROM history_checkpoints
            ORDER BY snapshot_id
            """
        ).fetchall()
    assert checkpoints == [(snapshot_ids[0],), (snapshot_ids[12],)]



def test_equal_score_reorder_is_saved_as_minimal_correction(tmp_path) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    now = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    first = _snapshot(
        now,
        _player("TieA#0001", 1, 50_000),
        _player("TieB#0002", 2, 50_000),
    )
    second = _snapshot(
        now + timedelta(minutes=20),
        _player("TieB#0002", 1, 50_000),
        _player("TieA#0001", 2, 50_000),
    )
    store.save_snapshot(first)
    second_id, _ = store.save_snapshot(second)

    assert [point.rank for point in store.history("TieA#0001")] == [1, 2]
    metadata = store.latest_snapshot("s11")
    assert metadata is not None
    assert metadata.changed_entry_count == 0
    assert metadata.order_event_count == 1
    assert metadata.order_correction_count == 1
    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM order_events
            WHERE snapshot_id = ? AND is_correction = 1
            """,
                (second_id,),
            ).fetchone()[0]
            == 1
        )


def test_delta_store_records_additions_removals_and_absent_history(tmp_path) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    first_time = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    first = _snapshot(
        first_time,
        _player("Player#1234", 1, 50_000),
        _player("Removed#0001", 2, 49_000),
    )
    second = _snapshot(
        first_time + timedelta(minutes=20),
        _player("New#0002", 1, 51_000),
        _player("Player#1234", 2, 50_000),
    )

    store.save_snapshot(first)
    second_id, _ = store.save_snapshot(second)

    metadata = store.latest_snapshot("s11")
    assert metadata is not None
    assert metadata.changed_entry_count == 1
    assert metadata.removed_entry_count == 1
    assert [row.player.display_name for row in store.latest_entries("s11")] == [
        "New#0002",
        "Player#1234",
    ]
    assert len(store.history("Removed#0001", season="s11")) == 1
    assert len(store.history("New#0002", season="s11")) == 1

    with sqlite3.connect(store.db_path) as connection:
        tombstone = connection.execute(
            """
            SELECT is_present FROM player_state_events
            WHERE snapshot_id = ? AND player_key = ?
            """,
            (second_id, "removed#0001"),
        ).fetchone()
    assert tombstone == (0,)


def test_invalid_or_out_of_order_snapshot_never_replaces_latest(tmp_path) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    now = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    valid = _snapshot(now, _player("Player#1234", 1, 50_000))
    store.save_snapshot(valid, expected_entry_count=1)

    with pytest.raises(DataIntegrityError, match="원본 행 수 불일치"):
        store.save_snapshot(valid, expected_entry_count=10_000)

    invalid_ranks = _snapshot(
        now + timedelta(minutes=20),
        _player("Player#1234", 2, 50_000),
    )
    with pytest.raises(DataIntegrityError, match="순위 연속성"):
        store.save_snapshot(invalid_ranks)

    older = _snapshot(
        now - timedelta(minutes=20),
        _player("Player#1234", 1, 51_000),
    )
    with pytest.raises(DataIntegrityError, match="오래된 스냅샷"):
        store.save_snapshot(older)

    same_timestamp_different_content = _snapshot(
        now,
        _player("Player#1234", 1, 51_000),
    )
    with pytest.raises(DataIntegrityError, match="동일한 원본 갱신 시각"):
        store.save_snapshot(same_timestamp_different_content)

    assert store.snapshot_count("s11") == 1
    assert store.latest_entries("s11")[0].player.score == 50_000


def test_v1_full_snapshots_migrate_without_deleting_legacy_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    first_ms = int(datetime(2026, 7, 16, 8, tzinfo=timezone.utc).timestamp() * 1_000)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season TEXT NOT NULL,
                leaderboard_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_updated_at_ms INTEGER NOT NULL,
                fetched_at_ms INTEGER NOT NULL,
                entry_count INTEGER NOT NULL,
                UNIQUE (season, source_updated_at_ms)
            );
            CREATE TABLE entries (
                snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
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
            """
        )
        connection.executemany(
            """
            INSERT INTO snapshots (
                id, season, leaderboard_name, source_url,
                source_updated_at_ms, fetched_at_ms, entry_count
            ) VALUES (?, 's11', 'Season 11', 'https://example.test', ?, ?, 2)
            """,
            (
                (1, first_ms, first_ms),
                (2, first_ms + 1_200_000, first_ms + 1_200_000),
            ),
        )
        connection.executemany(
            """
            INSERT INTO entries (
                snapshot_id, rank, rank_change_24h, display_name, league, score,
                steam_name, psn_name, xbox_name, club_tag, club_id
            ) VALUES (?, ?, 0, ?, 20, ?, '', '', '', '', '')
            """,
            (
                (1, 1, "Player#1234", 50_000),
                (1, 2, "Rival#5678", 49_000),
                (2, 1, "Rival#5678", 49_000),
                (2, 2, "Player#1234", 50_000),
            ),
        )

    store = LeaderboardStore(db_path)

    assert db_path.with_name("legacy.sqlite3-pre-delta-v1.bak").exists()
    assert [row.player.display_name for row in store.latest_entries("s11")] == [
        "Rival#5678",
        "Player#1234",
    ]
    assert [point.rank for point in store.history("Player#1234")] == [1, 2]
    metadata = store.latest_snapshot("s11")
    assert metadata is not None and metadata.integrity_verified is True

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM current_entries").fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM snapshot_reconstruction"
            ).fetchone()[0]
            == 2
        )
    with sqlite3.connect(
        db_path.with_name("legacy.sqlite3-pre-delta-v1.bak")
    ) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 4


def test_failed_delta_write_rolls_back_materialized_state(
    tmp_path, monkeypatch
) -> None:
    store = LeaderboardStore(tmp_path / "leaderboard.sqlite3")
    now = datetime(2026, 7, 16, 8, tzinfo=timezone.utc)
    store.save_snapshot(_snapshot(now, _player("Player#1234", 1, 50_000)))

    def fail_integrity(*args, **kwargs) -> None:
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(store, "_verify_current_state", fail_integrity)
    changed = _snapshot(
        now + timedelta(minutes=20),
        _player("Player#1234", 1, 60_000),
    )
    with pytest.raises(RuntimeError, match="simulated disk failure"):
        store.save_snapshot(changed)

    assert store.snapshot_count("s11") == 1
    assert store.latest_entries("s11")[0].player.score == 50_000

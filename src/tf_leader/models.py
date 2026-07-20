from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


LEAGUE_NAMES: dict[int, str] = {
    0: "Unranked",
    1: "Bronze 4",
    2: "Bronze 3",
    3: "Bronze 2",
    4: "Bronze 1",
    5: "Silver 4",
    6: "Silver 3",
    7: "Silver 2",
    8: "Silver 1",
    9: "Gold 4",
    10: "Gold 3",
    11: "Gold 2",
    12: "Gold 1",
    13: "Platinum 4",
    14: "Platinum 3",
    15: "Platinum 2",
    16: "Platinum 1",
    17: "Diamond 4",
    18: "Diamond 3",
    19: "Diamond 2",
    20: "Diamond 1",
    21: "Ruby",
}


@dataclass(frozen=True, slots=True)
class PlayerEntry:
    rank: int
    rank_change_24h: int
    display_name: str
    league: int
    score: int
    steam_name: str = ""
    psn_name: str = ""
    xbox_name: str = ""
    club_tag: str = ""
    club_id: str = ""

    @property
    def league_name(self) -> str:
        return LEAGUE_NAMES.get(self.league, f"Unknown ({self.league})")


@dataclass(frozen=True, slots=True)
class LeaderboardSnapshot:
    season: str
    name: str
    source_url: str
    source_updated_at: datetime
    fetched_at: datetime
    entries: tuple[PlayerEntry, ...]


@dataclass(frozen=True, slots=True)
class SnapshotMetadata:
    snapshot_id: int
    season: str
    leaderboard_name: str
    source_url: str
    source_updated_at: datetime
    fetched_at: datetime
    entry_count: int
    content_hash: str = ""
    changed_entry_count: int = 0
    removed_entry_count: int = 0
    rank_change_event_count: int = 0
    order_event_count: int = 0
    order_correction_count: int = 0
    integrity_verified: bool = False


@dataclass(frozen=True, slots=True)
class PlayerRecord:
    snapshot_id: int
    season: str
    source_updated_at: datetime
    player: PlayerEntry


@dataclass(frozen=True, slots=True)
class PlayerHistoryPoint:
    snapshot_id: int
    season: str
    source_updated_at: datetime
    rank: int
    rank_change_24h: int
    score: int
    league: int
    display_name: str

    @property
    def league_name(self) -> str:
        return LEAGUE_NAMES.get(self.league, f"Unknown ({self.league})")


@dataclass(frozen=True, slots=True)
class PlayerHistorySession:
    session: int
    total_sessions: int
    points: tuple[PlayerHistoryPoint, ...]


@dataclass(frozen=True, slots=True)
class SyncResult:
    snapshot_id: int
    season: str
    source_updated_at: datetime
    entries_saved: int
    created: bool
    changed_entries: int = 0
    removed_entries: int = 0
    rank_change_events: int = 0
    order_events: int = 0
    order_corrections: int = 0

from __future__ import annotations

import json
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

import httpx

from .models import LeaderboardSnapshot, PlayerEntry


class LeaderboardFetchError(RuntimeError):
    """Raised when Embark's leaderboard page cannot be fetched or decoded."""


class _NextDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "__NEXT_DATA__":
            self._capturing = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    @property
    def next_data(self) -> str:
        return "".join(self._parts)


class LeaderboardClient:
    """Fetch and decode THE FINALS' public Embark leaderboard page."""

    BASE_URL = "https://id.embark.games"
    COLUMN_RANK = "1"
    COLUMN_RANK_CHANGE = "2"
    COLUMN_NAME = "3"
    COLUMN_LEAGUE = "4"
    COLUMN_SCORE = "5"
    COLUMN_STEAM = "6"
    COLUMN_PSN = "7"
    COLUMN_XBOX = "8"
    COLUMN_CLUB_TAG = "12"
    COLUMN_CLUB_ID = "13"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        locale: str = "ko-KR",
        user_agent: str = "TF-Leader/0.1 (+local leaderboard archive)",
    ) -> None:
        self.timeout = timeout
        self.locale = locale
        self.user_agent = user_agent

    def fetch(self, season: str = "s11") -> LeaderboardSnapshot:
        season = self._validate_season(season)
        url = f"{self.BASE_URL}/{self.locale}/the-finals/leaderboards/{season}"
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": f"{self.locale},en;q=0.8",
            "User-Agent": self.user_agent,
        }

        try:
            with httpx.Client(
                follow_redirects=True,
                headers=headers,
                timeout=self.timeout,
            ) as client:
                response = client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LeaderboardFetchError(f"리더보드 요청에 실패했습니다: {exc}") from exc

        return self.parse_page(response.text, source_url=str(response.url))

    @classmethod
    def parse_page(cls, html: str, *, source_url: str) -> LeaderboardSnapshot:
        parser = _NextDataParser()
        parser.feed(html)
        if not parser.next_data:
            raise LeaderboardFetchError("페이지에서 __NEXT_DATA__를 찾지 못했습니다.")

        try:
            payload: dict[str, Any] = json.loads(parser.next_data)
            page_props = payload["props"]["pageProps"]
            metadata = page_props["metadata"]
            raw_entries = page_props["entries"]
            updated_ms = int(page_props["lastUpdatedAt"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LeaderboardFetchError(
                "Embark 리더보드 데이터 형식이 예상과 다릅니다."
            ) from exc

        if not isinstance(raw_entries, list):
            raise LeaderboardFetchError("entries가 목록 형식이 아닙니다.")

        entries = tuple(cls._parse_entry(entry) for entry in raw_entries)
        season = str(metadata.get("slug") or metadata.get("id") or "unknown")
        name = str(metadata.get("name") or season)

        return LeaderboardSnapshot(
            season=season,
            name=name,
            source_url=source_url,
            source_updated_at=datetime.fromtimestamp(
                updated_ms / 1000, tz=timezone.utc
            ),
            fetched_at=datetime.now(timezone.utc),
            entries=entries,
        )

    @classmethod
    def _parse_entry(cls, raw: Any) -> PlayerEntry:
        if not isinstance(raw, dict):
            raise LeaderboardFetchError("리더보드 행이 객체 형식이 아닙니다.")
        try:
            return PlayerEntry(
                rank=int(raw[cls.COLUMN_RANK]),
                rank_change_24h=_int_or_zero(raw.get(cls.COLUMN_RANK_CHANGE)),
                display_name=str(raw[cls.COLUMN_NAME]),
                league=_int_or_zero(raw.get(cls.COLUMN_LEAGUE)),
                score=_int_or_zero(raw.get(cls.COLUMN_SCORE)),
                steam_name=str(raw.get(cls.COLUMN_STEAM, "") or ""),
                psn_name=str(raw.get(cls.COLUMN_PSN, "") or ""),
                xbox_name=str(raw.get(cls.COLUMN_XBOX, "") or ""),
                club_tag=str(raw.get(cls.COLUMN_CLUB_TAG, "") or ""),
                club_id=str(raw.get(cls.COLUMN_CLUB_ID, "") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LeaderboardFetchError(
                f"리더보드 행을 해석할 수 없습니다: {raw!r}"
            ) from exc

    @staticmethod
    def _validate_season(season: str) -> str:
        normalized = season.strip().lower()
        if (
            not normalized
            or not normalized.startswith("s")
            or not normalized[1:].isdigit()
        ):
            raise ValueError("season은 's11' 같은 형식이어야 합니다.")
        return normalized


def _int_or_zero(value: Any) -> int:
    return 0 if value in (None, "") else int(value)

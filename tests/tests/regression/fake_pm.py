from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FakeStats:
    http_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    http_time_s: float = 0.0
    elapsed_s: float = 0.0
    by_host: Dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "http_requests": self.http_requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "http_time_s": round(self.http_time_s, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "by_host": dict(self.by_host),
        }


class FakePolymarketClient:
    """
    Offline fake for regression tests.
    Matches the methods used by analysis.py (and aclose/cache fields).
    """

    def __init__(
        self,
        *,
        markets_by_slug: Dict[str, Dict[str, Any]],
        events_by_slug: Dict[str, Dict[str, Any]],
        gamma_by_condition: Dict[str, Dict[str, Any]],
        holders_by_condition: Dict[str, Any],
        leaderboard_by_user: Dict[str, Optional[Dict[str, Any]]],
        positions_by_user_market: Dict[Tuple[str, str], Any],
        closed_by_user: Dict[str, List[Dict[str, Any]]],
    ):
        self.cache = None
        self.stats = FakeStats()

        self._markets_by_slug = markets_by_slug
        self._events_by_slug = events_by_slug
        self._gamma_by_condition = gamma_by_condition
        self._holders_by_condition = holders_by_condition
        self._leaderboard_by_user = leaderboard_by_user
        self._positions_by_user_market = positions_by_user_market
        self._closed_by_user = closed_by_user

        self.calls: Dict[str, int] = {
            "get_market_by_slug": 0,
            "get_event_by_slug": 0,
            "gamma_market": 0,
            "get_holders": 0,
            "get_leaderboard_user": 0,
            "get_positions_for_user_market": 0,
            "get_closed_positions_recent": 0,
        }

    async def aclose(self) -> None:
        return

    async def get_market_by_slug(self, slug: str) -> Dict[str, Any]:
        self.calls["get_market_by_slug"] += 1
        return self._markets_by_slug.get(slug, {})

    async def get_event_by_slug(self, slug: str) -> Dict[str, Any]:
        self.calls["get_event_by_slug"] += 1
        return self._events_by_slug.get(slug, {})

    async def gamma_market(self, *, condition_id: str) -> Dict[str, Any]:
        self.calls["gamma_market"] += 1
        return self._gamma_by_condition.get(condition_id, {})

    async def get_holders(self, *, condition_id: str, limit: int = 20, min_balance: int = 1) -> Any:
        self.calls["get_holders"] += 1
        # test harness ignores limit/min_balance; your analysis already handles caps
        return self._holders_by_condition.get(condition_id)

    async def get_leaderboard_user(self, user: str) -> Optional[Dict[str, Any]]:
        self.calls["get_leaderboard_user"] += 1
        return self._leaderboard_by_user.get(user)

    async def get_positions_for_user_market(
        self,
        *,
        user: Optional[str] = None,
        addr: Optional[str] = None,
        condition_id: str,
        limit: int = 200,
        size_threshold: float = 0.0,
    ) -> Any:
        self.calls["get_positions_for_user_market"] += 1
        who = user or addr
        if not who:
            return []
        return self._positions_by_user_market.get((str(who), str(condition_id)), [])

    async def get_closed_positions_recent(
        self,
        *,
        user: Optional[str] = None,
        addr: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        self.calls["get_closed_positions_recent"] += 1
        who = user or addr
        if not who:
            return []
        rows = list(self._closed_by_user.get(str(who), []))
        # emulate pagination
        offset = max(0, int(offset))
        limit = max(1, int(limit))
        return rows[offset : offset + limit]

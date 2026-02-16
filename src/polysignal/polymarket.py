from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import httpx
from diskcache import Cache
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError))


@dataclass
class RequestStats:
    started_at: float = field(default_factory=time.perf_counter)
    http_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    http_time_s: float = 0.0
    by_host: Dict[str, int] = field(default_factory=dict)

    def record_http(self, host: str, dt: float) -> None:
        self.http_requests += 1
        self.http_time_s += max(0.0, dt)
        self.by_host[host] = self.by_host.get(host, 0) + 1

    def snapshot(self) -> Dict[str, Any]:
        elapsed = time.perf_counter() - self.started_at
        return {
            "http_requests": self.http_requests,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "http_time_s": round(self.http_time_s, 3),
            "elapsed_s": round(elapsed, 3),
            "by_host": dict(self.by_host),
        }


class PolymarketClient:
    """
    Gamma API: markets/events/tags
    Data API: holders/positions/closed-positions/leaderboard
    """

    def __init__(
        self,
        cache: Optional[Cache] = None,
        timeout_s: float = 25.0,
        *,
        use_cache: bool = True,
        ttl_gamma_s: int = 6 * 60 * 60,
        ttl_data_s: int = 5 * 60,
    ):
        self.cache = cache
        self.use_cache = bool(use_cache and cache is not None)
        self.ttl_gamma_s = int(ttl_gamma_s)
        self.ttl_data_s = int(ttl_data_s)

        self.stats = RequestStats()
        headers = {"User-Agent": "polysignal/0.1"}

        self._gamma = httpx.AsyncClient(timeout=timeout_s, headers=headers)
        self._data = httpx.AsyncClient(timeout=timeout_s, headers=headers)

    async def aclose(self) -> None:
        await self._gamma.aclose()
        await self._data.aclose()

    # ---------------- Cache helpers ----------------

    def _cache_key(self, url: str, params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return f"GET:{url}"
        items = []
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                for vv in v:
                    items.append((k, str(vv)))
            else:
                items.append((k, str(v)))
        qs = urlencode(sorted(items), doseq=True)
        return f"GET:{url}?{qs}"

    def _ttl_for_url(self, url: str) -> Optional[int]:
        host = (urlparse(url).netloc or "").lower()
        if "gamma-api.polymarket.com" in host:
            ttl = self.ttl_gamma_s
        elif "data-api.polymarket.com" in host:
            ttl = self.ttl_data_s
        else:
            ttl = min(self.ttl_gamma_s, self.ttl_data_s)
        return None if ttl <= 0 else int(ttl)

    def _cache_get(self, key: str) -> tuple[bool, Any]:
        if not self.use_cache or self.cache is None:
            return (False, None)
        cached = self.cache.get(key, default=None)
        if cached is not None or (key in self.cache):
            self.stats.cache_hits += 1
            return (True, cached)
        self.stats.cache_misses += 1
        return (False, None)

    def _cache_set(self, key: str, value: Any, *, url: str) -> None:
        if not self.use_cache or self.cache is None:
            return
        expire = self._ttl_for_url(url)
        self.cache.set(key, value, expire=expire)

    # ---------------- HTTP ----------------

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.7, min=0.7, max=6),
        stop=stop_after_attempt(6),
        reraise=True,
    )
    async def _get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict | None = None,
        *,
        allow_404: bool = False,
    ) -> Any:
        key = self._cache_key(url, params)
        hit, cached = self._cache_get(key)
        if hit:
            return cached

        t0 = time.perf_counter()
        resp = await client.get(url, params=params)
        dt = time.perf_counter() - t0
        self.stats.record_http(urlparse(url).netloc, dt)

        if allow_404 and resp.status_code == 404:
            self._cache_set(key, None, url=url)
            return None

        if resp.status_code in (429, 500, 502, 503, 504):
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        self._cache_set(key, data, url=url)
        return data

    # ---------------- Gamma API ----------------

    async def gamma_market(self, *, condition_id: str) -> Dict[str, Any]:
        """
        Fetch the Gamma market object for a given conditionId.
        Uses /markets?condition_ids[]=... (list endpoint).
        """
        url = f"{GAMMA_BASE}/markets"
        params = {"condition_ids": [str(condition_id)], "limit": 1}
        data = await self._get_json(self._gamma, url, params=params, allow_404=True)

        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if isinstance(data, dict):
            return data
        return {}

    async def get_market_by_slug(self, slug: str) -> Dict[str, Any]:
        # First try the slug endpoint; fall back to list endpoint.
        url = f"{GAMMA_BASE}/markets/slug/{slug}"
        data = await self._get_json(self._gamma, url, allow_404=True)
        if isinstance(data, dict):
            return data

        url2 = f"{GAMMA_BASE}/markets"
        data2 = await self._get_json(self._gamma, url2, params={"slug": [slug], "limit": 1}, allow_404=True)
        if isinstance(data2, list) and data2 and isinstance(data2[0], dict):
            return data2[0]
        if isinstance(data2, dict):
            return data2
        raise ValueError("Gamma market lookup returned non-dict")

    async def get_event_by_slug(self, slug: str) -> Dict[str, Any]:
        """
        Robust: accepts dict OR [dict]. Fallback to /events?slug[]=...
        """
        url = f"{GAMMA_BASE}/events/slug/{slug}"
        data = await self._get_json(self._gamma, url, allow_404=True)

        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]

        url2 = f"{GAMMA_BASE}/events"
        data2 = await self._get_json(self._gamma, url2, params={"slug": [slug], "limit": 1}, allow_404=True)
        if isinstance(data2, list) and data2 and isinstance(data2[0], dict):
            return data2[0]
        if isinstance(data2, dict):
            return data2

        raise ValueError("Gamma event lookup returned non-dict")

    async def get_markets(self, *, event_id: Optional[str] = None, limit: int = 200, offset: int = 0) -> Any:
        """
        Used by some analysis versions: list markets filtered by event_id.
        """
        url = f"{GAMMA_BASE}/markets"
        params: Dict[str, Any] = {"limit": int(max(0, limit)), "offset": int(max(0, offset))}
        if event_id is not None:
            params["event_id"] = int(event_id)
        return await self._get_json(self._gamma, url, params=params, allow_404=True)

    # ---------------- Data API ----------------

    async def get_holders(self, *, condition_id: str, limit: int = 20, min_balance: int = 1) -> Any:
        """
        Data API holders requires market=string[] (condition IDs).
        """
        url = f"{DATA_BASE}/holders"
        params = {
            "market": [str(condition_id)],
            "limit": int(max(1, min(20, limit))),  # docs cap at 20
            "minBalance": int(max(0, min_balance)),
        }
        return await self._get_json(self._data, url, params=params, allow_404=True)

    async def get_positions_for_user_market(
        self,
        *,
        user: Optional[str] = None,
        addr: Optional[str] = None,
        condition_id: str,
        limit: int = 200,
        size_threshold: float = 0.0,
    ) -> Any:
        """
        Data API positions supports market=string[] plus user.
        Accept both 'user' and 'addr' for compatibility.
        """
        who = user or addr
        if not who:
            raise ValueError("get_positions_for_user_market requires user/addr")

        url = f"{DATA_BASE}/positions"
        params: Dict[str, Any] = {
            "user": str(who),
            "market": [str(condition_id)],
            "limit": int(max(1, min(500, limit))),
        }
        if size_threshold and size_threshold > 0:
            params["sizeThreshold"] = float(size_threshold)

        return await self._get_json(self._data, url, params=params, allow_404=True)

    async def get_closed_positions_recent(
        self,
        *,
        user: Optional[str] = None,
        addr: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Any:
        """
        Data API closed positions uses 'user' and optional market/event filters.
        Accept both 'user' and 'addr' for compatibility.
        """
        who = user or addr
        if not who:
            raise ValueError("get_closed_positions_recent requires user/addr")

        url = f"{DATA_BASE}/closed-positions"
        params = {
            "user": str(who),
            "limit": int(max(1, min(50, limit))),
            "offset": int(max(0, offset)),
            "sortBy": "timestamp",
            "sortDirection": "desc",
        }
        return await self._get_json(self._data, url, params=params, allow_404=True)

    # ---------- Leaderboard (PnL) ----------

    @staticmethod
    def _leaderboard_row_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
        """
        Normalize leaderboard responses to a single row dict, if present.
        Supports:
          - [ { ...row... }, ... ]
          - { "data": [ { ...row... }, ... ], ... }
          - { ...row... }  (rare)
        """
        if not payload:
            return None

        if isinstance(payload, list):
            return payload[0] if payload and isinstance(payload[0], dict) else None

        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]
            # If it already looks like a row
            return payload

        return None

    async def get_leaderboard_user(self, user: str) -> Optional[Dict[str, Any]]:
        """
        Prefer v1 leaderboard endpoint for stable ALL-time PnL, but fall back to legacy /leaderboard
        to avoid breaking older environments.
        """
        params_v1 = {
            "user": str(user),
            "timePeriod": "ALL",
            "category": "OVERALL",
            "limit": 1,
        }

        # 1) Preferred: /v1/leaderboard
        url_v1 = f"{DATA_BASE}/v1/leaderboard"
        data_v1 = await self._get_json(self._data, url_v1, params=params_v1, allow_404=True)
        row = self._leaderboard_row_from_payload(data_v1)
        if row is not None:
            return row

        # 2) Fallback: legacy /leaderboard
        url_legacy = f"{DATA_BASE}/leaderboard"
        params_legacy = {"user": str(user), "limit": 1}
        data_legacy = await self._get_json(self._data, url_legacy, params=params_legacy, allow_404=True)
        return self._leaderboard_row_from_payload(data_legacy)

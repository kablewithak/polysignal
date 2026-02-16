from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import polysignal.analysis as analysis
import polysignal.cli as cli


def _addr(ch: str) -> str:
    # deterministic 40-hex address
    return "0x" + (ch * 40)


ADDR_A = _addr("1")  # has leaderboard pnl
ADDR_B = _addr("2")  # unknown pnl, has recent closes -> [REC]
ADDR_C = _addr("3")  # has leaderboard pnl


class FakeCache:
    def close(self) -> None:
        return


class FakeStats:
    def snapshot(self):
        return {
            "http_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "http_time_s": 0.0,
            "elapsed_s": 0.0,
            "by_host": {},
        }


class FakePolymarketClient:
    def __init__(self):
        self.stats = FakeStats()
        self.cache = FakeCache()

        self.calls = {
            "get_event_by_slug": 0,
            "get_market_by_slug": 0,
            "gamma_market": 0,
            "get_holders": 0,
            "get_positions_for_user_market": 0,
            "get_closed_positions_recent": 0,
            "get_leaderboard_user": 0,
        }

        # modes to exercise gates + determinism
        self._expired_mode = False
        self._closed_mode = False
        self._inactive_mode = False
        self._reverse_holders = False

    async def aclose(self) -> None:
        return

    # ---- Gamma API (fake) ----

    async def get_event_by_slug(self, slug: str):
        self.calls["get_event_by_slug"] += 1

        # Event returns partial market entry (missing conditionId) to exercise fallback.
        return {
            "title": "Test Event",
            "slug": slug,
            "id": 123,
            "markets": [
                {
                    "question": "Will Test Team win?",
                    "slug": "test-market-slug",
                    # conditionId intentionally missing
                }
            ],
        }

    async def get_market_by_slug(self, slug: str):
        self.calls["get_market_by_slug"] += 1
        assert slug == "test-market-slug"

        return {
            "question": "Will Test Team win?",
            "slug": slug,
            "conditionId": "0xCOND",
        }

    async def gamma_market(self, *, condition_id: str):
        self.calls["gamma_market"] += 1
        assert condition_id == "0xCOND"

        # Ensure closed/active gates take precedence over expiry (expiry is checked later)
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

        if self._closed_mode:
            return {
                "conditionId": condition_id,
                "active": True,
                "closed": True,
                "endDate": future,
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.40", "0.60"],
            }

        if self._inactive_mode:
            return {
                "conditionId": condition_id,
                "active": False,
                "closed": False,
                "endDate": future,
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.40", "0.60"],
            }

        if self._expired_mode:
            return {
                "conditionId": condition_id,
                "active": True,
                "closed": False,
                "endDate": past,
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["0.40", "0.60"],
            }

        return {
            "conditionId": condition_id,
            "active": True,
            "closed": False,
            "endDate": future,
            "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.40", "0.60"],
        }

    # ---- Data API (fake) ----

    async def get_holders(self, *, condition_id: str, limit: int = 20, min_balance: int = 1):
        self.calls["get_holders"] += 1
        assert condition_id == "0xCOND"

        wallets = [ADDR_A, ADDR_B, ADDR_C]
        if self._reverse_holders:
            wallets = list(reversed(wallets))

        return [
            {
                "token": "fake",
                "holders": [{"proxyWallet": w} for w in wallets],
            }
        ]

    async def get_leaderboard_user(self, user: str):
        self.calls["get_leaderboard_user"] += 1
        if user == ADDR_A:
            return {"user": user, "pnl": 10_000}
        if user == ADDR_C:
            return {"user": user, "pnl": 20_000}
        # ADDR_B: unknown
        return None

    async def get_positions_for_user_market(
        self,
        *,
        user=None,
        addr=None,
        condition_id: str,
        limit: int = 200,
        size_threshold: float = 0.0,
    ):
        self.calls["get_positions_for_user_market"] += 1
        who = addr or user
        assert condition_id == "0xCOND"

        # A + B are "No", C is "Yes"
        if who == ADDR_C:
            return [{"outcome": "Yes", "currentValue": 80}]
        return [{"outcome": "No", "currentValue": 50}]

    async def get_closed_positions_recent(self, *, user=None, addr=None, limit: int = 50, offset: int = 0):
        self.calls["get_closed_positions_recent"] += 1
        who = addr or user

        now_ts = int(datetime.now(timezone.utc).timestamp())

        # A has mixed closes
        if who == ADDR_A:
            return [
                {"realizedPnl": 10, "timestamp": now_ts},
                {"realizedPnl": -5, "timestamp": now_ts},
            ]

        # B has closes -> [REC] path
        if who == ADDR_B:
            return [
                {"realizedPnl": 7, "timestamp": now_ts},
                {"realizedPnl": 3, "timestamp": now_ts},
            ]

        # C has no closes
        return []


def _patch_client(monkeypatch, client: FakePolymarketClient):
    def _make_pm_client(**kwargs):
        return client

    monkeypatch.setattr(analysis, "_make_pm_client", _make_pm_client)


def test_event_market_fallback_resolves_condition_id(monkeypatch):
    client = FakePolymarketClient()
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,  # avoid gating noise in this unit test
            use_cache=True,
        )
    )

    assert result["market"]["conditionId"] == "0xCOND"
    assert client.calls["get_market_by_slug"] == 1  # fallback actually happened
    assert client.calls["gamma_market"] == 1


def test_unknown_pnl_dropped_when_min_profit_positive(monkeypatch):
    client = FakePolymarketClient()
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=5000,
            min_qualified_wallets=1,
            use_cache=True,
        )
    )

    addrs = [r["addr"] for r in result.get("rows", [])]
    assert ADDR_B not in addrs  # unknown pnl should be dropped
    drops = (result.get("diagnostics") or {}).get("drop_reasons") or {}
    assert drops.get("pnl_unknown", 0) >= 1


def test_unknown_pnl_allowed_when_min_profit_zero_and_cli_formats_rec(monkeypatch):
    client = FakePolymarketClient()
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            use_cache=True,
        )
    )

    rows = result.get("rows") or []
    row_b = next(r for r in rows if r["addr"] == ADDR_B)

    # analysis marks unknown pnl explicitly
    assert row_b["pnl_src"] == "UNK"
    assert row_b["pnl_all_known"] is False

    # CLI shows [REC] (sum of scanned closes) and never prints fake "≥0"
    pnl_cell = cli._format_pnl_cell(row_b, debug=False)
    assert pnl_cell.endswith("[REC]")
    assert "≥" not in pnl_cell


def test_market_expired_gate_short_circuits_holders(monkeypatch):
    client = FakePolymarketClient()
    client._expired_mode = True
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            use_cache=True,
        )
    )

    diag = result.get("diagnostics") or {}
    assert diag.get("gate") == "market_expired"
    assert client.calls["get_holders"] == 0  # critical: no expensive holder scanning


# ----------------------------
# NEW: extra “holistic” gates
# ----------------------------

def test_market_closed_gate_short_circuits_holders(monkeypatch):
    client = FakePolymarketClient()
    client._closed_mode = True
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            use_cache=True,
        )
    )

    diag = result.get("diagnostics") or {}
    assert diag.get("gate") == "market_closed"
    assert client.calls["get_holders"] == 0


def test_market_inactive_gate_short_circuits_holders(monkeypatch):
    client = FakePolymarketClient()
    client._inactive_mode = True
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            use_cache=True,
        )
    )

    diag = result.get("diagnostics") or {}
    assert diag.get("gate") == "market_inactive"
    assert client.calls["get_holders"] == 0


# ----------------------------
# NEW: determinism + whale gate
# ----------------------------

def test_deterministic_result_independent_of_holders_order(monkeypatch):
    # Make weight deterministic + simple (avoid depending on scoring internals)
    monkeypatch.setattr(analysis, "wallet_weight", lambda feats: float(getattr(feats, "pnl_all", 0.0)))

    client_a = FakePolymarketClient()
    client_a._reverse_holders = False
    _patch_client(monkeypatch, client_a)

    r1 = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            whale_threshold=0.99,         # avoid whale gate; we’re testing determinism
            consensus_threshold=0.50,
            use_cache=True,
        )
    )

    client_b = FakePolymarketClient()
    client_b._reverse_holders = True
    monkeypatch.setattr(analysis, "_make_pm_client", lambda **kwargs: client_b)

    r2 = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            whale_threshold=0.99,
            consensus_threshold=0.50,
            use_cache=True,
        )
    )

    assert r1["recommendation"] == r2["recommendation"]
    assert r1.get("dist") == r2.get("dist")


def test_whale_dominance_gate_triggers(monkeypatch):
    # Deterministic weights: use pnl_all as weight so ADDR_C dominates (20k vs 10k)
    monkeypatch.setattr(analysis, "wallet_weight", lambda feats: float(getattr(feats, "pnl_all", 0.0)))

    client = FakePolymarketClient()
    _patch_client(monkeypatch, client)

    result = asyncio.run(
        analysis.analyze_market(
            market_url_or_slug="https://polymarket.com/event/test-event",
            market_index=0,
            min_profit=0,
            min_qualified_wallets=1,
            whale_threshold=0.55,  # default-ish behavior: 20k/(20k+10k)=0.666 -> triggers
            consensus_threshold=0.50,
            use_cache=True,
        )
    )

    diag = result.get("diagnostics") or {}
    assert result["recommendation"] == "STAY OUT"
    assert isinstance(diag.get("gate"), str) and diag["gate"].startswith("whale_dominance")

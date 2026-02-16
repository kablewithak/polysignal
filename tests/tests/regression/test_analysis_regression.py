from __future__ import annotations

import asyncio
import time

import polysignal.analysis as analysis
from tests.regression.fake_pm import FakePolymarketClient


def _patch_make_client(monkeypatch, fake: FakePolymarketClient) -> None:
    # analyze_market() calls _make_pm_client(...). We intercept that to prevent real HTTP.
    monkeypatch.setattr(analysis, "_make_pm_client", lambda *args, **kwargs: fake)


def test_event_needs_selection(monkeypatch):
    fake = FakePolymarketClient(
        markets_by_slug={},
        events_by_slug={
            "my-event": {
                "title": "My Event",
                "slug": "my-event",
                "id": 123,
                "markets": [
                    {"question": "M1?", "slug": "m1", "conditionId": "0xcond1"},
                    {"question": "M2?", "slug": "m2", "conditionId": "0xcond2"},
                ],
            }
        },
        gamma_by_condition={},
        holders_by_condition={},
        leaderboard_by_user={},
        positions_by_user_market={},
        closed_by_user={},
    )
    _patch_make_client(monkeypatch, fake)

    result = asyncio.run(analysis.analyze_market("https://polymarket.com/event/my-event", min_profit=0))

    assert result.get("needs_selection") is True
    assert result["event"]["slug"] == "my-event"
    assert len(result.get("event_markets") or []) == 2


def test_market_expired_gates_before_holders(monkeypatch):
    cond = "0xexpired"
    fake = FakePolymarketClient(
        markets_by_slug={
            "expired-market": {"question": "Expired?", "slug": "expired-market", "conditionId": cond}
        },
        events_by_slug={},
        gamma_by_condition={
            cond: {
                "outcomes": '["Yes","No"]',
                "outcomePrices": "[50,50]",
                "active": True,
                "closed": False,
                "endDate": "2000-01-01T00:00:00Z",  # definitely in the past
            }
        },
        holders_by_condition={
            cond: [{"holders": [{"proxyWallet": "0x" + "1" * 40}]}]
        },
        leaderboard_by_user={},
        positions_by_user_market={},
        closed_by_user={},
    )
    _patch_make_client(monkeypatch, fake)

    result = asyncio.run(analysis.analyze_market("https://polymarket.com/market/expired-market", min_profit=0))

    assert result["recommendation"] == "STAY OUT"
    assert (result.get("diagnostics") or {}).get("gate") == "market_expired"
    # this is the key invariant: expiry gate should prevent holders scanning
    assert fake.calls["get_holders"] == 0


def test_whale_dominance_gate(monkeypatch):
    cond = "0xwhale"
    w1 = "0x" + "a" * 40  # whale
    w2 = "0x" + "b" * 40
    w3 = "0x" + "c" * 40

    fake = FakePolymarketClient(
        markets_by_slug={"whale-market": {"question": "Whale?", "slug": "whale-market", "conditionId": cond}},
        events_by_slug={},
        gamma_by_condition={
            cond: {"outcomes": '["Yes","No"]', "outcomePrices": "[10,90]", "active": True, "closed": False}
        },
        holders_by_condition={
            cond: [{"holders": [{"proxyWallet": w1}, {"proxyWallet": w2}, {"proxyWallet": w3}]}]
        },
        leaderboard_by_user={
            w1: {"pnl": 50000},  # pnl_score caps at 5 in your scoring -> creates dominance vs 1/1
            w2: {"pnl": 0},
            w3: {"pnl": 0},
        },
        positions_by_user_market={
            (w1, cond): [{"outcome": "No", "currentValue": 1000}],
            (w2, cond): [{"outcome": "No", "currentValue": 1000}],
            (w3, cond): [{"outcome": "No", "currentValue": 1000}],
        },
        closed_by_user={
            w1: [],
            w2: [],
            w3: [],
        },
    )
    _patch_make_client(monkeypatch, fake)

    result = asyncio.run(
        analysis.analyze_market(
            "https://polymarket.com/market/whale-market",
            min_profit=0,
            holders_limit=20,
            min_qualified_wallets=3,
            whale_threshold=0.60,  # should trigger
            consensus_threshold=0.60,
        )
    )

    assert result["recommendation"] == "STAY OUT"
    gate = (result.get("diagnostics") or {}).get("gate") or ""
    assert gate.startswith("whale_dominance")

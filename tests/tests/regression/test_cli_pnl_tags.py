from __future__ import annotations

from rich.console import Console

import polysignal.cli as cli


def _render(result: dict, *, debug: bool) -> str:
    c = Console(record=True, width=160)
    cli.console = c  # monkeypatch without fixture
    cli._print_single_result(result, debug=debug)
    return c.export_text()


def test_cli_pnl_tags_wallet_row_only_in_debug():
    addr = "0x" + "1" * 40

    # Provide many aliases so the fixture matches whichever key the CLI is using today.
    row = {
        "addr": addr,
        "wallet": addr,

        # numeric variants
        "pnl_all": 12345.0,
        "pnl": 12345.0,
        "profit": 12345.0,
        "pnl_usd": 12345.0,
        "pnl_value": 12345.0,

        # display-string variants (some CLIs use these)
        "pnl_display": "12,345",
        "pnl_str": "12,345",
        "pnl_fmt": "12,345",

        # tag/source variants
        "pnl_source": "LB",
        "pnl_tag": "LB",

        "outcome": "No",
        "market_value": 1000.0,
        "win_rate": 0.5,
        "wr_n": 10,
        "closed_scanned": 10,
        "days_since_active": 1.0,
        "conviction_ratio": 1.2,
        "weight": 10.0,
    }

    result = {
        "market": {"question": "Q?", "slug": "s", "conditionId": "0xcond"},
        "recommendation": "BUY No",
        "confidence": 9.7,
        "n_wallets_qualified": 1,
        "n_wallets_considered": 1,
        "dist": {"No": 1.0},
        "rows": [row],
        "diagnostics": {"top_wallet_share": 0.9, "top_outcome": "No", "top_outcome_share": 1.0, "drop_reasons": {}},
    }

    out_non_debug = _render(result, debug=False)

    # Legend may appear even in non-debug runs (your current CLI behavior).
    assert "PnL tags:" in out_non_debug

    # Non-debug should NOT append the tag to the wallet row value.
    assert "12,345 [LB]" not in out_non_debug

    out_debug = _render(result, debug=True)

    # Debug SHOULD append the tag to the wallet row value.
    assert "12,345 [LB]" in out_debug

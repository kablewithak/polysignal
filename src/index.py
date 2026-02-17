from __future__ import annotations

import os
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

from polysignal.analysis import analyze_market

# bump this string if you ever want to confirm which version is deployed
APP_REV = "vercel-fastapi-rev-003"

app = FastAPI(
    title="Polysignal",
    version="0.1",
    description="Web wrapper around the Polysignal CLI analysis pipeline.",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


async def _run_analysis(
    *,
    url: str,
    market_index: Optional[int],
    all_markets: bool,
    min_profit: float,
    holders_limit: int,
    min_balance: float,
    max_closed: int,
    closed_page_size: int,
    consensus_threshold: float,
    whale_threshold: float,
    min_qualified_wallets: int,
    concurrency: int,
    timeout_s: float,
    debug: bool,
) -> Dict[str, Any]:
    cache_dir = os.getenv("POLYSIGNAL_CACHE_DIR", "/tmp/polysignal-cache")
    use_cache = _bool_env("POLYSIGNAL_USE_CACHE", True)
    clear_cache = _bool_env("POLYSIGNAL_CLEAR_CACHE", False)

    return await analyze_market(
        market_url_or_slug=url,
        market_index=market_index,
        all_markets_in_event=all_markets,
        min_profit=min_profit,
        holders_limit=holders_limit,
        min_balance=min_balance,
        max_closed=max_closed,
        closed_page_size=closed_page_size,
        consensus_threshold=consensus_threshold,
        whale_threshold=whale_threshold,
        min_qualified_wallets=min_qualified_wallets,
        concurrency=concurrency,
        timeout_s=timeout_s,
        debug=debug,
        use_cache=use_cache,
        cache_dir=cache_dir,
        clear_cache=clear_cache,
        ttl_gamma_s=300,
        ttl_data_s=300,
    )


def _format_implied(market: Dict[str, Any]) -> str:
    """
    market_probs sometimes comes back as:
      - dict: {"Yes": 0.42, "No": 0.58}
      - list: [0.42, 0.58] with market["outcomes"] = ["Yes","No"]
      - list[dict]: [{"outcome":"Yes","prob":0.42}, ...]
    """
    probs = market.get("market_probs")
    if probs is None:
        # fallback: some responses carry outcomePrices
        outcomes = market.get("outcomes")
        outcome_prices = market.get("outcomePrices")
        if isinstance(outcomes, list) and isinstance(outcome_prices, list) and len(outcomes) == len(outcome_prices):
            try:
                return " | ".join(f"{o}: {float(p):.2f}" for o, p in zip(outcomes, outcome_prices))
            except Exception:
                return "-"
        return "-"

    # dict case
    if isinstance(probs, dict):
        try:
            return " | ".join(f"{k}: {float(v):.2f}" for k, v in probs.items())
        except Exception:
            return str(probs)

    # list case
    if isinstance(probs, list):
        outcomes = market.get("outcomes")

        # list[float] + outcomes
        if isinstance(outcomes, list) and len(outcomes) == len(probs):
            try:
                return " | ".join(f"{o}: {float(p):.2f}" for o, p in zip(outcomes, probs))
            except Exception:
                pass

        # list[dict] case
        if probs and isinstance(probs[0], dict):
            parts: List[str] = []
            for d in probs:
                if not isinstance(d, dict):
                    continue
                o = d.get("outcome") or d.get("label") or d.get("name")
                p = d.get("prob") or d.get("p") or d.get("value") or d.get("price")
                if o is None or p is None:
                    continue
                try:
                    parts.append(f"{o}: {float(p):.2f}")
                except Exception:
                    continue
            if parts:
                return " | ".join(parts)

        return str(probs)

    return str(probs)


def _format_cli_like(result: Dict[str, Any], top_n: int = 10) -> str:
    # event selection
    if result.get("needs_selection"):
        ev = result.get("event", {}) or {}
        lines: List[str] = []
        lines.append(f"EVENT: {ev.get('title') or '-'}")
        lines.append("")
        lines.append("Markets in this event (pick one):")
        for m in result.get("event_markets") or []:
            lines.append(f"  [{m.get('index')}] {m.get('question')}  (slug: {m.get('slug')})")
        lines.append("")
        lines.append("Re-run with market_index=<N> (or all=true).")
        return "\n".join(lines)

    market = result.get("market", {}) or {}
    diag = result.get("diagnostics", {}) or {}
    dist = result.get("dist") or {}
    rows = result.get("rows") or []

    lines: List[str] = []
    lines.append("Polysignal")
    lines.append(f"Question: {market.get('question') or '-'}")
    lines.append(f"Market implied: {_format_implied(market)}")
    lines.append("")
    lines.append(
        f"Recommendation: {result.get('recommendation')} (confidence {float(result.get('confidence') or 0.0):.1f}/10)"
    )
    lines.append(
        f"Qualified wallets: {result.get('n_wallets_qualified', 0)} / considered: {result.get('n_wallets_considered', 0)}"
    )

    if isinstance(diag, dict) and diag.get("gate"):
        lines.append(f"Gate: {diag.get('gate')}")

    if dist:
        lines.append("")
        lines.append("Smart-money weighted stance:")
        for k, v in sorted(dist.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {k}: {float(v) * 100:.2f}%")

    if rows:
        lines.append("")
        lines.append(f"Top wallets (top {min(top_n, len(rows))} by weight)")

        def g(r: Dict[str, Any], *keys: str, default=None):
            for k in keys:
                if k in r and r[k] is not None:
                    return r[k]
            return default

        # Keep widths reasonable but include the columns you want (when present)
        lines.append(
            "addr                               pnl_all   src  outcome  mkt_value  win%  n   closed  days  conv   weight"
        )
        lines.append("-" * 120)

        for r0 in rows[:top_n]:
            r = r0 or {}

            addr = str(g(r, "addr", "wallet", default=""))[:34].ljust(34)

            outcome = str(g(r, "outcome", "side", default="-"))[:7].ljust(7)

            # PnL display: only show numeric if we know it's safe/known.
            pnl_all_known = bool(g(r, "pnl_all_known", default=False))
            pnl_all_val = g(r, "pnl_all", "pnl", "pnl_all_time", default=None)
            if pnl_all_known and pnl_all_val is not None:
                try:
                    pnl_all = f"{float(pnl_all_val):,.0f}".rjust(8)
                except Exception:
                    pnl_all = "   —   ".rjust(8)
            else:
                pnl_all = "   —   ".rjust(8)

            pnl_src = str(g(r, "pnl_src", default="UNK")).rjust(4)

            mkt_value = f"{float(g(r, 'market_value', 'mkt_value', default=0.0)):.0f}".rjust(8)

            win_rate = g(r, "win_rate", "wr", default=None)
            if win_rate is not None:
                try:
                    win_s = f"{float(win_rate) * 100:.0f}%".rjust(4)
                except Exception:
                    win_s = "  - ".rjust(4)
            else:
                win_s = "  - ".rjust(4)

            wr_n = g(r, "wr_n", "win_n", default=None)
            wr_n_s = (f"{int(wr_n)}".rjust(3) if wr_n is not None else "  -")

            closed = g(r, "closed_scanned", "closed", default=None)
            closed_s = (f"{int(closed)}".rjust(6) if closed is not None else "     -")

            days = g(r, "days_since_active", "days", default=None)
            days_s = (f"{float(days):.0f}".rjust(4) if days is not None else "   -")

            conv = g(r, "conviction_ratio", "conv", default=None)
            if conv is not None:
                try:
                    conv_s = f"{float(conv):.2f}".rjust(5)
                except Exception:
                    conv_s = "  -  ".rjust(5)
            else:
                conv_s = "  -  ".rjust(5)

            weight = f"{float(g(r, 'weight', default=0.0)):.4f}".rjust(7)

            lines.append(
                f"{addr} {pnl_all} {pnl_src}  {outcome} {mkt_value}  {win_s} {wr_n_s} {closed_s} {days_s} {conv_s} {weight}"
            )

        lines.append("")
        lines.append("PnL tags: [LB]=leaderboard all-time, [REC]=sum scanned closes, UNK=unknown")

    lines.append("")
    lines.append("Tip: /docs for API docs. Use debug=true to see full error messages.")
    return "\n".join(lines)


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Polysignal</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 32px; }
      input { width: 720px; padding: 10px; }
      button { padding: 10px 14px; margin-left: 8px; }
      pre { margin-top: 18px; padding: 14px; background: #111; color: #eee; overflow-x: auto; }
      .row { margin-top: 10px; }
      a { color: #2563eb; }
      .small { width: 160px; }
    </style>
  </head>
  <body>
    <h1>Polysignal</h1>
    <p>Paste a Polymarket <b>event</b> or <b>market</b> URL.</p>

    <div class="row">
      <input id="url" placeholder="https://polymarket.com/event/..." />
      <button onclick="run()">Run</button>
    </div>

    <div class="row">
      <label>market_index (optional): <input id="idx" class="small" placeholder="0"></label>
      <label style="margin-left:16px;">min_profit: <input id="minp" class="small" value="5000"></label>
      <label style="margin-left:16px;">debug: <input id="dbg" type="checkbox"></label>
    </div>

    <p class="row">
      <a href="/docs">/docs</a> • <a href="/api/health">/api/health</a>
    </p>

    <pre id="out">Output will appear here…</pre>

    <script>
      async function run() {
        const url = document.getElementById("url").value.trim();
        const idx = document.getElementById("idx").value.trim();
        const minp = document.getElementById("minp").value.trim();
        const dbg = document.getElementById("dbg").checked;

        if (!url) {
          document.getElementById("out").textContent = "Please paste a URL.";
          return;
        }

        const params = new URLSearchParams();
        params.set("url", url);
        if (idx) params.set("market_index", idx);
        if (minp) params.set("min_profit", minp);
        if (dbg) params.set("debug", "true");

        document.getElementById("out").textContent = "Running…";
        const res = await fetch("/api/cli?" + params.toString());
        const txt = await res.text();
        document.getElementById("out").textContent = txt;
      }
    </script>
  </body>
</html>
"""


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "polysignal", "rev": APP_REV}


@app.get("/api/analyze")
async def analyze(
    url: str = Query(...),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),
    min_profit: float = Query(5000.0, ge=0.0),
    holders_limit: int = Query(20, ge=1, le=200),
    min_balance: float = Query(0.0, ge=0.0),
    max_closed: int = Query(500, ge=0, le=5000),
    closed_page_size: int = Query(50, ge=1, le=500),
    consensus_threshold: float = Query(0.60, ge=0.0, le=1.0),
    whale_threshold: float = Query(0.55, ge=0.0, le=1.0),
    min_qualified_wallets: int = Query(3, ge=0, le=200),
    concurrency: int = Query(8, ge=1, le=50),
    timeout_s: float = Query(25.0, ge=1.0, le=120.0),
    debug: bool = Query(False),
) -> Any:
    try:
        return await _run_analysis(
            url=url,
            market_index=market_index,
            all_markets=bool(all),
            min_profit=float(min_profit),
            holders_limit=int(holders_limit),
            min_balance=float(min_balance),
            max_closed=int(max_closed),
            closed_page_size=int(closed_page_size),
            consensus_threshold=float(consensus_threshold),
            whale_threshold=float(whale_threshold),
            min_qualified_wallets=int(min_qualified_wallets),
            concurrency=int(concurrency),
            timeout_s=float(timeout_s),
            debug=bool(debug),
        )
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"detail": (str(e) if debug else "Internal error")}, status_code=500)


@app.get("/api/cli", response_class=PlainTextResponse)
async def cli(
    url: str = Query(...),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),
    min_profit: float = Query(5000.0, ge=0.0),
    holders_limit: int = Query(20, ge=1, le=200),
    min_balance: float = Query(0.0, ge=0.0),
    max_closed: int = Query(500, ge=0, le=5000),
    closed_page_size: int = Query(50, ge=1, le=500),
    consensus_threshold: float = Query(0.60, ge=0.0, le=1.0),
    whale_threshold: float = Query(0.55, ge=0.0, le=1.0),
    min_qualified_wallets: int = Query(3, ge=0, le=200),
    concurrency: int = Query(8, ge=1, le=50),
    timeout_s: float = Query(25.0, ge=1.0, le=120.0),
    debug: bool = Query(False),
) -> Any:
    try:
        result = await _run_analysis(
            url=url,
            market_index=market_index,
            all_markets=bool(all),
            min_profit=float(min_profit),
            holders_limit=int(holders_limit),
            min_balance=float(min_balance),
            max_closed=int(max_closed),
            closed_page_size=int(closed_page_size),
            consensus_threshold=float(consensus_threshold),
            whale_threshold=float(whale_threshold),
            min_qualified_wallets=int(min_qualified_wallets),
            concurrency=int(concurrency),
            timeout_s=float(timeout_s),
            debug=bool(debug),
        )
        return _format_cli_like(result)
    except ValueError as e:
        return PlainTextResponse(f"ERROR (400): {e}", status_code=400)
    except Exception as e:
        msg = str(e) if debug else "Internal error"
        return PlainTextResponse(f"ERROR (500): {msg}", status_code=500)

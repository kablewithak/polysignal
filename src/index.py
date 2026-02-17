from __future__ import annotations

import os
import html
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

from polysignal.analysis import analyze_market

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


def _format_cli_like(result: Dict[str, Any], top_n: int = 10) -> str:
    # Handle gates / early-exit shapes
    if result.get("all_markets") is True:
        event = result.get("event", {}) or {}
        lines = [
            "Polysignal (Event: all markets)",
            f"Event: {event.get('title') or '-'}",
            "",
        ]
        for i, r in enumerate(result.get("results", []) or []):
            m = (r or {}).get("market", {}) or {}
            lines.append(f"[{i}] {m.get('question') or m.get('slug') or '-'}")
            lines.append(f"  Recommendation: {r.get('recommendation')}  (confidence {r.get('confidence')}/10)")
            dist = r.get("dist") or {}
            if dist:
                lines.append(f"  Weighted stance: {dist}")
            lines.append("")
        return "\n".join(lines)

    market = result.get("market", {}) or {}
    rec = result.get("recommendation")
    conf = result.get("confidence", 0.0)
    dist = result.get("dist") or {}
    rows = result.get("rows") or []
    diag = result.get("diagnostics") or {}

    probs = market.get("market_probs") or {}
    implied = " | ".join([f"{k}: {float(v):.2f}" for k, v in probs.items()]) if probs else "-"

    lines: list[str] = []
    lines.append("Polysignal")
    lines.append(f"Question: {market.get('question') or '-'}")
    lines.append(f"Market implied: {implied}")
    lines.append("")
    lines.append(f"Recommendation: {rec}  (confidence {float(conf):.1f}/10)")
    lines.append(f"Qualified wallets: {result.get('n_wallets_qualified', 0)} / considered: {result.get('n_wallets_considered', 0)}")

    gate = diag.get("gate")
    if gate:
        lines.append(f"Gate: {gate}")

    if dist:
        lines.append("")
        lines.append("Smart-money weighted stance:")
        for k, v in sorted(dist.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {k}: {float(v)*100:.2f}%")

    # simple table
    if rows:
        lines.append("")
        lines.append(f"Top wallets (top {min(top_n, len(rows))} by weight)")
        lines.append("addr                               outcome  weight   pnl(all)  pnl_src  mkt_value  win%  n  days  conv")
        lines.append("-" * 110)
        for r in rows[:top_n]:
            addr = str(r.get("addr", ""))[:34].ljust(34)
            outcome = str(r.get("outcome", "-"))[:7].ljust(7)
            weight = f"{float(r.get('weight', 0.0)):.4f}".rjust(7)

            pnl_src = str(r.get("pnl_src", "UNK"))
            pnl_all_known = bool(r.get("pnl_all_known", False))
            if pnl_all_known:
                pnl_all = f"{float(r.get('pnl_all', 0.0)):.0f}".rjust(8)
            else:
                # stable: do NOT imply >=0, show unknown
                pnl_all = "   —   ".rjust(8)
                pnl_src = "UNK"

            mkt_value = f"{float(r.get('market_value', 0.0)):.0f}".rjust(8)
            win = f"{float(r.get('win_rate', 0.0))*100:.0f}%".rjust(4)
            n = f"{int(r.get('wr_n', 0))}".rjust(3)
            days = f"{float(r.get('days_since_active', 9999)):.0f}".rjust(4)
            conv = f"{float(r.get('conviction_ratio', 0.0)):.2f}".rjust(5)

            lines.append(f"{addr}  {outcome}  {weight}  {pnl_all}  {pnl_src:>6}  {mkt_value}  {win}  {n}  {days}  {conv}")

    lines.append("")
    lines.append("Tip: open /docs for the interactive API docs.")
    return "\n".join(lines)


@app.get("/", response_class=HTMLResponse)
async def home() -> str:
    # Minimal UI: submit a Polymarket URL and show CLI-like output
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
      <label>market_index (optional): <input id="idx" style="width:80px" placeholder="0"></label>
      <label style="margin-left:16px;">min_profit: <input id="minp" style="width:120px" value="5000"></label>
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
        if (!url) {
          document.getElementById("out").textContent = "Please paste a URL.";
          return;
        }
        const params = new URLSearchParams();
        params.set("url", url);
        if (idx) params.set("market_index", idx);
        if (minp) params.set("min_profit", minp);

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
    return {"ok": True, "service": "polysignal"}


@app.get("/api/analyze")
async def analyze(
    url: str = Query(..., description="Polymarket event or market URL"),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False, description="Analyze all markets in an event (slow)"),
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
) -> Dict[str, Any]:
    cache_dir = os.getenv("POLYSIGNAL_CACHE_DIR", "/tmp/polysignal-cache")
    use_cache = _bool_env("POLYSIGNAL_USE_CACHE", True)
    clear_cache = _bool_env("POLYSIGNAL_CLEAR_CACHE", False)

    try:
        return await analyze_market(
            market_url_or_slug=url,
            market_index=market_index,
            all_markets_in_event=all,
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        msg = str(e) if debug else "Internal error"
        raise HTTPException(status_code=500, detail=msg) from e


@app.get("/api/cli", response_class=PlainTextResponse)
async def cli(
    url: str = Query(...),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),
    min_profit: float = Query(5000.0, ge=0.0),
    debug: bool = Query(False),
) -> str:
    result = await analyze(
        url=url,
        market_index=market_index,
        all=all,
        min_profit=min_profit,
        debug=debug,
    )
    return _format_cli_like(result)

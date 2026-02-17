from __future__ import annotations

import os
from typing import Any, Dict, Optional, List

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
    # Event-selection case
    if result.get("needs_selection"):
        ev = result.get("event", {}) or {}
        lines: List[str] = []
        lines.append(f"EVENT: {ev.get('title') or '-'}")
        lines.append(f"slug={ev.get('slug') or '-'} id={ev.get('id') or '-'}")
        lines.append("")
        lines.append("Markets in this event (pick one):")
        for m in result.get("event_markets") or []:
            lines.append(f"  [{m.get('index')}] {m.get('question')}  (slug: {m.get('slug')})")
        lines.append("")
        lines.append("Re-run with market_index=<N> (or all=true).")
        return "\n".join(lines)

    # All-markets case
    if result.get("all_markets") is True:
        ev = result.get("event", {}) or {}
        chunks: List[str] = []
        chunks.append(f"EVENT (ALL MARKETS): {ev.get('title') or '-'}  slug={ev.get('slug') or '-'}")
        chunks.append("")
        for i, r in enumerate(result.get("results") or []):
            m = (r or {}).get("market", {}) or {}
            chunks.append(f"=== Market #{i} ===")
            chunks.append(f"Question: {m.get('question') or m.get('slug') or '-'}")
            chunks.append(f"Recommendation: {r.get('recommendation')} (confidence {float(r.get('confidence') or 0.0):.1f}/10)")
            dist = r.get("dist") or {}
            if dist:
                chunks.append(f"Weighted stance: {dist}")
            chunks.append("")
        return "\n".join(chunks).rstrip()

    market = result.get("market", {}) or {}
    rec = result.get("recommendation")
    conf = float(result.get("confidence") or 0.0)
    dist = result.get("dist") or {}
    rows = result.get("rows") or []
    diag = result.get("diagnostics") or {}

    probs = market.get("market_probs") or {}
    implied = " | ".join([f"{k}: {float(v):.2f}" for k, v in probs.items()]) if probs else "-"

    lines: List[str] = []
    lines.append("Polysignal")
    lines.append(f"Question: {market.get('question') or '-'}")
    lines.append(f"Market implied: {implied}")
    lines.append("")
    lines.append(f"Recommendation: {rec} (confidence {conf:.1f}/10)")
    lines.append(f"Qualified wallets: {result.get('n_wallets_qualified', 0)} / considered: {result.get('n_wallets_considered', 0)}")

    gate = diag.get("gate") if isinstance(diag, dict) else None
    if gate:
        lines.append(f"Gate: {gate}")

    if dist:
        lines.append("")
        lines.append("Smart-money weighted stance:")
        for k, v in sorted(dist.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {k}: {float(v) * 100:.2f}%")

    if rows:
        lines.append("")
        lines.append(f"Top wallets (top {min(top_n, len(rows))} by weight)")
        lines.append("addr                               outcome  weight   mkt_value")
        lines.append("-" * 78)
        for r in rows[:top_n]:
            addr = str((r or {}).get("addr", ""))[:34].ljust(34)
            outcome = str((r or {}).get("outcome", "-"))[:7].ljust(7)
            weight = f"{float((r or {}).get('weight', 0.0)):.4f}".rjust(7)
            mkt_value = f"{float((r or {}).get('market_value', 0.0)):.0f}".rjust(8)
            lines.append(f"{addr}  {outcome}  {weight}  {mkt_value}")

    lines.append("")
    lines.append("Tip: open /docs for interactive API docs.")
    return "\n".join(lines)


# -------------------------
# IMPORTANT: internal runner
# -------------------------
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
    try:
        return await _run_analysis(
            url=url,
            market_index=market_index,
            all_markets=all,
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
) -> str:
    try:
        result = await _run_analysis(
            url=url,
            market_index=market_index,
            all_markets=all,
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
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        msg = str(e) if debug else "Internal error"
        raise HTTPException(status_code=500, detail=msg) from e

from __future__ import annotations

import html
import os
import sys
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

# Ensure src/ is importable for "src layout" packages (src/polysignal/*)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from polysignal.analysis import analyze_market  # noqa: E402


app = FastAPI(
    title="Polysignal",
    version="0.1",
    description="HTTP wrapper + lightweight UI for Polysignal.",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _short_addr(addr: str) -> str:
    if not addr:
        return ""
    if len(addr) <= 12:
        return addr
    return f"{addr[:6]}…{addr[-4:]}"


def _fmt_money(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return "—"
    # compact-ish display
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.2f}K"
    return f"{v:.2f}"


def _fmt_pct(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return "—"
    return f"{v*100:.2f}%"


def _pnl_cell(r: Dict[str, Any]) -> str:
    # Stable PnL display for web:
    # - If all-time known: show pnl_all + [LB]
    # - Else if we have realized pnl from scanned closes: show pnl_recent + [REC]
    # - Else: —
    if bool(r.get("pnl_all_known")):
        return f"{_fmt_money(r.get('pnl_all'))} [LB]"
    n_recent = int(r.get("pnl_recent_n") or 0)
    if n_recent > 0:
        return f"{_fmt_money(r.get('pnl_recent'))} [REC]"
    return "—"


def _format_cli_like_text(result: Dict[str, Any]) -> str:
    # Selection result
    if result.get("needs_selection"):
        event = result.get("event") or {}
        lines = []
        lines.append("This is an EVENT with multiple markets. Pick a market_index and re-run.")
        lines.append(f"Event: {event.get('title') or ''}  (slug: {event.get('slug') or ''})")
        lines.append("")
        lines.append("Markets:")
        for m in result.get("event_markets") or []:
            idx = m.get("index")
            q = m.get("question") or ""
            slug = m.get("slug") or ""
            lines.append(f"  [{idx}] {q}  (slug: {slug})")
        lines.append("")
        lines.append("Tip: use /api/ui to click a market index.")
        return "\n".join(lines)

    market = result.get("market") or {}
    diag = result.get("diagnostics") or {}

    question = market.get("question") or ""
    probs = market.get("market_probs") or {}
    outcomes = market.get("outcomes") or []

    lines = []
    lines.append(f"Market: {question}")

    if isinstance(outcomes, list) and outcomes:
        implied_parts = []
        for o in outcomes:
            p = probs.get(o)
            if p is None:
                continue
            implied_parts.append(f"{o}: {float(p):.3f}")
        if implied_parts:
            lines.append("Market implied: " + " | ".join(implied_parts))

    rec = result.get("recommendation") or "STAY OUT"
    conf = float(result.get("confidence") or 0.0)
    lines.append(f"Recommendation: {rec} (confidence {conf:.1f}/10)")

    dist = result.get("dist") or {}
    if isinstance(dist, dict) and dist:
        lines.append("")
        lines.append("Smart-money weighted stance:")
        for k, v in dist.items():
            lines.append(f"  {k}: {_fmt_pct(v)}")

    n_q = int(result.get("n_wallets_qualified") or 0)
    n_c = int(result.get("n_wallets_considered") or 0)
    lines.append("")
    lines.append(f"Qualified wallets: {n_q} / holders scanned: {n_c}")

    if diag.get("top_wallet_share") is not None:
        lines.append(f"Top wallet share: {_fmt_pct(diag.get('top_wallet_share'))}")
    if diag.get("gate"):
        lines.append(f"Gate: {diag.get('gate')}")
    if diag.get("drop_reasons"):
        # only show drops if they exist and matter
        drops = diag.get("drop_reasons") or {}
        if isinstance(drops, dict) and any(int(v or 0) > 0 for v in drops.values()):
            # show a compact summary
            parts = [f"{k}={int(v)}" for k, v in drops.items() if int(v or 0) > 0]
            if parts:
                lines.append("Drop reasons: " + ", ".join(parts))

    rows = result.get("rows") or []
    if isinstance(rows, list) and rows:
        lines.append("")
        lines.append("Top wallets (ranked by weight):")
        header = (
            f"{'Wallet':<14}  {'Out':<3}  {'Weight':>8}  {'Value':>10}  "
            f"{'PnL':>14}  {'Win%':>7}  {'n':>4}  {'Days':>6}  {'Conv':>6}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for r in rows[:15]:
            addr = _short_addr(str(r.get("addr") or ""))
            outcome = str(r.get("outcome") or "")[:3]
            weight = _fmt_pct(r.get("weight"))
            value = _fmt_money(r.get("market_value"))
            pnl = _pnl_cell(r)
            win = r.get("win_rate")
            win_s = "—" if win is None else f"{float(win)*100:.0f}%"
            wr_n = int(r.get("wr_n") or 0)
            days = r.get("days_since_active")
            days_s = "—" if days is None else f"{float(days):.1f}"
            conv = r.get("conviction_ratio")
            conv_s = "—" if conv is None else f"{float(conv):.2f}"

            lines.append(
                f"{addr:<14}  {outcome:<3}  {weight:>8}  {value:>10}  "
                f"{pnl:>14}  {win_s:>7}  {wr_n:>4}  {days_s:>6}  {conv_s:>6}"
            )

        lines.append("")
        lines.append("PnL tags: [LB]=leaderboard all-time, [REC]=sum of scanned closes, —=unknown")

    return "\n".join(lines)


def _ui_page(form_html: str, result_text: Optional[str] = None, error: Optional[str] = None) -> str:
    # Minimal, no-JS page
    out = []
    out.append("<!doctype html>")
    out.append("<html><head><meta charset='utf-8'/>")
    out.append("<meta name='viewport' content='width=device-width, initial-scale=1'/>")
    out.append("<title>Polysignal</title>")
    out.append(
        "<style>"
        "body{font-family:ui-sans-serif,system-ui,Segoe UI,Roboto,Arial;margin:24px;max-width:980px;}"
        "input,select{padding:10px;font-size:14px;width:100%;max-width:780px;}"
        "label{display:block;margin-top:12px;font-weight:600;}"
        "button{padding:10px 14px;font-size:14px;margin-top:14px;cursor:pointer;}"
        "pre{background:#0b0f14;color:#e6edf3;padding:14px;border-radius:10px;overflow:auto;}"
        ".hint{color:#57606a;font-size:13px;margin-top:6px;}"
        ".err{background:#ffebe9;color:#82071e;padding:10px;border-radius:10px;}"
        "</style>"
    )
    out.append("</head><body>")
    out.append("<h1>Polysignal</h1>")
    out.append("<p class='hint'>Paste a Polymarket event/market URL. You’ll get CLI-style output.</p>")
    if error:
        out.append(f"<div class='err'><b>Error:</b> {html.escape(error)}</div>")
    out.append(form_html)
    if result_text:
        out.append("<h2>Output</h2>")
        out.append("<pre>")
        out.append(html.escape(result_text))
        out.append("</pre>")
    out.append("<p class='hint'>API endpoints: <code>/api/health</code>, <code>/api/analyze</code> (JSON), <code>/api/cli</code> (text)</p>")
    out.append("</body></html>")
    return "".join(out)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "polysignal"}


@app.get("/", response_class=RedirectResponse)
async def api_root() -> RedirectResponse:
    # This is /api/ on Vercel. Domain root "/" is separate unless you add a rewrite.
    return RedirectResponse(url="/api/ui")


@app.get("/analyze")
async def analyze(
    url: str = Query(..., description="Polymarket event or market URL"),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),

    min_profit: float = Query(5000.0, ge=0.0),
    holders_limit: int = Query(20, ge=1, le=200),
    min_balance: float = Query(0.0, ge=0.0),

    consensus_threshold: float = Query(0.60, ge=0.0, le=1.0),
    whale_threshold: float = Query(0.55, ge=0.0, le=1.0),
    min_qualified_wallets: int = Query(3, ge=0, le=200),

    concurrency: int = Query(8, ge=1, le=50),
    timeout_s: float = Query(25.0, ge=1.0, le=120.0),

    debug: bool = Query(False),
) -> Dict[str, Any]:
    # Writable location on Vercel is /tmp
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


@app.get("/cli", response_class=PlainTextResponse)
async def cli(
    url: str = Query(..., description="Polymarket event or market URL"),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),
    min_profit: float = Query(5000.0, ge=0.0),
    holders_limit: int = Query(20, ge=1, le=200),
    min_balance: float = Query(0.0, ge=0.0),
    consensus_threshold: float = Query(0.60, ge=0.0, le=1.0),
    whale_threshold: float = Query(0.55, ge=0.0, le=1.0),
    min_qualified_wallets: int = Query(3, ge=0, le=200),
    concurrency: int = Query(8, ge=1, le=50),
    timeout_s: float = Query(25.0, ge=1.0, le=120.0),
    debug: bool = Query(False),
) -> str:
    data = await analyze(
        url=url,
        market_index=market_index,
        all=all,
        min_profit=min_profit,
        holders_limit=holders_limit,
        min_balance=min_balance,
        consensus_threshold=consensus_threshold,
        whale_threshold=whale_threshold,
        min_qualified_wallets=min_qualified_wallets,
        concurrency=concurrency,
        timeout_s=timeout_s,
        debug=debug,
    )
    return _format_cli_like_text(data)


@app.get("/ui", response_class=HTMLResponse)
async def ui(
    url: Optional[str] = Query(None),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),

    min_profit: float = Query(5000.0, ge=0.0),
    holders_limit: int = Query(20, ge=1, le=200),
    min_balance: float = Query(0.0, ge=0.0),

    consensus_threshold: float = Query(0.60, ge=0.0, le=1.0),
    whale_threshold: float = Query(0.55, ge=0.0, le=1.0),
    min_qualified_wallets: int = Query(3, ge=0, le=200),

    concurrency: int = Query(8, ge=1, le=50),
    timeout_s: float = Query(25.0, ge=1.0, le=120.0),

    debug: bool = Query(False),
) -> HTMLResponse:
    # Build the form (GET-based so it’s shareable)
    checked_all = "checked" if all else ""
    checked_dbg = "checked" if debug else ""

    form = []
    form.append("<form method='get' action='/api/ui'>")
    form.append("<label>Polymarket URL</label>")
    form.append(
        f"<input name='url' placeholder='https://polymarket.com/event/...' value='{html.escape(url or '')}' required/>"
    )

    form.append("<label>Market index (optional)</label>")
    form.append(
        f"<input name='market_index' type='number' min='0' placeholder='e.g. 2' value='{'' if market_index is None else market_index}'/>"
    )
    form.append("<div class='hint'>If you paste an event URL with multiple markets, pick an index.</div>")

    form.append("<label><input type='checkbox' name='all' value='true' " + checked_all + "/> Analyze all markets in event (slow)</label>")

    form.append("<label>min_profit</label>")
    form.append(f"<input name='min_profit' type='number' min='0' step='1' value='{min_profit}'/>")

    form.append("<label>holders_limit</label>")
    form.append(f"<input name='holders_limit' type='number' min='1' max='200' step='1' value='{holders_limit}'/>")

    form.append("<label>min_balance</label>")
    form.append(f"<input name='min_balance' type='number' min='0' step='0.01' value='{min_balance}'/>")

    form.append("<label>consensus_threshold</label>")
    form.append(f"<input name='consensus_threshold' type='number' min='0' max='1' step='0.01' value='{consensus_threshold}'/>")

    form.append("<label>whale_threshold</label>")
    form.append(f"<input name='whale_threshold' type='number' min='0' max='1' step='0.01' value='{whale_threshold}'/>")

    form.append("<label>min_qualified_wallets</label>")
    form.append(f"<input name='min_qualified_wallets' type='number' min='0' max='200' step='1' value='{min_qualified_wallets}'/>")

    form.append("<label>concurrency</label>")
    form.append(f"<input name='concurrency' type='number' min='1' max='50' step='1' value='{concurrency}'/>")

    form.append("<label>timeout_s</label>")
    form.append(f"<input name='timeout_s' type='number' min='1' max='120' step='1' value='{timeout_s}'/>")

    form.append("<label><input type='checkbox' name='debug' value='true' " + checked_dbg + "/> Debug</label>")
    form.append("<button type='submit'>Analyze</button>")
    form.append("</form>")
    form_html = "".join(form)

    if not url:
        return HTMLResponse(_ui_page(form_html))

    # Convert checkbox query strings into booleans if needed
    # (Vercel/fastapi may pass "true" as string)
    all_bool = all if isinstance(all, bool) else str(all).lower() == "true"
    debug_bool = debug if isinstance(debug, bool) else str(debug).lower() == "true"

    try:
        data = await analyze(
            url=url,
            market_index=market_index,
            all=all_bool,
            min_profit=min_profit,
            holders_limit=holders_limit,
            min_balance=min_balance,
            consensus_threshold=consensus_threshold,
            whale_threshold=whale_threshold,
            min_qualified_wallets=min_qualified_wallets,
            concurrency=concurrency,
            timeout_s=timeout_s,
            debug=debug_bool,
        )
    except HTTPException as e:
        return HTMLResponse(_ui_page(form_html, error=str(e.detail)), status_code=e.status_code)

    # If needs selection, render clickable market list
    if data.get("needs_selection"):
        event = data.get("event") or {}
        markets = data.get("event_markets") or []
        parts = []
        parts.append("<h2>Pick a market</h2>")
        parts.append(f"<div class='hint'>Event: <b>{html.escape(str(event.get('title') or ''))}</b></div>")
        parts.append("<ul>")
        for m in markets:
            idx = m.get("index")
            q = m.get("question") or ""
            # link back to /api/ui with same params, plus market_index
            base = f"/api/ui?url={quote(url)}&market_index={idx}"
            base += f"&min_profit={min_profit}&holders_limit={holders_limit}&min_balance={min_balance}"
            base += f"&consensus_threshold={consensus_threshold}&whale_threshold={whale_threshold}&min_qualified_wallets={min_qualified_wallets}"
            base += f"&concurrency={concurrency}&timeout_s={timeout_s}"
            if debug_bool:
                base += "&debug=true"
            parts.append(f"<li><a href='{base}'>{html.escape(f'[{idx}] {q}')}</a></li>")
        parts.append("</ul>")

        return HTMLResponse(_ui_page(form_html + "".join(parts)))

    text_out = _format_cli_like_text(data)
    return HTMLResponse(_ui_page(form_html, result_text=text_out))

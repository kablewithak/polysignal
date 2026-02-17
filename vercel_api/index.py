from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

# Ensure src/ is importable for "src layout" packages (src/polysignal/*)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from polysignal.analysis import analyze_market  # noqa: E402


app = FastAPI(
    title="Polysignal",
    version="0.1",
    description="HTTP wrapper for the Polysignal analysis pipeline.",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _as_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    try:
        return f"{100.0 * float(x):.2f}%"
    except Exception:
        return "—"


def _row_get(r: Any, *keys: str, default: Any = None) -> Any:
    # Backward compatible with dict rows and object rows
    if isinstance(r, dict):
        for k in keys:
            if k in r and r[k] is not None:
                return r[k]
        feats = r.get("features")
        if isinstance(feats, dict):
            for k in keys:
                if k in feats and feats[k] is not None:
                    return feats[k]
        return default

    for k in keys:
        v = getattr(r, k, None)
        if v is not None:
            return v
    return default


def _f0(x: Any) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0


def _format_pnl_cell(r: Any) -> str:
    """
    Stable PnL display for web text:

    - LB: leaderboard all-time PnL => show number + [LB]
    - REC: derived from scanned closes => show number + [REC]
    - UNK: show [REC] only if we have recent pnl fields, else —
    - Legacy: if no source tag exists, show pnl_all if present
    """
    pnl_src = _row_get(r, "pnl_src", "pnl_source", "pnl_tag", default=None)
    pnl_all = _row_get(r, "pnl_all", "pnl", "profit", default=None)

    if pnl_src is not None:
        src = str(pnl_src).upper()

        if src == "LB":
            val = _f0(pnl_all)
            return f"{val:,.0f} [LB]"

        pnl_recent = _row_get(r, "pnl_recent", default=None)
        pnl_recent_n = int(_f0(_row_get(r, "pnl_recent_n", default=0)))

        if src in {"REC", "UNK"}:
            if pnl_recent is not None and pnl_recent_n > 0:
                val = _f0(pnl_recent)
                return f"{val:,.0f} [REC]"
            return "—"

        return "—"

    if pnl_all is None:
        return "—"
    return f"{_f0(pnl_all):,.0f}"


def _fmt_wallet(addr: str) -> str:
    if not addr:
        return "—"
    return addr[:10] + "…" if len(addr) > 12 else addr


def _format_cli_text(result: Dict[str, Any], *, debug: bool = False) -> str:
    # Event selection case
    if result.get("needs_selection"):
        ev = result.get("event") or {}
        lines: List[str] = []
        lines.append(f"EVENT: {ev.get('title')}")
        lines.append(f"slug={ev.get('slug')} id={ev.get('id')}")
        lines.append("")
        lines.append("Markets in this event (pick one):")
        for m in result.get("event_markets") or []:
            lines.append(f"  [{m.get('index')}] {m.get('question')}  (slug: {m.get('slug')})")
        lines.append("")
        lines.append("Re-run with market_index=<N> (or all=true).")
        return "\n".join(lines)

    # All markets case
    if result.get("all_markets"):
        ev = result.get("event") or {}
        chunks: List[str] = []
        chunks.append(f"EVENT (ALL MARKETS): {ev.get('title')}  slug={ev.get('slug')}")
        chunks.append("")
        for i, r in enumerate(result.get("results") or []):
            chunks.append(f"=== Market #{i} ===")
            chunks.append(_format_cli_text(r, debug=debug))
            chunks.append("")
        return "\n".join(chunks).rstrip()

    m = result.get("market") or {}
    lines = []
    lines.append(f"Market: {m.get('question')}")
    lines.append(f"slug={m.get('slug')} conditionId={m.get('conditionId')}")
    lines.append("")

    outcomes = m.get("outcomes") or []
    probs = m.get("market_probs") or []
    if outcomes and probs and len(outcomes) == len(probs):
        implied = " | ".join(f"{o}: {float(p):.2f}" for o, p in zip(outcomes, probs))
        lines.append(f"Market implied: {implied}")
        lines.append("")

    lines.append(f"Recommendation: {result.get('recommendation')} (confidence {float(result.get('confidence') or 0.0):.1f}/10)")
    lines.append(f"Qualified wallets: {result.get('n_wallets_qualified')} / holders scanned: {result.get('n_wallets_considered', result.get('n_wallets_scanned', ''))}")

    diag = result.get("diagnostics") or {}
    if isinstance(diag, dict) and diag:
        if diag.get("gate"):
            lines.append(f"Gate triggered: {diag.get('gate')}")
        if "top_wallet_share" in diag:
            lines.append(f"Top wallet share: {_as_pct(diag.get('top_wallet_share'))}")
        if "top_outcome" in diag and "top_outcome_share" in diag:
            lines.append(f"Top outcome: {diag.get('top_outcome')} ({_as_pct(diag.get('top_outcome_share'))} weight share)")
        if debug and isinstance(diag.get("drop_reasons"), dict) and diag.get("drop_reasons"):
            drops = diag.get("drop_reasons") or {}
            lines.append("Drop reasons: " + ", ".join(f"{k}={v}" for k, v in drops.items()))

    lines.append("")
    lines.append("Smart-money weighted stance:")
    for k, v in (result.get("dist") or {}).items():
        try:
            lines.append(f"  {k}: {float(v):.2%}")
        except Exception:
            lines.append(f"  {k}: —")

    rows = result.get("rows") or []
    if rows:
        lines.append("")
        lines.append("Top wallets (ranked by weight):")
        header = f"{'Wallet':<12}  {'PnL (ALL)':>12}  {'Outcome':<6}  {'MktValue':>10}  {'Win%':>5}  {'WRn':>4}  {'Closed':>6}  {'Days':>4}  {'Conv':>5}  {'Weight':>8}"
        lines.append(header)
        lines.append("-" * len(header))

        for r in rows[:10]:
            addr = str(_row_get(r, "addr", "wallet", default="") or "")
            outcome = str(_row_get(r, "outcome", "side", "market_outcome", default="") or "")
            mv = _f0(_row_get(r, "market_value", "marketValue", "position_size", default=0.0))
            win_rate = _row_get(r, "win_rate", default=None)
            wr_n = _row_get(r, "wr_n", default="")
            closed_scanned = _row_get(r, "closed_scanned", default="")
            days = _row_get(r, "days_since_active", default=None)
            conv = _row_get(r, "conviction_ratio", default=None)
            weight = _f0(_row_get(r, "weight", default=0.0))

            wr = "—" if win_rate is None else f"{100.0 * float(win_rate):.0f}%"
            ds = "—" if days is None else f"{float(days):.0f}"
            cr = "—" if conv is None else f"{float(conv):.2f}"

            wallet_disp = addr if debug else _fmt_wallet(addr)
            pnl_cell = _format_pnl_cell(r)

            lines.append(
                f"{wallet_disp:<12}  {pnl_cell:>12}  {outcome:<6}  {mv:>10,.0f}  {wr:>5}  {str(wr_n):>4}  {str(closed_scanned):>6}  {ds:>4}  {cr:>5}  {weight:>8,.1f}"
            )

        lines.append("")
        lines.append("PnL tags: [LB]=leaderboard all-time, [REC]=sum of scanned closes, —=unknown")

    return "\n".join(lines)


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "polysignal"}


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
    return _format_cli_text(data, debug=debug)

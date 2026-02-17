from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query

from polysignal.analysis import analyze_market

app = FastAPI(
    title="Polysignal",
    version="0.1",
    description="HTTP wrapper around the Polysignal CLI analysis pipeline.",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "polysignal"}


@app.get("/api/analyze")
async def analyze(
    url: str = Query(..., description="Polymarket event or market URL"),
    market_index: Optional[int] = Query(None, ge=0, description="Required if event has multiple markets (unless all=true)"),
    all: bool = Query(False, description="Analyze all markets in an event (slow)"),

    # Keep these aligned with analyze_market defaults unless you *intend* to diverge:
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
    """
    Returns the *same* JSON-like dict your analysis pipeline returns:
    - market analysis result OR
    - {needs_selection: true, event_markets: [...]} for event URLs without market_index.
    """
    # Vercel runtime filesystem: safest writable location is /tmp.
    cache_dir = os.getenv("POLYSIGNAL_CACHE_DIR", "/tmp/polysignal-cache")
    use_cache = _bool_env("POLYSIGNAL_USE_CACHE", True)
    clear_cache = _bool_env("POLYSIGNAL_CLEAR_CACHE", False)

    try:
        result = await analyze_market(
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

            # Keep your short TTL defaults unless you want longer caching on Vercel:
            ttl_gamma_s=300,
            ttl_data_s=300,
        )
        return result

    except ValueError as e:
        # Your code raises ValueError for “not found”, “market_index out of range”, etc.
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        # Don’t leak internals by default; allow debug to return the message.
        msg = str(e) if debug else "Internal error"
        raise HTTPException(status_code=500, detail=msg) from e

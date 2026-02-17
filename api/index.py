from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query

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


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "polysignal"}


@app.get("/analyze")
async def analyze(
    url: str = Query(..., description="Polymarket event or market URL"),
    market_index: Optional[int] = Query(None, ge=0),
    all: bool = Query(False),

    # Keep API-friendly defaults; CLI can still exist unchanged
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

            # conservative TTLs; tune later
            ttl_gamma_s=300,
            ttl_data_s=300,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        msg = str(e) if debug else "Internal error"
        raise HTTPException(status_code=500, detail=msg) from e

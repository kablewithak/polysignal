from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from diskcache import Cache

from .polymarket import PolymarketClient
from .scoring import WalletFeatures, to_days_since, wallet_weight
from .utils import parse_jsonish_list, parse_polymarket_ref

_WALLET_RE = re.compile(r"0x[a-fA-F0-9]{40}")


# -------------------------
# General helpers
# -------------------------


def _as_list(x: Any) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return [x]


def _safe_float(x: Any, default: float = 0.0) -> float:
    """
    Defensive float parsing:
      - accepts ints/floats
      - accepts strings with commas/$/spaces
    """
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return default
        s = s.replace(",", "")
        s = s.replace("$", "")
        # keep digits, minus, dot
        s = re.sub(r"[^0-9\.\-]", "", s)
        if s in ("", "-", ".", "-."):
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


# -------------------------
# Wallet extraction
# -------------------------


def _extract_wallets_from_holders(holders_payload: Any) -> List[str]:
    """
    Data API /holders can return multiple shapes. We support:
      1) [ {token: ..., holders: [ {proxyWallet: ...}, ... ] }, ... ]
      2) { holders: [ ... ] }
      3) [ {proxyWallet: ...}, ... ]  (flat list)
    We also defensively extract the first 0x{40} address if present.
    """
    if not holders_payload:
        return []

    holder_rows: List[dict] = []

    if isinstance(holders_payload, list):
        if holders_payload and isinstance(holders_payload[0], dict) and "holders" in holders_payload[0]:
            for token_entry in holders_payload:
                if not isinstance(token_entry, dict):
                    continue
                for h in _as_list(token_entry.get("holders")):
                    if isinstance(h, dict):
                        holder_rows.append(h)
        else:
            for h in holders_payload:
                if isinstance(h, dict):
                    holder_rows.append(h)
    elif isinstance(holders_payload, dict):
        for h in _as_list(holders_payload.get("holders")):
            if isinstance(h, dict):
                holder_rows.append(h)

    addrs: List[str] = []
    for h in holder_rows:
        w = h.get("proxyWallet") or h.get("wallet") or h.get("user") or h.get("address")
        if not w:
            continue
        s = str(w)
        m = _WALLET_RE.search(s)
        addrs.append(m.group(0) if m else s)

    seen: set[str] = set()
    out: List[str] = []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


# -------------------------
# Market status / expiry gate
# -------------------------


def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _market_end_dt(market: dict, gamma: dict) -> Optional[datetime]:
    for src in (gamma, market):
        if not isinstance(src, dict):
            continue
        for k in ("endDate", "endDateIso", "closedTime", "closeTime", "resolvedTime"):
            v = src.get(k)
            if isinstance(v, str):
                dt = _parse_iso_dt(v)
                if dt:
                    return dt
            if isinstance(v, (int, float)) and v > 0:
                t = int(v)
                if t > 10_000_000_000:
                    t //= 1000
                return datetime.fromtimestamp(t, tz=timezone.utc)
    return None


def _market_status_gate(market: Dict[str, Any], gamma: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    closed = gamma.get("closed")
    active = gamma.get("active")

    if closed is True:
        return {"gate": "market_closed", "closed": True}
    if active is False:
        return {"gate": "market_inactive", "active": False}

    end_dt = _market_end_dt(market, gamma)
    if end_dt is not None:
        now = datetime.now(timezone.utc)
        if end_dt < now:
            return {"gate": "market_expired", "end_dt": end_dt.isoformat()}

    return None


# -------------------------
# PnL helpers (stable + provenance)
# -------------------------


_PNL_KEYS_ALLTIME = (
    "pnl",
    "profit",
    "PNL",
    "pnlUsd",
    "pnl_usd",
    "pnlAllTime",
    "pnl_all_time",
    "totalPnl",
    "totalProfit",
    "lifetimePnl",
    "lifetimeProfit",
    "realizedPnl",  # some versions store all-time realized here
)


def _pick_user_pnl_alltime(leaderboard_row: Optional[Dict[str, Any]]) -> Optional[float]:
    if not leaderboard_row or not isinstance(leaderboard_row, dict):
        return None
    for k in _PNL_KEYS_ALLTIME:
        if k in leaderboard_row and leaderboard_row.get(k) is not None:
            v = _safe_float(leaderboard_row.get(k), default=0.0)
            return float(v)
    return None


def _normalize_ts(ts: Any) -> Optional[int]:
    if ts is None:
        return None
    try:
        t = int(ts)
        if t > 10_000_000_000:
            t //= 1000
        return t
    except Exception:
        return None


def _sum_realized_pnl(closed_positions: List[dict]) -> Tuple[Optional[float], int]:
    """
    Sum realizedPnl across the closed positions we scanned (this is NOT all-time).
    Returns (sum, n_used). n_used counts rows where realizedPnl was present.
    """
    if not closed_positions:
        return (None, 0)

    total = 0.0
    n = 0
    for c in closed_positions:
        if not isinstance(c, dict):
            continue
        if "realizedPnl" not in c or c.get("realizedPnl") is None:
            continue
        total += _safe_float(c.get("realizedPnl"), 0.0)
        n += 1

    if n == 0:
        return (None, 0)
    return (float(total), int(n))


# -------------------------
# Closed positions -> winrate/recency + conviction
# -------------------------


def _winrate_and_recency_from_closed(closed_positions: List[dict]) -> Tuple[Optional[float], int, Optional[float]]:
    pnl: List[float] = []
    ts_max: Optional[int] = None

    for c in closed_positions:
        if not isinstance(c, dict):
            continue

        rp = _safe_float(c.get("realizedPnl"), 0.0)
        pnl.append(rp)

        ts_i = _normalize_ts(c.get("timestamp"))
        if ts_i is not None:
            ts_max = ts_i if ts_max is None else max(ts_max, ts_i)

    pnl_nonzero = [p for p in pnl if abs(p) > 1e-9]
    if not pnl_nonzero:
        win_rate = None
        wr_n = 0
    else:
        wins = sum(1 for p in pnl_nonzero if p > 0)
        win_rate = wins / float(len(pnl_nonzero))
        wr_n = len(pnl_nonzero)

    days_since: Optional[float] = to_days_since(ts_max) if ts_max is not None else None
    return win_rate, wr_n, days_since


def _conviction_ratio(market_value: float, closed_positions: List[dict]) -> Optional[float]:
    sizes: List[float] = []
    for c in closed_positions:
        if not isinstance(c, dict):
            continue
        sizes.append(_safe_float(c.get("totalBought"), 0.0))
    sizes = [s for s in sizes if s > 0]
    if not sizes:
        return None
    sizes.sort()
    med = sizes[len(sizes) // 2]
    if med <= 0:
        return None
    return float(market_value) / float(med)


# -------------------------
# Market + wallet enrichment
# -------------------------


def _market_outcomes_from_gamma(gamma_market: Dict[str, Any]) -> List[str]:
    outcomes = parse_jsonish_list(gamma_market.get("outcomes")) or []
    return [str(x) for x in outcomes]


def _market_probs_from_gamma(gamma_market: Dict[str, Any]) -> List[float]:
    probs_raw = parse_jsonish_list(gamma_market.get("outcomePrices")) or []
    probs = [_safe_float(p, 0.0) for p in probs_raw]
    if probs and max(probs) > 1.5:
        probs = [p / 100.0 for p in probs]
    return probs


def _market_position_summary(positions_payload: Any) -> Tuple[Optional[str], float]:
    """
    Returns (dominant_outcome, total_value) for user's open positions in this market.
    """
    if not positions_payload:
        return (None, 0.0)

    rows: List[dict] = []
    if isinstance(positions_payload, list):
        rows = [r for r in positions_payload if isinstance(r, dict)]
    elif isinstance(positions_payload, dict):
        data = positions_payload.get("data")
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        else:
            rows = []
    else:
        rows = []

    if not rows:
        return (None, 0.0)

    best_outcome: Optional[str] = None
    best_val = 0.0
    total_val = 0.0

    for r in rows:
        outcome = r.get("outcome")
        cur_val = _safe_float(r.get("currentValue"), 0.0)
        if cur_val <= 0:
            cur_val = _safe_float(r.get("totalBought"), 0.0)

        cur_val = max(0.0, cur_val)
        total_val += cur_val

        if cur_val > best_val:
            best_val = cur_val
            best_outcome = str(outcome) if outcome is not None else None

    return (best_outcome, float(total_val))


async def _fetch_closed_positions(pm: PolymarketClient, addr: str, max_closed: int, page_size: int) -> List[dict]:
    page_size = max(1, min(50, int(page_size)))
    out: List[dict] = []
    offset = 0
    while len(out) < max_closed:
        batch = await pm.get_closed_positions_recent(addr=addr, limit=page_size, offset=offset)
        if not batch:
            break
        if isinstance(batch, list):
            rows = [r for r in batch if isinstance(r, dict)]
        else:
            rows = []
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return out[:max_closed]


async def _enrich_wallet_for_market(
    pm: PolymarketClient,
    addr: str,
    condition_id: str,
    min_profit: float,
    max_closed: int,
    closed_page_size: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    # 1) All-time PnL from leaderboard (the only "real" ALL-TIME source we trust)
    try:
        lb = await pm.get_leaderboard_user(addr)
    except Exception:
        lb = None

    pnl_all_opt = _pick_user_pnl_alltime(lb)
    pnl_src = "LB" if pnl_all_opt is not None else "UNK"

    # If we require profit > 0 and we can't verify all-time pnl, drop.
    if pnl_all_opt is None and float(min_profit) > 0:
        return (None, "pnl_unknown")

    pnl_all_for_filter = float(pnl_all_opt) if pnl_all_opt is not None else 0.0
    if pnl_all_opt is not None and pnl_all_for_filter < float(min_profit):
        return (None, "pnl_below_min_profit")

    # 2) Must have an open position in this market (we are inferring "stance")
    try:
        positions = await pm.get_positions_for_user_market(addr=addr, condition_id=condition_id, limit=200)
    except Exception:
        return (None, "positions_error")

    outcome, market_value = _market_position_summary(positions)
    if not outcome:
        return (None, "no_position_in_market")
    if market_value <= 0:
        return (None, "position_zero_value")

    # 3) Closed positions (for win-rate + recency + conviction) + optional recent pnl estimate
    try:
        closed = await _fetch_closed_positions(pm, addr, max_closed=max_closed, page_size=closed_page_size)
    except Exception:
        closed = []

    win_rate, wr_n, days_since = _winrate_and_recency_from_closed(closed)
    conv = _conviction_ratio(market_value, closed)
    pnl_recent, pnl_recent_n = _sum_realized_pnl(closed)

    feats = WalletFeatures(
        pnl_all=float(pnl_all_for_filter),  # always a float for weighting stability
        win_rate=win_rate,
        days_since_active=days_since,
        conviction_ratio=conv,
        market_outcome=str(outcome) if outcome is not None else None,
        market_value=float(market_value),
    )
    weight = wallet_weight(feats)

    return (
        {
            "addr": addr,
            "outcome": str(outcome),
            "market_value": float(market_value),
            "win_rate": win_rate,
            "wr_n": int(wr_n),
            "closed_scanned": int(len(closed)),
            "days_since_active": days_since,
            "conviction_ratio": conv,
            "weight": float(weight),
            # PnL fields + provenance
            "pnl_all": float(pnl_all_for_filter),
            "pnl_all_known": bool(pnl_all_opt is not None),
            "pnl_src": pnl_src,  # "LB" or "UNK"
            "pnl_recent": pnl_recent,
            "pnl_recent_n": int(pnl_recent_n),
        },
        None,
    )


def _weighted_distribution(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    dist: Dict[str, float] = {}
    tot = 0.0
    for r in rows:
        w = _safe_float(r.get("weight"), 0.0)
        if w <= 0:
            continue
        o = str(r.get("outcome") or "")
        if not o:
            continue
        dist[o] = dist.get(o, 0.0) + w
        tot += w
    if tot <= 0:
        return {}
    return {k: v / tot for k, v in sorted(dist.items(), key=lambda kv: -kv[1])}


def _confidence_from_dist(dist: Dict[str, float]) -> float:
    if not dist:
        return 0.0
    shares = list(dist.values())
    if len(shares) == 1:
        return 10.0
    top = shares[0]
    second = shares[1]
    margin = max(0.0, top - second)
    return max(0.0, min(10.0, 10.0 * margin))


def _gate_recommendation(
    dist: Dict[str, float],
    rows: List[Dict[str, Any]],
    consensus_threshold: float,
    whale_threshold: float,
    min_qualified_wallets: int,
) -> Tuple[str, Dict[str, Any]]:
    diag: Dict[str, Any] = {}

    if not dist or not rows:
        diag["gate"] = "no_qualified_wallets"
        return ("STAY OUT", diag)

    top_outcome, top_share = next(iter(dist.items()))
    diag["top_outcome"] = top_outcome
    diag["top_outcome_share"] = float(top_share)

    tot_w = sum(max(0.0, _safe_float(r.get("weight"), 0.0)) for r in rows)
    top_wallet_share = 0.0
    if tot_w > 0:
        top_wallet_share = max(max(0.0, _safe_float(r.get("weight"), 0.0)) / tot_w for r in rows)
    diag["top_wallet_share"] = float(top_wallet_share)

    if len(rows) < int(min_qualified_wallets):
        diag["gate"] = f"min_qualified_wallets_not_met ({len(rows)} < {min_qualified_wallets})"
        return ("STAY OUT", diag)

    if top_wallet_share >= float(whale_threshold):
        diag["gate"] = f"whale_dominance ({top_wallet_share:.2%} >= {whale_threshold:.0%})"
        return ("STAY OUT", diag)

    if float(top_share) < float(consensus_threshold):
        diag["gate"] = f"no_consensus ({top_share:.2%} < {consensus_threshold:.0%})"
        return ("STAY OUT", diag)

    return (f"BUY {top_outcome}", diag)


async def _resolve_market_if_needed(pm: PolymarketClient, market: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fallback path:
    Sometimes event.markets items are partial and missing conditionId.
    If so, fetch the full market by slug.
    """
    if not isinstance(market, dict):
        return market
    if market.get("conditionId"):
        return market
    slug = market.get("slug")
    if not slug:
        return market
    try:
        full = await pm.get_market_by_slug(str(slug))
        if isinstance(full, dict) and full:
            merged = dict(full)
            merged.update(market)  # keep any event-attached fields
            return merged
    except Exception:
        pass
    return market


async def _analyze_single_market(
    pm: PolymarketClient,
    market: Dict[str, Any],
    min_profit: float,
    holders_limit: int,
    min_balance: float,
    max_closed: int,
    closed_page_size: int,
    concurrency: int,
    consensus_threshold: float,
    whale_threshold: float,
    min_qualified_wallets: int,
) -> Dict[str, Any]:
    market = await _resolve_market_if_needed(pm, market)

    condition_id = str(market.get("conditionId") or "")
    if not condition_id:
        return {
            "market": {"question": market.get("question"), "slug": market.get("slug"), "conditionId": None},
            "recommendation": "STAY OUT",
            "confidence": 0.0,
            "n_wallets_qualified": 0,
            "n_wallets_considered": 0,
            "dist": {},
            "rows": [],
            "diagnostics": {"gate": "missing_conditionId"},
        }

    gamma = await pm.gamma_market(condition_id=condition_id)
    outcomes = _market_outcomes_from_gamma(gamma)
    probs = _market_probs_from_gamma(gamma)

    status_gate = _market_status_gate(market, gamma)
    if status_gate is not None:
        return {
            "market": {
                "question": market.get("question"),
                "slug": market.get("slug"),
                "conditionId": condition_id,
                "outcomes": outcomes,
                "market_probs": probs,
                "active": gamma.get("active"),
                "closed": gamma.get("closed"),
                "endDate": gamma.get("endDate"),
                "closedTime": gamma.get("closedTime"),
            },
            "recommendation": "STAY OUT",
            "confidence": 0.0,
            "n_wallets_qualified": 0,
            "n_wallets_considered": 0,
            "dist": {},
            "rows": [],
            "diagnostics": status_gate,
        }

    holders_payload = await pm.get_holders(condition_id=condition_id, limit=holders_limit, min_balance=int(min_balance))
    wallets = _extract_wallets_from_holders(holders_payload)

    drop: Counter[str] = Counter()
    if not wallets:
        drop["no_holders_returned"] += 1

    request_stats: Dict[str, Any] = {
        "conditionId": condition_id,
        "holders_limit": holders_limit,
        "holders_found": len(wallets),
    }

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def enrich(addr: str) -> Optional[Dict[str, Any]]:
        async with sem:
            try:
                row, reason = await _enrich_wallet_for_market(
                    pm=pm,
                    addr=addr,
                    condition_id=condition_id,
                    min_profit=min_profit,
                    max_closed=max_closed,
                    closed_page_size=closed_page_size,
                )
                if row is None:
                    drop[reason or "filtered_out"] += 1
                return row
            except Exception:
                drop["enrich_error"] += 1
                return None

    rows = [r for r in await asyncio.gather(*[enrich(a) for a in wallets]) if r is not None]
    rows.sort(key=lambda r: _safe_float(r.get("weight"), 0.0), reverse=True)

    dist = _weighted_distribution(rows)
    confidence = _confidence_from_dist(dist)
    rec, gate_diag = _gate_recommendation(
        dist=dist,
        rows=rows,
        consensus_threshold=consensus_threshold,
        whale_threshold=whale_threshold,
        min_qualified_wallets=min_qualified_wallets,
    )

    diagnostics: Dict[str, Any] = dict(gate_diag)
    diagnostics["drop_reasons"] = dict(drop)
    diagnostics["request_stats"] = request_stats

    return {
        "market": {
            "question": market.get("question"),
            "slug": market.get("slug"),
            "conditionId": condition_id,
            "outcomes": outcomes,
            "market_probs": probs,
            "active": gamma.get("active"),
            "closed": gamma.get("closed"),
            "endDate": gamma.get("endDate"),
            "closedTime": gamma.get("closedTime"),
        },
        "recommendation": rec,
        "confidence": float(confidence),
        "n_wallets_qualified": int(len(rows)),
        "n_wallets_considered": int(len(wallets)),
        "dist": dist,
        "rows": rows,
        "diagnostics": diagnostics,
    }


def _make_pm_client(
    use_cache: bool,
    cache_dir: str,
    clear_cache: bool,
    ttl_gamma_s: int,
    ttl_data_s: int,
    timeout_s: float,
) -> PolymarketClient:
    cache = Cache(cache_dir)
    if clear_cache:
        try:
            cache.clear()
        except Exception:
            pass
    return PolymarketClient(
        cache=cache,
        use_cache=use_cache,
        ttl_gamma_s=ttl_gamma_s,
        ttl_data_s=ttl_data_s,
        timeout_s=timeout_s,
    )


async def analyze_market(
    market_url_or_slug: str,
    market_index: Optional[int] = None,
    min_profit: float = 5000.0,
    holders_limit: int = 20,
    min_balance: float = 0.0,
    use_cache: bool = True,
    cache_dir: str = ".cache/polysignal",
    clear_cache: bool = False,
    ttl_gamma_s: int = 300,
    ttl_data_s: int = 300,
    max_closed: int = 500,
    closed_page_size: int = 50,
    consensus_threshold: float = 0.60,
    whale_threshold: float = 0.55,
    min_qualified_wallets: int = 3,
    concurrency: int = 12,
    timeout_s: float = 25.0,
    debug: bool = False,
    all_markets_in_event: bool = False,
) -> Dict[str, Any]:
    _ = debug
    kind, ref = parse_polymarket_ref(market_url_or_slug)

    pm = _make_pm_client(
        use_cache=use_cache,
        cache_dir=cache_dir,
        clear_cache=clear_cache,
        ttl_gamma_s=ttl_gamma_s,
        ttl_data_s=ttl_data_s,
        timeout_s=timeout_s,
    )

    try:
        if kind == "market":
            market = await pm.get_market_by_slug(ref)
            if not market:
                raise ValueError(f"Market not found for slug: {ref}")
            result = await _analyze_single_market(
                pm=pm,
                market=market,
                min_profit=min_profit,
                holders_limit=holders_limit,
                min_balance=min_balance,
                max_closed=max_closed,
                closed_page_size=closed_page_size,
                concurrency=concurrency,
                consensus_threshold=consensus_threshold,
                whale_threshold=whale_threshold,
                min_qualified_wallets=min_qualified_wallets,
            )
            result["request_stats"] = pm.stats.snapshot()
            return result

        event = await pm.get_event_by_slug(ref)
        if not event:
            raise ValueError(f"Event not found for slug: {ref}")

        markets = event.get("markets") or []
        if not isinstance(markets, list):
            markets = []

        if not markets:
            raise ValueError("Event has no markets attached.")

        if len(markets) > 1 and market_index is None and not all_markets_in_event:
            return {
                "needs_selection": True,
                "event": {"title": event.get("title"), "slug": event.get("slug"), "id": event.get("id")},
                "event_markets": [
                    {"index": i, "question": m.get("question"), "slug": m.get("slug")} for i, m in enumerate(markets)
                ],
                "request_stats": pm.stats.snapshot(),
            }

        if all_markets_in_event:
            results: List[Dict[str, Any]] = []
            for m in markets:
                res = await _analyze_single_market(
                    pm=pm,
                    market=m,
                    min_profit=min_profit,
                    holders_limit=holders_limit,
                    min_balance=min_balance,
                    max_closed=max_closed,
                    closed_page_size=closed_page_size,
                    concurrency=concurrency,
                    consensus_threshold=consensus_threshold,
                    whale_threshold=whale_threshold,
                    min_qualified_wallets=min_qualified_wallets,
                )
                results.append(res)
            return {
                "all_markets": True,
                "event": {"title": event.get("title"), "slug": event.get("slug"), "id": event.get("id")},
                "results": results,
                "request_stats": pm.stats.snapshot(),
            }

        idx = int(market_index or 0)
        if idx < 0 or idx >= len(markets):
            raise ValueError(f"market_index out of range: {idx} (0..{len(markets)-1})")

        market = markets[idx]
        result = await _analyze_single_market(
            pm=pm,
            market=market,
            min_profit=min_profit,
            holders_limit=holders_limit,
            min_balance=min_balance,
            max_closed=max_closed,
            closed_page_size=closed_page_size,
            concurrency=concurrency,
            consensus_threshold=consensus_threshold,
            whale_threshold=whale_threshold,
            min_qualified_wallets=min_qualified_wallets,
        )
        result["request_stats"] = pm.stats.snapshot()
        return result

    finally:
        await pm.aclose()
        try:
            if pm.cache is not None:
                pm.cache.close()
        except Exception:
            pass

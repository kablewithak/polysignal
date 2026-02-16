from __future__ import annotations

import json
import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse


def parse_jsonish_list(x: Any) -> Optional[List[Any]]:
    """
    Accepts:
      - real lists
      - JSON strings like '["Yes","No"]'
      - python-ish strings like "['Yes', 'No']"
      - returns list or None
    """
    if x is None:
        return None
    if isinstance(x, list):
        return x
    if not isinstance(x, str):
        return None

    s = x.strip()
    if not s:
        return None

    # Try strict JSON first
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else None
    except Exception:
        pass

    # Try a gentle python-ish conversion: single quotes -> double quotes
    # Only do this if it looks like a list.
    if s.startswith("[") and s.endswith("]"):
        s2 = re.sub(r"'", '"', s)
        try:
            v = json.loads(s2)
            return v if isinstance(v, list) else None
        except Exception:
            return None

    return None


def parse_polymarket_ref(ref: str) -> Tuple[str, str]:
    """
    Returns (kind, slug_or_ref):
      kind in {"market", "event", "category"}

    Supports:
      - Full URLs:
          https://polymarket.com/market/<slug>
          https://polymarket.com/event/<slug>
          https://polymarket.com/<category>   (e.g., /finance)
      - Short forms:
          market:<slug>
          event:<slug>
          category:<slug>
      - Raw slug (defaults to "market")
    """
    if not ref or not str(ref).strip():
        raise ValueError("Empty polymarket reference")

    s = str(ref).strip()

    # Explicit prefixes
    lowered = s.lower()
    for prefix, kind in (("market:", "market"), ("event:", "event"), ("category:", "category")):
        if lowered.startswith(prefix):
            return kind, s[len(prefix) :].strip()

    # URL parsing
    if "://" in s:
        u = urlparse(s)
        host = (u.netloc or "").lower()
        path = (u.path or "").strip("/")

        # tolerate www.
        if host.endswith("polymarket.com"):
            parts = [p for p in path.split("/") if p]

            # /market/<slug>
            if len(parts) >= 2 and parts[0].lower() == "market":
                return "market", parts[1]

            # /event/<slug>
            if len(parts) >= 2 and parts[0].lower() == "event":
                return "event", parts[1]

            # /<category> (finance, sports, politics, etc.)
            if len(parts) == 1:
                return "category", parts[0]

        # If it’s a URL but not polymarket, fall through as raw string.
        # (You may pass a slug-like string; we'll treat it as market by default.)

    # Raw slug fallback
    return "market", s


# Backwards-compatible helper name (if older code imported this)
def parse_polymarket_ref_slug_only(ref: str) -> str:
    _, slug = parse_polymarket_ref(ref)
    return slug


# Keep an alias that older code sometimes expects
parse_market_slug = parse_polymarket_ref_slug_only

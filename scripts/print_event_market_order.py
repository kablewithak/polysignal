import asyncio
import sys

import polysignal.analysis as analysis


async def main(event_slug: str) -> None:
    """
    Prints the order of markets exactly as returned by the underlying Gamma/event API call
    (raw list order we receive).
    """

    client = analysis._make_pm_client(
        use_cache=False,
        cache_dir=".cache/polysignal",  # required by factory; cache is disabled anyway
        clear_cache=False,
        ttl_gamma_s=0,
        ttl_data_s=0,
        timeout_s=25.0,
    )

    try:
        ev = await client.get_event_by_slug(event_slug)
        markets = ev.get("markets") or []

        print(f"RAW API markets order (count={len(markets)})")
        for i, m in enumerate(markets):
            print(f"{i:02d}  slug={m.get('slug')}  question={m.get('question')}")
    finally:
        try:
            await client.aclose()
        finally:
            try:
                if getattr(client, "cache", None) is not None:
                    client.cache.close()
            except Exception:
                pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/print_event_market_order.py <event-slug>")
        print("Example: python scripts/print_event_market_order.py 2026-winter-olympics-most-gold-medals")
        raise SystemExit(2)

    asyncio.run(main(sys.argv[1]))

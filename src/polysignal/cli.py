from __future__ import annotations

import asyncio
import platform
import shutil
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .analysis import analyze_market

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    context_settings={"allow_interspersed_args": True},
)
console = Console()

DEFAULT_CACHE_DIR = str(Path.home() / ".polysignal-cache")
DEFAULT_TTL_GAMMA_S = 6 * 60 * 60
DEFAULT_TTL_DATA_S = 5 * 60


def _normalize_url_args(args: List[str]) -> str:
    if not args:
        raise typer.BadParameter('Missing URL.\nTry: polysignal "https://polymarket.com/event/<slug>"')

    if args[0].lower() == "analyze":
        args = args[1:]

    if len(args) != 1:
        raise typer.BadParameter(f"Expected exactly one URL. Got: {args}")

    return args[0]


def _as_pct(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"{100.0 * x:.2f}%"


def _format_reliability(rel: Any) -> Optional[str]:
    if rel is None:
        return None
    if isinstance(rel, (int, float)):
        return f"{float(rel):.2f}"
    if isinstance(rel, dict):
        r = rel.get("reliability")
        if r is None:
            return None
        n_wallets = rel.get("n_wallets")
        low_cnt = rel.get("low_sample_wallets")
        cap_n = rel.get("cap_n")
        low_thr = rel.get("low_threshold")
        med_factor = rel.get("median_factor")
        low_pen = rel.get("low_penalty")

        parts = [f"{float(r):.2f}"]
        detail = []
        if med_factor is not None:
            detail.append(f"median_factor={med_factor}")
        if low_pen is not None:
            detail.append(f"low_penalty={low_pen}")
        if low_cnt is not None and n_wallets is not None and low_thr is not None:
            detail.append(f"low_sample_wallets={low_cnt}/{n_wallets} (<{low_thr})")
        if cap_n is not None:
            detail.append(f"cap_n={cap_n}")

        if detail:
            parts.append("(" + ", ".join(str(x) for x in detail) + ")")
        return " ".join(parts)

    return str(rel)


def _print_request_stats(stats: Dict[str, Any]) -> None:
    console.print("\n[bold]Request stats[/bold]")
    console.print(
        f"[dim]http_requests={stats.get('http_requests')} "
        f"cache_hits={stats.get('cache_hits')} cache_misses={stats.get('cache_misses')} "
        f"http_time_s={stats.get('http_time_s')} elapsed_s={stats.get('elapsed_s')}[/dim]"
    )
    by_host = stats.get("by_host") or {}
    if isinstance(by_host, dict) and by_host:
        for host, n in by_host.items():
            console.print(f"[dim]- {host}: {n}[/dim]")


def _print_doctor(*, cache_dir: str) -> None:
    try:
        version = metadata.version("polysignal")
    except Exception:
        version = "unknown"

    import polysignal  # noqa: WPS433
    import polysignal.cli as c  # noqa: WPS433

    console.print("\n[bold]Polysignal doctor[/bold]")
    console.print(f"[dim]version[/dim] {version}")
    console.print(f"[dim]python[/dim] {sys.version.split()[0]} ({sys.executable})")
    console.print(f"[dim]platform[/dim] {platform.platform()}")
    console.print(f"[dim]polysignal.__file__[/dim] {polysignal.__file__}")
    console.print(f"[dim]polysignal.cli.__file__[/dim] {c.__file__}")
    console.print(f"[dim]polysignal (exe)[/dim] {shutil.which('polysignal')}")
    console.print(f"[dim]cache_dir[/dim] {cache_dir}\n")


def _print_event_selection(result: dict) -> None:
    ev = result.get("event") or {}
    console.print(f"\n[bold]EVENT:[/bold] {ev.get('title')}")
    console.print(f"[dim]slug={ev.get('slug')} id={ev.get('id')}[/dim]\n")

    table = Table(title="Markets in this event (pick one)")
    table.add_column("Index", justify="right")
    table.add_column("Question")
    table.add_column("Market Slug", style="dim")

    for m in result.get("event_markets") or []:
        table.add_row(str(m.get("index")), str(m.get("question")), str(m.get("slug")))
    console.print(table)

    console.print("\nRun one of these:\n")
    console.print('  [bold]polysignal "<event-url>" --market-index 2[/bold]')
    console.print('  [bold]polysignal analyze "<event-url>" --market-index 2[/bold]  [dim](alias; same thing)[/dim]')
    console.print('  [bold]polysignal "<event-url>" --all[/bold]  [dim](slow, analyzes every market)[/dim]\n')


def _row_get(r: Any, *keys: str, default: Any = None) -> Any:
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


def _format_pnl_cell(r: Any, *, debug: bool = False) -> str:
    """
    Stable PnL display, preserving truthfulness:

    - LB: all-time leaderboard pnl. In non-debug, show numeric. In debug, append [LB].
    - REC: pnl derived from scanned closes (NOT all-time). Always append [REC] so we don't mislead.
    - UNK: if we have a computed recent pnl, show it as [REC]; else "—".
    - Backward compatible: if no pnl provenance exists, show pnl_all as numeric.
    """
    pnl_src = _row_get(r, "pnl_src", "pnl_source", "pnl_tag", default=None)
    pnl_all = _row_get(r, "pnl_all", "pnl", "profit", default=None)

    if pnl_src is not None:
        src = str(pnl_src).upper()

        if src == "LB":
            val = _f0(pnl_all)
            return f"{val:,.0f} [LB]" if debug else f"{val:,.0f}"

        # Prefer explicit recent fields if present (analysis can provide these)
        pnl_recent = _row_get(r, "pnl_recent", default=None)
        pnl_recent_n = int(_f0(_row_get(r, "pnl_recent_n", default=0)))

        if src == "REC":
            # REC must be labeled even in non-debug (it's not truly "ALL")
            if pnl_recent is not None and pnl_recent_n > 0:
                val = _f0(pnl_recent)
                return f"{val:,.0f} [REC]"
            # If REC but missing fields, do not fake it.
            return "—"

        if src == "UNK":
            if pnl_recent is not None and pnl_recent_n > 0:
                val = _f0(pnl_recent)
                return f"{val:,.0f} [REC]"
            return "—"

        # Unknown source label: be conservative
        return "—"

    # Older analysis: just show pnl_all if it exists.
    if pnl_all is None:
        return "—"
    return f"{_f0(pnl_all):,.0f}"


def _print_single_result(result: dict, *, debug: bool = False) -> None:
    m = result["market"]
    console.print(f"\n[bold]Market:[/bold] {m.get('question')}")
    console.print(f"[dim]slug={m.get('slug')} conditionId={m.get('conditionId')}[/dim]")

    if m.get("outcomes") and m.get("market_probs"):
        pairs = " | ".join(f"{o}: {p:.2f}" for o, p in zip(m["outcomes"], m["market_probs"]))
        console.print(f"[bold]Market implied:[/bold] {pairs}")

    console.print(
        f"\n[bold]Recommendation:[/bold] {result['recommendation']}  "
        f"[dim](confidence {result['confidence']:.1f}/10)[/dim]"
    )

    console.print(
        f"[dim]Qualified wallets: {result.get('n_wallets_qualified')} / "
        f"holders scanned: {result.get('n_wallets_considered', result.get('n_wallets_scanned', ''))}[/dim]"
    )

    diag = result.get("diagnostics") or {}
    if isinstance(diag, dict) and diag:
        gate = diag.get("gate")
        if gate:
            console.print(f"[yellow]Gate triggered:[/yellow] {gate}")

        if "top_wallet_share" in diag:
            console.print(f"[dim]Top wallet share: {_as_pct(diag.get('top_wallet_share'))}[/dim]")

        if "top_outcome" in diag and "top_outcome_share" in diag:
            console.print(
                f"[dim]Top outcome: {diag.get('top_outcome')} "
                f"({_as_pct(diag.get('top_outcome_share'))} weight share)[/dim]"
            )

        ss = diag.get("sample_size") or {}
        if isinstance(ss, dict) and ss:
            console.print(
                f"[dim]Win-rate sample: median wr_n={ss.get('median_wr_n')} "
                f"(min {ss.get('min_wr_n')}, max {ss.get('max_wr_n')}), "
                f"median closed scanned={ss.get('median_closed_scanned')}. "
                f"Low-sample wallets (<{ss.get('low_sample_threshold')}): {ss.get('low_sample_wallets')}[/dim]"
            )

        rel_str = _format_reliability(diag.get("reliability"))
        if rel_str is not None:
            console.print(f"[dim]Reliability: {rel_str}[/dim]")

        if (result.get("n_wallets_qualified") or 0) == 0:
            drops = diag.get("drop_reasons") or {}
            if isinstance(drops, dict) and drops:
                console.print("[yellow]Drop reasons (why wallets were filtered out)[/yellow]")
                for k, v in sorted(drops.items(), key=lambda kv: (-int(kv[1]), str(kv[0]))):
                    console.print(f"[dim]- {k}: {v}[/dim]")
            else:
                console.print("[yellow]No drop reasons recorded (unexpected)[/yellow]")
        elif debug:
            drops = diag.get("drop_reasons") or {}
            if isinstance(drops, dict) and drops:
                console.print("[dim]Drop reasons:[/dim] " + ", ".join(f"{k}={v}" for k, v in drops.items()))

    console.print()

    dist_table = Table(title="Smart-money weighted stance")
    dist_table.add_column("Outcome", justify="left")
    dist_table.add_column("Weight share", justify="right")
    for k, v in (result.get("dist") or {}).items():
        dist_table.add_row(k, f"{v:.2%}")
    console.print(dist_table)

    t = Table(title="Top wallets (ranked by weight)")
    t.add_column("Wallet")
    t.add_column("PnL (ALL)", justify="right")
    t.add_column("Outcome", justify="left")
    t.add_column("Mkt Value", justify="right")
    t.add_column("Win%", justify="right")
    t.add_column("WR n", justify="right")
    t.add_column("Closed", justify="right")
    t.add_column("Days", justify="right")
    t.add_column("Conv", justify="right")
    t.add_column("Weight", justify="right")

    rows = result.get("rows") or []
    for r in rows[:10]:
        addr = str(_row_get(r, "addr", "wallet", default="") or "")
        outcome = _row_get(r, "outcome", "side", "market_outcome", default="") or ""
        mv = _f0(_row_get(r, "market_value", "marketValue", "position_size", default=0.0))

        win_rate = _row_get(r, "win_rate", default=None)
        wr_n = _row_get(r, "wr_n", default="")
        closed_scanned = _row_get(r, "closed_scanned", default="")
        days = _row_get(r, "days_since_active", default=None)
        conv = _row_get(r, "conviction_ratio", default=None)
        weight = _f0(_row_get(r, "weight", default=0.0))

        wr = "" if win_rate is None else f"{100.0 * float(win_rate):.0f}%"
        ds = "" if days is None else f"{float(days):.0f}"
        cr = "" if conv is None else f"{float(conv):.2f}"

        if not addr:
            wallet_disp = "—"
        else:
            wallet_disp = addr if debug else (addr[:10] + "…")

        pnl_cell = _format_pnl_cell(r, debug=debug)

        t.add_row(
            wallet_disp,
            pnl_cell,
            str(outcome),
            f"{mv:,.0f}",
            wr,
            str(wr_n),
            str(closed_scanned),
            ds,
            cr,
            f"{weight:,.1f}",
        )
    console.print(t)

    # Legend always visible (tests expect this, and it helps interpret [REC] when present)
    console.print("[dim]PnL tags: [LB]=leaderboard all-time, [REC]=sum of scanned closes, —=unknown[/dim]")


@app.callback(invoke_without_command=True)
def main(
    args: List[str] = typer.Argument(..., metavar="ARGS", help='Polymarket URL, or: analyze <URL> (or "doctor")'),
    min_profit: float = typer.Option(5000.0, help="Only include wallets with >= this ALL-TIME PnL (USD)."),
    holders_limit: int = typer.Option(20, help="Top holders to consider (Data API caps at 20 per token)."),
    min_balance: int = typer.Option(1, help="Min token balance to consider in holders list."),
    concurrency: int = typer.Option(8, help="Max concurrent wallet profiling requests."),
    market_index: Optional[int] = typer.Option(None, help="If URL is an EVENT, pick a market index from the printed list."),
    all_markets_in_event: bool = typer.Option(False, "--all", help="If URL is an EVENT, analyze all markets (slow)."),
    max_closed: int = typer.Option(500, help="Max closed positions to scan per wallet."),
    closed_page_size: int = typer.Option(50, help="Closed positions page size (max 50)."),
    consensus_threshold: float = typer.Option(0.62, help="Top outcome weight share required to recommend BUY."),
    whale_threshold: float = typer.Option(0.60, help="If any single wallet exceeds this weight share -> STAY OUT."),
    min_qualified_wallets: int = typer.Option(5, help="Minimum wallets passing filters to consider a BUY."),
    cache_dir: str = typer.Option(DEFAULT_CACHE_DIR, "--cache-dir", help="Disk cache directory (persists across runs)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable cache reads/writes (does NOT delete cache)."),
    clear_cache: bool = typer.Option(False, "--clear-cache", help="Clear cache before running."),
    ttl_gamma_s: int = typer.Option(DEFAULT_TTL_GAMMA_S, "--ttl-gamma", help="Gamma API cache TTL seconds."),
    ttl_data_s: int = typer.Option(DEFAULT_TTL_DATA_S, "--ttl-data", help="Data API cache TTL seconds."),
    debug: bool = typer.Option(False, "--debug", help="Print request/cache stats and extra diagnostics."),
):
    if args and args[0].lower() == "doctor":
        _print_doctor(cache_dir=cache_dir)
        raise typer.Exit(code=0)

    url = _normalize_url_args(args)

    result = asyncio.run(
        analyze_market(
            market_url_or_slug=url,
            min_profit=min_profit,
            holders_limit=holders_limit,
            min_balance=min_balance,
            cache_dir=cache_dir,
            concurrency=concurrency,
            market_index=market_index,
            all_markets_in_event=all_markets_in_event,
            max_closed=max_closed,
            closed_page_size=closed_page_size,
            consensus_threshold=consensus_threshold,
            whale_threshold=whale_threshold,
            min_qualified_wallets=min_qualified_wallets,
            use_cache=not no_cache,
            clear_cache=clear_cache,
            ttl_gamma_s=ttl_gamma_s,
            ttl_data_s=ttl_data_s,
            debug=debug,
        )
    )

    if debug and isinstance(result, dict) and isinstance(result.get("request_stats"), dict):
        _print_request_stats(result["request_stats"])

    if result.get("needs_selection"):
        _print_event_selection(result)
        raise typer.Exit(code=0)

    if result.get("all_markets"):
        ev = result.get("event") or {}
        console.print(f"\n[bold]EVENT (ALL MARKETS):[/bold] {ev.get('title')} [dim]slug={ev.get('slug')}[/dim]\n")
        for i, r in enumerate(result.get("results") or []):
            console.print(f"[bold]=== Market #{i} ===[/bold]")
            _print_single_result(r, debug=debug)
            console.print()
        raise typer.Exit(code=0)

    _print_single_result(result, debug=debug)


def entrypoint() -> None:
    app()


if __name__ == "__main__":
    app()

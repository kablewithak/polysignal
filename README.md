# polysignal

A CLI that analyzes Polymarket markets/events and outputs a **smart-money weighted stance** based on top holders, their positions, and lightweight performance signals — with **stable, provenance-aware PnL display** and a **regression harness** to prevent breaking what already works.

## What it does

Given a Polymarket **market URL** or **event URL**, `polysignal`:

- Resolves the target market (supports **event pages** and **market pages**).
- Pulls market metadata from the **Gamma API** (outcomes, implied probabilities, status).
- Uses the **Data API** to fetch top holders (capped by the API), then profiles wallets:
  - wallet’s open position outcome and market value
  - recent closed positions to estimate win-rate/recency and conviction
  - **PnL provenance** (leaderboard vs inferred recent closes vs unknown)
- Produces:
  - a recommendation: `BUY <outcome>` or `STAY OUT`
  - a confidence score (0–10)
  - a table of the top wallets ranked by weight
  - diagnostics (gates, drop reasons, request stats in debug)

## Key safety + stability decisions

### 1) Market status/expiry gate (short-circuit)
Before scanning holders (expensive), we gate:
- closed / inactive
- expired (endDate < now)

If gated, we **return early** and do **not** call holders scanning.

### 2) Provenance-aware PnL (stable display)
We avoid showing misleading “≥0” or fake PnL values.

PnL display rules:
- **[LB]**: leaderboard all-time PnL (trustworthy)
- **[REC]**: sum of scanned realized PnL from recent closes (fallback signal)
- **—**: unknown (no leaderboard row and no usable recent closes)

In non-debug mode we keep output clean; in debug mode we can show more provenance cues.

### 3) Regression harness
We ship tests that protect the working baseline:
- event market fallback resolves missing `conditionId`
- unknown PnL behavior depends on `--min-profit`
- expiry gate prevents holders scanning
- CLI formatting rules stay stable

---

## Install

### Requirements
- Python **3.11+**

### Setup (PowerShell)
From repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
If you want to run tests:

pip install -U pytest
Usage
Doctor (sanity check)
polysignal doctor
Analyze an event URL (select a market)
polysignal analyze "https://polymarket.com/event/<event-slug>"
If the event has multiple markets, you’ll get an index list. Then run:

polysignal analyze "https://polymarket.com/event/<event-slug>" --market-index 2
Analyze all markets in an event (slow)
polysignal analyze "https://polymarket.com/event/<event-slug>" --all
Common flags
Profit filter (leaderboard PnL)

polysignal analyze "<url>" --min-profit 5000
Allow unknown PnL wallets (only recommended for exploration)

polysignal analyze "<url>" --min-profit 0
Debug mode (extra diagnostics + request stats)

polysignal analyze "<url>" --market-index 2 --debug
Concurrency

polysignal analyze "<url>" --market-index 2 --concurrency 8
Caching
By default we cache API responses on disk.

default cache dir: ~/.polysignal-cache

Gamma TTL: 6 hours

Data TTL: 5 minutes

Override TTLs:

polysignal analyze "<url>" --market-index 2 --ttl-gamma 300 --ttl-data 300
Clear cache before running:

polysignal analyze "<url>" --market-index 2 --clear-cache
Disable cache entirely:

polysignal analyze "<url>" --market-index 2 --no-cache
How scoring works (high level)
We compute a weighted distribution of wallet stances (dominant outcomes) and gate recommendations using:

min qualified wallets (avoid tiny samples)

whale dominance check (if a single wallet dominates weight share → STAY OUT)

consensus threshold (top outcome must have enough weight share)

Confidence is derived from the margin between the top two outcome shares.

Output: how to read it
Market implied: implied probabilities from Gamma

Recommendation + confidence: what the engine suggests

Top wallet share: if too high, it can trigger whale gate

Smart-money stance: weighted distribution across outcomes

Top wallets table:

PnL (ALL) shows provenance tags:

[LB] leaderboard all-time PnL

[REC] sum of scanned closes

— unknown

Tests
Run the full suite:

python -m pytest
Quiet:

python -m pytest -q
These tests are designed to prevent regressions in:

event → market fallback

expiry gate short-circuit

unknown PnL behavior & CLI formatting

Repo layout
src/polysignal/
  cli.py
  analysis.py
  polymarket.py
  scoring.py
  utils.py

tests/
  conftest.py
  tests/
    regression/
      fake_pm.py
      test_analysis_regression_*.py
      test_cli_pnl_tags.py
      test_regression_harness.py
Development notes
Build + run from source (editable install)
pip install -e .
polysignal doctor
polysignal analyze "<url>" --market-index 0 --debug
python -m pytest
License
Choose a license (MIT recommended for simple open-source reuse).

::contentReference[oaicite:0]{index=0}
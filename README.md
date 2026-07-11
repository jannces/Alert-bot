# Tamad Strategy Scanner

A 24/7 market scanner for **MEXC USDT Perpetual Futures**. Python detects
valid **Tamad Strategy** setups on every active contract and notifies you on
**Telegram** with an annotated **TradingView** chart screenshot.

> **This is NOT a trading bot.** It never places, modifies, or closes orders
> and holds no exchange credentials — it reads *public* MEXC market data and
> writes Telegram messages, screenshots, logs, and database rows. Every trade
> is analyzed and executed manually by you.
>
> **Python is the single source of truth.** All scanning, pattern detection,
> S/R detection, and validation happen in Python. TradingView is strictly a
> charting and screenshot service.
>
> **Accuracy over quantity.** Every rule is mandatory, every candidate passes
> a final strict validation, and when in doubt the setup is rejected.

## Pipeline

```
MEXC Futures API → Async Python Scanner → Tamad Strategy Validation
→ Support/Resistance Validation → Risk Management Calculation
→ Duplicate Check → TradingView Screenshot (Playwright)
→ Python-drawn Overlay (Entry/SL/TP/Highlights) → Telegram Notification
→ SQLite Logging
```

A sweep fires at every 15m/30m/1h candle close over all active USDT
perpetuals (discovered automatically, refreshed hourly), rate-limited under
MEXC's public API limits. Only fully closed candles are ever evaluated, and
each bar is evaluated exactly once — including across restarts.

The full design, decision record (D1–D4), and known limitations are in
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## The Tamad Strategy (implemented exactly, no approximations)

A strict three-candle rejection pattern at meaningful support/resistance.

| | SHORT | LONG |
|---|---|---|
| Candle 1 | Green | Red |
| Candle 2 | Red, close **equal** to candle 1's close (default tolerance 0.1%) | Green, equal close |
| Level | The equal closes form the **resistance** | The equal closes form the **support** |
| Candle 3 | Green, fully **closed**. Wick may pierce the resistance, close must be ≤ resistance | Red, fully closed. Wick may pierce the support, close must be ≥ support |
| S/R filter | *Optional, off by default.* When enabled, the pattern must sit within `proximity_percent` of a confirmed swing high/low | same |
| Entry | **Close of Candle 3** (never next-candle open, market price, or midpoint) | same |
| Stop loss | **Highest wick** of the three candles | **Lowest wick** of the three candles |
| Targets | Risk = Entry − SL; TP2 = 2R, TP3 = 3R | Risk = Entry − SL; TP2 = 2R, TP3 = 3R |

The stop loss uses *only* the three strategy candles — never ATR, never
percentages, never indicators, never volatility.

Every candidate passes a final validation gate that recomputes every rule
from the raw candle data (colors, equal close, third-candle rule, S/R
presence/side/proximity, closed-candle confirmation, freshness, entry, stop,
TP2, TP3). Any failure → rejected, logged, never sent.

## Project layout

```
├── mexc/client.py               # public futures data client (read-only, rate-limited)
├── scanner/
│   ├── scheduler.py             # candle-close boundary scheduling
│   └── engine.py                # sweep → pre-filter → build candidates
├── strategy/
│   ├── models.py                # Candle / SRLevel / TamadSetup / ValidationReport
│   ├── tamad_strategy.py        # pattern rules (single source of truth)
│   ├── sr_levels.py             # S/R detection (swing high/low; extensible)
│   ├── validation.py            # final strict validation gate
│   └── pipeline.py              # dedup → screenshot → annotate → notify → persist
├── screenshots/
│   ├── capture.py               # Playwright capture of your TradingView layout
│   ├── calibration.py           # pixel↔price mapping (best-effort, auto-fallback)
│   └── annotate.py              # Pillow overlay: highlights, level lines, legend
├── tradingview/
│   ├── links.py                 # live-chart links for alerts
│   └── pine_overlay.pine        # OPTIONAL display-only fallback (draws, never decides)
├── telegram/bot.py              # alert formatting + Bot API delivery
├── database/repository.py       # signal snapshots, rejections, dedup, scan state
├── config/config.yaml           # every knob, documented inline
├── tests/                       # 113 unit + integration tests
└── main.py                      # entrypoint (+ /health endpoint)
```

## Setup

### 1. Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env      # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
set -a; source .env; set +a
```

All knobs live in [`config/config.yaml`](config/config.yaml): timeframes,
symbol whitelist, equal-close tolerance and comparison mode
(`strict`/`midpoint`), S/R method and sensitivity, rate limits, screenshot
and annotation behavior, logging.

### 3. TradingView (visualization only)

1. Log in to TradingView, build the chart layout you like (colors, zoom
   showing ~50–100 candles), and **save** it.
2. Set `screenshots.playwright.chart_url` to your saved layout URL, e.g.
   `https://www.tradingview.com/chart/AbCdEfGh/`.
3. Export a login session once so Playwright can open your private layout:

   ```bash
   python -m playwright codegen --save-storage=config/tv_storage_state.json https://www.tradingview.com
   # log in in the opened browser, then close it
   ```

No Pine script, no alerts, no webhooks — TradingView performs zero
calculations. (If pixel calibration ever proves unreliable on your layout,
`tradingview/pine_overlay.pine` is an optional display-only fallback; see
ARCHITECTURE.md §7.)

### 4. Run

```bash
python main.py --config config/config.yaml
```

or 24/7 with Docker (auto-restart, state persisted on the host):

```bash
docker compose up -d --build
```

`GET :8080/health` reports last sweep per timeframe and counters.

## Alerts

Each Telegram alert contains pair, direction, timeframe, entry, stop loss,
TP2/TP3, risk:reward, detection time, a 7-point validation checklist with a
confidence line ("7 / 7 Rules Passed"), a live TradingView chart link, and
the annotated screenshot: the three pattern candles highlighted, S/R, entry,
SL, TP2, TP3 drawn as price-anchored lines plus a legend panel. When pixel
calibration fails on a capture, the image degrades gracefully to
chart + legend (numbers always exact); when capture fails entirely, the
configured `on_failure` policy applies.

## Persistence & logging

- **Signals:** every sent alert stores a complete snapshot — OHLC of all
  three candles, levels, entry/SL/TP, S/R details, screenshot path,
  timestamps, and the validation report. Ready-made data for future
  backtesting and win-rate analytics.
- **Rejections:** near-miss candidates (matched colors + equal close but
  failed a later rule) are stored with passed rules, failed rules, failure
  reason, and candle OHLC — precise debugging without millions of "no
  pattern" rows.
- **Duplicate protection:** dedup keys are claimed atomically in SQLite
  *before* sending and survive restarts; a sent setup can never repeat.
- **Operational logs:** rotating structured logs in `logs/tamad.log`.

## Debugging: how close are near-misses getting?

If alerts feel too rare, don't guess — check the data. While the scanner is
running, in a **second** terminal:

```bash
python scripts/near_miss_report.py
```

This reads the `rejections` table and, for every candidate that failed the
third-candle rule (the strategy's core "close must never pass the level"
check), reports how far past the level it closed, closest-first. That tells
you whether the remaining filter is actually close to firing (small
overshoots — patience is the answer) or whether candidates are missing by a
mile (a config knob is still too tight). It never changes the rule itself —
it's read-only, for informed tuning decisions.

## Tests

```bash
pytest
```

Covers the pattern rules and tolerance boundaries, S/R detection (including
confirmation lag and plateau edge cases), the validation gate (every failure
mode), boundary scheduling math, the sweep engine against a fake MEXC client
(forming-candle handling, evaluate-once, restart survival), the MEXC client
(mocked transport), annotation rendering, alert formatting, the duplicate
guard, and the end-to-end pipeline.

## Future expansion

New S/R methods (fractals, daily/weekly high-low, pivots) implement the
`SRDetector` interface; new strategies add their own rules module beside
`tamad_strategy.py` — the scanner, pipeline, Telegram, screenshot, and
persistence layers are strategy-agnostic. The stored signal snapshots are the
foundation for backtesting, win-rate statistics, and analytics dashboards.

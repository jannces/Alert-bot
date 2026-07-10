# Tamad Strategy Scanner

A 24/7 market scanner for **MEXC USDT Perpetual Futures** that detects valid
**Tamad Strategy** setups on TradingView and notifies you on **Telegram**
with a TradingView chart screenshot.

> **This is NOT a trading bot.** It never places, modifies, or closes orders
> and holds no exchange credentials. Its only outputs are Telegram messages
> and log entries. Every trade is analyzed and executed manually by you.
>
> **Accuracy over quantity.** Every rule is mandatory, every setup passes a
> final strict validation, and when in doubt the setup is rejected.

## How it works

```
TradingView (Pine v6)                    This service (Python 3.12)
┌──────────────────────────┐             ┌────────────────────────────────────┐
│ Tamad pattern detection  │   webhook   │ 1. authenticate shared secret      │
│ on MEXC:<SYMBOL>USDT.P   ├────────────►│ 2. re-validate EVERY rule from     │
│ 15m / 30m / 1h           │  JSON alert │    the raw OHLC of the 3 candles   │
│ (only after candle 3     │             │ 3. duplicate guard (SQLite)        │
│  fully closes)           │             │ 4. TradingView chart screenshot    │
│ + draws the full setup   │             │ 5. Telegram alert with the chart   │
│   on the chart           │             │ 6. log signal / rejection          │
└──────────────────────────┘             └────────────────────────────────────┘
```

TradingView is the single source of truth for candle data, pattern detection,
alerts, and chart visualization — the screenshot in every alert is the exact
TradingView chart you would analyze by hand. There is no custom chart UI.

## The Tamad Strategy (implemented exactly, no approximations)

A strict three-candle rejection pattern at meaningful support/resistance.

| | SHORT | LONG |
|---|---|---|
| Candle 1 | Green | Red |
| Candle 2 | Red, close **equal** to candle 1's close (default tolerance 0.05%) | Green, equal close |
| Level | The equal closes form the **resistance** | The equal closes form the **support** |
| Candle 3 | Green, fully **closed**. Wick may pierce the resistance, close must be ≤ resistance | Red, fully closed. Wick may pierce the support, close must be ≥ support |
| S/R filter | Pattern must sit at a meaningful level (swing high/low, previous day/week high/low, pivot) — middle-of-range patterns are rejected | same |
| Entry | Close of candle 3 | Close of candle 3 |
| Stop loss | **Highest wick** of the three candles | **Lowest wick** of the three candles |
| Targets | TP2 = 2R, TP3 = 3R | same |

The stop loss uses *only* the three strategy candles — never ATR, never
percentages, never indicators.

### Defense in depth

Pattern rules are enforced **twice**: once in Pine Script on TradingView, and
again in `strategy/validation.py`, which recomputes every rule from the raw
OHLC values carried in the webhook (candle colors, equal-close, third-candle
rule, S/R proximity and side, closed-candle confirmation, entry, stop, TP2,
TP3, signal freshness, symbol/timeframe scope). If **any** check fails, no
alert is sent and the rejection is logged with its reasons.

## Project layout

```
├── tradingview/
│   ├── pine_script.pine     # Pine v6: detection + chart drawings + webhook alert
│   └── webhook_handler.py   # FastAPI endpoint: auth, schema validation, enqueue
├── strategy/
│   ├── models.py            # Candle / TamadSetup / ValidationReport value objects
│   ├── tamad_strategy.py    # pure pattern rules (single source of truth)
│   ├── validation.py        # final strict validation gate
│   └── pipeline.py          # validate → dedup → screenshot → notify worker
├── telegram/
│   └── bot.py               # Telegram Bot API client + alert formatting
├── screenshots/
│   └── capture.py           # TradingView chart capture (Playwright / chart-img)
├── database/
│   └── repository.py        # SQLite: signals, rejections, duplicate guard
├── config/
│   ├── config.yaml          # all knobs, env-expanded (${VAR})
│   ├── settings.py          # typed config loading (pydantic)
│   └── logging_setup.py     # console + rotating file logs
├── tests/                   # unit + pipeline tests (pytest)
└── main.py                  # entrypoint
```

## Setup

### 1. Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # only for the playwright screenshot provider
```

### 2. Configure secrets

```bash
cp .env.example .env               # fill in the values
set -a; source .env; set +a
```

- `WEBHOOK_SECRET` — long random string; also entered in the Pine script input.
- `TELEGRAM_BOT_TOKEN` — create a bot with [@BotFather](https://t.me/BotFather).
- `TELEGRAM_CHAT_ID` — your chat/channel id.

All other knobs (timeframes, symbols whitelist, equal-close tolerance, swing
lookback, S/R sensitivity, R multiples, screenshot provider, logging level)
live in [`config/config.yaml`](config/config.yaml), documented inline.

### 3. TradingView

1. Open the Pine Editor, paste [`tradingview/pine_script.pine`](tradingview/pine_script.pine),
   **Save** and **Add to chart**, then save the chart layout.
2. In the indicator settings, set **Webhook Secret** to your `WEBHOOK_SECRET`
   and keep the other inputs identical to `config.yaml` (tolerance, level
   basis, swing lookback, S/R proximity).
3. Create an alert:
   - **Condition:** `Tamad Strategy Scanner` → *Any alert() function call*
   - **Options:** *Once per bar close*
   - **Webhook URL:** `https://<your-host>/webhook/tradingview`
4. Repeat for each MEXC symbol (`MEXC:BTCUSDT.P`, …) and timeframe
   (15m / 30m / 1h) you want scanned. TradingView requires one alert per
   symbol+timeframe; the number of simultaneous alerts depends on your
   TradingView plan. Prioritize the pairs you actually trade, or use
   watchlist-wide alerts if your plan supports them.

The indicator draws the complete setup on the chart — highlighted pattern
candles, S/R line, entry, stop loss, TP2, TP3 — so the screenshot contains
every annotation.

### 4. Screenshots

**Playwright (default).** Point `screenshots.playwright.chart_url` at your
saved TradingView layout (the one with the indicator applied), e.g.
`https://www.tradingview.com/chart/AbCdEfGh/`. For a private layout, export a
login session once:

```bash
python -m playwright codegen --save-storage=config/tv_storage_state.json https://www.tradingview.com
# log in in the opened browser, then close it
```

**chart-img.** Set `screenshots.provider: chart_img` and `CHART_IMG_API_KEY`
(no browser needed, requires a [chart-img.com](https://chart-img.com) key).

If a screenshot cannot be captured, the behavior is yours to choose via
`screenshots.on_failure`: `send_without_image` (default — the alert arrives
with a warning line) or `skip_alert` (strictest — no image, no alert).

### 5. Run

```bash
python main.py --config config/config.yaml
```

or with Docker (auto-restarts, state persisted on the host):

```bash
docker compose up -d --build
```

The webhook endpoint must be reachable from TradingView over HTTPS — put it
behind a reverse proxy (Caddy/nginx) or a tunnel. `GET /health` reports queue
and counter stats for monitoring.

## Reliability

- **No duplicates, ever:** each setup's identity (exchange, symbol,
  timeframe, direction, candle-3 bar time) is claimed atomically in SQLite
  *before* sending; the guard survives restarts and crashes.
- **No unfinished candles:** Pine fires only on confirmed bar close, and the
  validator independently confirms candle 3's close time has passed. Stale
  alerts (older than `max_alert_age_seconds`) are rejected.
- **Crash-proof worker:** webhook responds instantly; a background worker
  processes the queue and survives any per-alert error. Telegram sends are
  retried with backoff and honor rate limits; a failed send releases the
  dedup reservation so the alert can be replayed.
- **Full audit trail:** every sent signal and every rejection (with per-rule
  reasons) is stored in SQLite and logged to `logs/tamad.log` (rotating).

## Tests

```bash
pytest
```

Covers the pattern rules (colors, tolerance boundaries, third-candle rule,
doji rejection, trade levels), the strict validation gate (each failure
mode), the duplicate guard (including restart survival), webhook auth and
parsing, Telegram formatting, and the end-to-end pipeline.

## Future expansion

The layered design keeps additions localized: new strategies implement their
own rules module + validator alongside `strategy/tamad_strategy.py`; the
pipeline, Telegram, screenshots, and persistence layers are strategy-agnostic.
Planned directions: backtesting and win-rate statistics on the stored signal
history, multi-timeframe confirmation, volume/EMA/RSI filters, order-block /
liquidity-sweep / MSS / FVG detection, and Telegram commands (`/status`,
`/pairs`, `/settings`).

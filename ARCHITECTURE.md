# Tamad Strategy Scanner — Architecture (v2)

**Status: APPROVED — decisions D1–D4 resolved by the project owner; this
document is the implementation reference.**

Python is the single source of truth for all market scanning, strategy
detection, and validation. TradingView is strictly a charting and screenshot
service. The scanner never trades: no order placement, no position
management, no exchange credentials — it reads public market data and writes
Telegram messages, log lines, and database rows. Nothing else.

---

## 1. Decision record

| # | Decision |
|---|---|
| **D1** | Screenshots come from Playwright driving the owner's saved TradingView layout. All annotations (3-candle highlight, S/R, entry, SL, TP2, TP3, legend) are drawn **by Python (Pillow) onto the captured image**, so they always match the Python-validated signal. Pine overlays are **not** used for strategy visualization; a display-only Pine overlay remains available solely as a documented fallback if pixel calibration proves unreliable in the field (§7). |
| **D2** | `comparison_mode` supports `strict`, `midpoint`, and `outer` (added 2026-07, now the default — see §5). **`average` was removed** because with exactly two reference candles the arithmetic mean of the two closes *is* the midpoint — two names for one behavior invite config confusion without adding capability. |
| **D3** | Rejections are logged **only for near-miss candidates**: setups that passed the pattern pre-filter (candle colors + equal close) but failed one or more later rules. Each rejection stores pair, timeframe, detection time, rules passed, rules failed, OHLC of all three candles, and the failure reason. Plain "no pattern on this bar" is never persisted. |
| **D4** | The v1 TradingView-detection architecture is **deleted**: the detection Pine script, the webhook endpoint, and all webhook-payload handling. Git history preserves them. Only a minimal `/health` endpoint remains for monitoring. |

## 2. Pipeline (authoritative)

```
MEXC Futures API                 public read-only endpoints; no API key
        │
        ▼
Async Python Scanner             candle-close scheduler + rate-limited sweep
        │                        over every active USDT perpetual
        ▼
Tamad Strategy Validation        colors, equal close, third-candle rule —
        │                        pure Python, only fully closed candles
        ▼
Support/Resistance Validation    configurable detector (swing high/low first);
        │                        middle-of-range patterns rejected
        ▼
Risk Management Calculation      Entry = Candle 3 close · SL = extreme wick
        │                        of the 3 candles · Risk = |Entry − SL| ·
        │                        TP2 = 2R · TP3 = 3R
        ▼
Duplicate Check                  SQLite atomic claim; survives restarts
        │
        ▼
Generate Screenshot from         Playwright: saved layout, symbol/timeframe
TradingView                      via URL, wait for render, capture PNG
        │
        ▼
Overlay Entry / SL / TP /        Pillow, Python-drawn from the validated
Highlights                       signal values (§7)
        │
        ▼
Telegram Notification            alert + screenshot + validation checklist
        │                        + confidence + TradingView link
        ▼
SQLite Logging                   full signal snapshot / near-miss rejections
```

Everything before "Duplicate Check" runs continuously 24/7; everything after
runs per detected signal. A final strict validation gate re-runs the complete
rule set on the exact setup object immediately before the duplicate check —
if any rule fails, the setup is rejected and logged, never sent.

## 3. Module map

```
├── mexc/
│   └── client.py              Public futures market-data client (contract
│                              list + klines). Token-bucket rate limiter,
│                              retries with backoff + jitter. READ-ONLY.
├── scanner/
│   ├── scheduler.py           Fires at each 15m/30m/1h close boundary
│   │                          (+ settle delay); pure boundary math.
│   └── engine.py              Sweep: fetch candles → drop forming candle →
│                              skip processed bars → pre-filter → build
│                              setup → hand to pipeline.
├── strategy/
│   ├── models.py              Candle / SRLevel / TamadSetup / ValidationReport
│   ├── tamad_strategy.py      Pattern rules — the single source of truth
│   ├── sr_levels.py           S/R detection behind a common interface;
│   │                          swing_high_low now, fractals / daily / weekly /
│   │                          pivots pluggable later
│   ├── validation.py          Final strict pre-alert gate
│   └── pipeline.py            validate → dedup → screenshot → annotate →
│                              notify → persist
├── screenshots/
│   ├── capture.py             Playwright capture of the saved layout +
│   │                          best-effort pixel calibration (§7)
│   └── annotate.py            Pillow overlay: highlights, level lines, legend
├── tradingview/
│   ├── links.py               Chart-URL builder for alerts
│   └── pine_overlay.pine      Display-only fallback overlay (D1); OPTIONAL,
│                              draws only, never detects
├── telegram/bot.py            Alert formatting + Bot API delivery
├── database/repository.py     Signal snapshots, near-miss rejections,
│                              duplicate guard, per-bar scan state
├── config/                    config.yaml + typed settings + logging
├── tests/
└── main.py                    Wires everything; /health endpoint
```

Deleted per D4: `tradingview/pine_script.pine`, `tradingview/webhook_handler.py`.

## 4. Data acquisition

- **Symbol discovery:** `GET /api/v1/contract/detail` → all contracts;
  keep USDT-quoted perpetuals in normal trading state. Refreshed hourly
  (new listings picked up, delisted pairs dropped). Optional whitelist.
- **Klines:** `GET /api/v1/contract/kline/{symbol}` (`Min15`/`Min30`/`Min60`),
  fetching only `history_bars` (default 150) candles — enough for S/R
  context plus the 3 pattern candles. No bulk historical downloads.
- **Forming-candle handling:** any bar whose close time is after the sweep
  boundary is discarded. Candle 3 must be exactly the bar that closed at the
  boundary, verified by timestamp arithmetic; if the API hasn't published it
  yet, one short retry, then skip and log.
- **Evaluate once:** last processed bar per (symbol, timeframe) is persisted
  in SQLite, so each bar is evaluated at most once, including across restarts.
- **Rate limits:** token bucket (default 8 req/s, config) under MEXC's
  ~20 req/2 s public limit. ~750 symbols ≈ 95 s per timeframe sweep; worst
  case (hour boundary, all three TFs) ≈ 4–5 min, 15m sweep first. The
  freshness rule (`max_signal_age_seconds`, default 600) guarantees a slow
  sweep degrades to *no* alert rather than a stale one.

## 5. Strategy rules (100% Python)

- **Candle colors** — SHORT: green/red/green; LONG: red/green/red; dojis fail.
- **Equal close** — |close₁ − close₂| ≤ close₁ × `tolerance_percent` / 100
  (default 0.1%; raised from 0.05% by owner decision, 2026-07).
- **Level** (`comparison_mode`):
  - `outer` *(default; owner decision 2026-07)* — the far edge of the zone
    the two closes span (SHORT: higher close; LONG: lower). Candle 3 may
    close anywhere inside the equal-close zone. Adopted after a live BTC
    example where Candle 3 closed between the two closes — visually a
    textbook rejection — and `strict` refused it.
  - `midpoint` — `(close₁ + close₂) / 2`.
  - `strict` — the close Candle 3 is least allowed to break
    (SHORT: lower of the two closes; LONG: higher). Harshest reading.
  - *(`average` intentionally not offered — see D2.)*
- **Third candle rule** — wick may pierce the level; the close must not.
- **Entry** — Candle 3 close. Never next-candle open, market price, or midpoint.
- **Stop loss** — SHORT: highest high of candles 1–3; LONG: lowest low.
  No ATR, no indicators, no percentages, no volatility.
- **Targets** — Risk = |Entry − SL|; TP2 = Entry ∓ 2·Risk; TP3 = Entry ∓ 3·Risk.
- **Pre-filter vs full validation:** the sweep pre-filter is colors +
  equal-close (defines a *candidate* and the near-miss logging boundary, D3);
  every candidate then passes through the full validator.

## 6. Support / Resistance (configurable, never guessed)

```yaml
support_resistance:
  enabled: false          # optional filter — OFF by default (owner decision,
                          # 2026-07: it rejected too many otherwise-valid
                          # patterns; re-enable any time)
  method: swing_high_low
  left_bars: 20
  right_bars: 20
  proximity_percent: 0.25
```

When disabled, the engine skips level detection, the validator skips the
S/R checks, and the Telegram checklist shows 6 rules instead of 7. When
enabled:

`SRDetector.find_levels(candles) -> list[SRLevel]` is the interface;
`swing_high_low` (a bar strictly exceeding `left_bars` highs before and
`right_bars` highs after it, mirrored for lows) is the first implementation.
Fractals, daily/weekly high-low, and pivot points plug in behind the same
interface later. For a SHORT the pattern level must sit within
`proximity_percent` of a confirmed *high*-side level; LONG mirrors with lows.

Inherent, non-repainting limitation: with `right_bars: 20` a swing confirms
20 bars after it prints, so very recent extremes are not yet levels.

## 7. Screenshots & Python-drawn annotations (D1)

Capture flow: headless Chromium with a stored TradingView login session →
open the saved layout with `?symbol=MEXC:<SYM>&interval=<TF>` → wait for the
canvas plus a settle delay → screenshot the chart pane.

**Annotation flow (Python-drawn, Pillow):** to place price-anchored lines on
the captured image, the pixel↔price/time mapping is derived at capture time
by *calibration*:

1. **Price axis:** hover the crosshair at two known y-pixels and read the
   crosshair price from the page; two (pixel, price) points give the linear
   y-mapping. (Requires a linear price scale on the layout — log scale
   breaks the linear fit and is detected/refused.)
2. **Time axis:** hover along the bar row and match the OHLC legend readout
   against Candle 3's known values to find Candle 3's x-pixel and bar width.
3. With the mapping, Pillow draws onto the PNG: translucent highlight box
   over the three pattern candles, horizontal lines with price tags for S/R,
   Entry, SL, TP2, TP3, and a legend panel with the full trade details.

**Fallback chain (automatic, per capture):** TradingView's page internals are
unversioned, so calibration is best-effort by design. If any step fails, the
capture degrades gracefully and says so in logs:

1. `python_overlay` — full annotations (default).
2. `legend_only` — clean chart + Pillow legend panel with all validated
   numbers (always works; levels as text, not positioned lines).
3. If field experience shows calibration chronically unreliable, the
   display-only `tradingview/pine_overlay.pine` can be added to the saved
   layout as a visual fallback (D1). It draws only — detection remains 100%
   Python — and any drawing it makes is cosmetic, never authoritative.

Known limitations, stated plainly: TradingView sessions expire and captchas
can appear (mitigated by a session health check and a Telegram operational
warning); automating tradingview.com is a ToS gray area; captures take
~5–15 s per signal and are serialized; TradingView renders its own MEXC feed,
which can differ from the MEXC API on rare boundary ticks — the authoritative
numbers are always the Python-validated ones in the message and overlay.
`on_failure` policy for a completely failed capture: `send_without_image`
(default) or `skip_alert`.

## 8. Telegram alert

```
🚨 TAMAD STRATEGY

Pair:            BTCUSDT
Direction:       SHORT
Timeframe:       15m

Entry:           118,250
Stop Loss:       118,730
Take Profit 2R:  117,290
Take Profit 3R:  116,810
Risk Reward:     1:2 / 1:3

Detection Time:  2026-07-10 14:30:05 UTC

Validation
✅ Candle 1 Color
✅ Candle 2 Color
✅ Candle 3 Rule
✅ Equal Close
✅ Support / Resistance
✅ Stop Loss
✅ Take Profit

Confidence
7 / 7 Rules Passed

📊 https://www.tradingview.com/chart/?symbol=MEXC:BTCUSDT.P&interval=15
```

Screenshot attached as the photo, this text as the caption. The checklist is
rendered from the actual validation report (an alert only exists at 7/7 —
anything less was rejected upstream). The TradingView link always opens the
live chart with symbol and interval pre-selected.

## 9. Persistence

**signals** — full snapshot per sent alert (backtesting-ready by design):
identity (dedup key, exchange, pair, timeframe, direction), full OHLC + open
time of Candles 1–3, equal-close level, S/R kind + price, entry, SL, risk,
TP2, TP3, detection/sent timestamps, screenshot file path, and the serialized
validation report.

**rejections** (near-miss only, D3): pair, timeframe, direction, detection
time, passed rules, failed rules, failure reason, OHLC of the three candles.
Example: `BTCUSDT | 15m | REJECTED | third_candle_rule: candle 3 closed
above resistance`.

**scan_state** — last processed bar per (symbol, timeframe): the
evaluate-once guarantee across restarts.

**Duplicate protection** (unchanged): dedup key
`(exchange, symbol, timeframe, direction, candle3 open time)` claimed
atomically *before* sending; survives restarts; a failed Telegram send
releases the claim so the signal can be retried, a sent one can never repeat.

## 10. Configuration

```yaml
exchange: { name: MEXC, tv_prefix: MEXC }

scanner:
  timeframes: ["15", "30", "60"]
  symbols: { mode: all, whitelist: [] }
  history_bars: 150
  boundary_settle_seconds: 5
  max_signal_age_seconds: 600
  clock_skew_seconds: 30

mexc:
  base_url: https://contract.mexc.com
  requests_per_second: 8
  max_concurrency: 8
  retries: 3

strategy:
  equal_close:
    tolerance_percent: 0.1
    comparison_mode: outer           # outer | midpoint | strict
  support_resistance:
    method: swing_high_low
    left_bars: 20
    right_bars: 20
    proximity_percent: 0.25
  risk_reward_targets: [2.0, 3.0]

screenshots:
  provider: playwright               # playwright | disabled
  annotations: python_overlay        # python_overlay | legend_only | none
  on_failure: send_without_image     # send_without_image | skip_alert
  output_dir: screenshots/out
  playwright:
    chart_url: https://www.tradingview.com/chart/     # your saved layout URL
    storage_state_path: config/tv_storage_state.json
    render_wait_seconds: 6

telegram: { bot_token: ${TELEGRAM_BOT_TOKEN}, chat_id: ${TELEGRAM_CHAT_ID} }
database: { path: database/tamad.sqlite3 }
logging:  { level: INFO, file: logs/tamad.log }
```

## 11. Reliability

- Scheduler, sweeps, capture, and delivery are isolated `asyncio` tasks; any
  per-symbol or per-signal exception is logged and never stops the loop.
- Exponential backoff + jitter on MEXC and Telegram; an unrecoverable sweep
  is abandoned (freshness makes late processing pointless) and the next
  boundary starts clean.
- Process supervision via Docker `restart: unless-stopped` or systemd;
  `/health` reports last sweep per timeframe, queue/counters, uptime.

## 12. Testing

Pure-logic modules (pattern rules, S/R detection, validation, boundary math,
annotation rendering, message formatting, repository) are unit-tested
directly; the sweep engine is tested against a fake MEXC client (forming
candle dropped, evaluate-once, near-miss logging); the pipeline end-to-end
with stubbed capture/Telegram. Live TradingView calibration is intentionally
outside unit-test scope — it is guarded by the runtime fallback chain instead.

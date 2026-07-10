# Tamad Strategy Scanner — Revised Architecture (v2)

**Status: PROPOSED — awaiting review. No implementation has started.**

This document describes the revision from the v1 design (TradingView detects,
Python validates) to the v2 design (Python detects everything, TradingView is
display-only). It also lists the technical limitations found during design —
especially around Playwright-driven TradingView screenshots — and the
practical alternatives where a requirement cannot be met reliably.

---

## 1. Principles (unchanged)

- **Never trades.** No order placement, no position management, no account
  credentials. The scanner reads public market data and writes Telegram
  messages, log lines, and database rows. Nothing else.
- **Accuracy over quantity.** Every rule is mandatory. Any failed rule
  rejects the setup. When in doubt, reject.
- **Only fully closed candles.** A setup is evaluated only after Candle 3 has
  completely closed.

## 2. What changes vs v1

| Concern | v1 (current code) | v2 (this proposal) |
|---|---|---|
| Candle data | TradingView (via webhook payload) | **MEXC public futures API** (read-only, no API key) |
| Pattern detection | Pine Script v6 | **100% Python** |
| S/R detection | Pine Script | **100% Python** (configurable methods) |
| Trigger | TradingView alert webhooks (one alert per symbol/TF, plan-limited) | **Python scheduler on candle close** — scans *every* active USDT perp with no TradingView plan limits |
| Entry/SL/TP | Pine computes, Python re-validates | **Python computes** (single source of truth) |
| TradingView's role | Detection + chart + webhook | **Chart visualization + screenshot only** |
| Webhook server | FastAPI `/webhook/tradingview` | **Removed.** Only `/health` remains for monitoring |

Removing the webhook dependency also removes the v1 architecture's biggest
practical constraint: TradingView cannot natively scan "all MEXC USDT perps"
— it needed one configured alert per symbol per timeframe. In v2, Python
discovers and scans every active contract itself.

## 3. Revised workflow

```
                     ┌──────────────────────────────────────────────┐
                     │ every 15m / 30m / 1h candle-close boundary   │
                     └──────────────────────┬───────────────────────┘
                                            ▼
MEXC public API ──► Symbol discovery (all active USDT perpetuals, refreshed hourly)
        │
        ▼
Python Scanner ──── rate-limited async sweep: fetch last N closed candles
        │           per (symbol, timeframe); forming candle discarded;
        │           already-processed bars skipped
        ▼
Tamad Strategy Validation ── candle colors, equal close, third-candle rule,
        │                    S/R detection + proximity, sanity checks
        ▼
Duplicate Check ──── SQLite, atomic claim, survives restarts
        │
        ▼
Generate Trade Details ── Entry = Candle 3 close
        │                 SL = extreme wick of the 3 candles
        │                 Risk = |Entry − SL|; TP2 = 2R; TP3 = 3R
        ▼
Open TradingView Chart ── Playwright: load saved layout, switch symbol
        │                 and timeframe via URL, wait for render
        ▼
Capture Screenshot ── PNG saved to disk (path stored with the signal)
        │
        ▼
Telegram Bot ──── alert with screenshot, validation checklist,
        │         confidence score, and a TradingView chart link
        ▼
Signal snapshot persisted ── pair, TF, OHLC of all 3 candles, entry, SL,
                             TP2, TP3, S/R details, screenshot path,
                             timestamps → future backtesting/statistics
```

Every step after "Duplicate Check" is per-signal; everything before is the
continuous 24/7 scan loop.

## 4. Module map

Kept modular; only the responsibilities move. New modules marked **NEW**,
removed marked ~~struck~~.

```
├── mexc/                          NEW
│   └── client.py                  Public futures market-data client:
│                                  contract list + klines. Token-bucket rate
│                                  limiter, retries with backoff + jitter.
│                                  READ-ONLY — no auth, no trading endpoints.
├── scanner/                       NEW
│   ├── scheduler.py               Fires a sweep at each timeframe boundary
│   │                              (+ small settle delay); tracks last
│   │                              processed bar per (symbol, TF).
│   └── engine.py                  Per-symbol scan: fetch candles → detect
│                                  pattern → S/R filter → hand to pipeline.
├── strategy/
│   ├── models.py                  Candle / TamadSetup / ValidationReport
│   ├── tamad_strategy.py          Pattern rules (kept — already pure Python)
│   ├── sr_levels.py               NEW  S/R detection: swing high/low now;
│   │                              fractals, daily/weekly H-L, pivots later
│   │                              behind the same interface.
│   ├── validation.py              Final pre-alert gate (kept, adapted)
│   └── pipeline.py                dedup → screenshot → notify → persist
├── screenshots/
│   └── capture.py                 Playwright: saved layout, symbol/TF via
│                                  URL, render wait, capture. Annotation
│                                  strategy per §7.
├── tradingview/
│   ├── links.py                   NEW  chart-URL builder for alerts
│   ├── pine_overlay.pine          NEW (optional) display-only companion
│   │                              indicator — draws, never decides (§7)
│   ├── ~~pine_script.pine~~       REMOVED (detection moves to Python)
│   └── ~~webhook_handler.py~~     REMOVED (no inbound webhooks; a minimal
│                                  /health endpoint moves to main.py)
├── telegram/bot.py                Alert format per §8
├── database/repository.py         Signals (full snapshots), rejections,
│                                  duplicate guard, scan-progress state
├── config/                        settings.py + config.yaml (§10)
├── tests/
└── main.py                        Wires scheduler + pipeline + health server
```

The strategy rules in `strategy/tamad_strategy.py` were already implemented
as pure Python functions in v1 (they were the re-validation layer). In v2
they are promoted from "verifier" to "detector" — same code, same tests, now
the single source of truth.

## 5. Detection engine (100% Python)

### 5.1 Data acquisition

- **Symbol discovery:** `GET /api/v1/contract/detail` on the MEXC futures
  API → all contracts; keep those quoted/settled in USDT and in a tradable
  state. Refreshed hourly (new listings picked up automatically; delisted
  pairs dropped). Optional whitelist still supported.
- **Klines:** `GET /api/v1/contract/kline/{symbol}` with `Min15` / `Min30` /
  `Min60`. Per sweep, fetch only `history_bars` (default 150) candles — the
  minimum needed for S/R context (left/right swing bars) plus the 3 pattern
  candles. No bulk historical downloads.
- **Forming candle handling:** the newest kline row is the live bar — always
  discarded. Candle 3 is the newest *closed* bar, cross-checked by timestamp
  arithmetic against the sweep boundary. If the API is lagging (candle-3
  timestamp not yet present), the symbol is retried within the same sweep,
  then skipped and logged.
- **Newly-closed only:** last processed bar time per (symbol, timeframe) is
  tracked in SQLite; a bar is evaluated at most once — also across restarts.

### 5.2 Rate limits and sweep timing

MEXC's public futures endpoints allow roughly 20 requests / 2 s per IP per
endpoint. Design numbers:

- ~750 active USDT perps × 1 kline request per sweep.
- Token bucket at a conservative **8 req/s** (config) → ~95 s per timeframe
  sweep.
- Worst case at a full-hour boundary all three timeframes close at once:
  ~2,250 requests ≈ **4–5 minutes** for the complete triple sweep,
  interleaved so the 15m sweep (most time-sensitive) completes first.
- Signal freshness is enforced: any setup whose Candle 3 closed more than
  `max_signal_age_seconds` ago (default 600 s) is rejected as stale, so a
  slow sweep can never produce misleadingly late alerts. This freshness
  budget and the sweep time must be kept consistent in config.

### 5.3 Strategy rules (unchanged semantics, Python only)

- **Candle colors** — SHORT: green/red/green; LONG: red/green/red. Dojis
  always fail.
- **Equal close** — |close₁ − close₂| within `tolerance_percent` of close₁.
- **Level derivation** (`comparison_mode`, see §5.4 open question):
  the equal closes form the resistance (SHORT) / support (LONG).
- **Third candle rule** — wick may pierce the level; close must not.
- **Entry** — **Candle 3 close.** Never next-candle open, never market
  price, never midpoint.
- **Stop loss** — SHORT: highest high of candles 1–3; LONG: lowest low of
  candles 1–3. No ATR, no indicators, no percentages, no volatility.
- **Targets** — Risk = |Entry − SL|; TP2 = Entry ∓ 2 × Risk;
  TP3 = Entry ∓ 3 × Risk. Both displayed.
- **S/R filter** — the pattern level must sit within `proximity_percent` of
  a *confirmed* structural level (§6). Middle-of-range patterns rejected.
- **Final validation gate** — before any alert, the complete rule set is
  re-run one last time on the exact setup object that will be sent
  (identical to v1's `FinalValidator`, minus the now-obsolete
  "does TradingView's payload match?" cross-checks).

### 5.4 Equal-close configuration

```yaml
equal_close:
  tolerance_percent: 0.05
  comparison_mode: strict      # strict | midpoint | average
```

Proposed semantics (please confirm — see Open Decisions):

- `strict` *(default)* — the level is the close Candle 3 is **least** allowed
  to break (SHORT: the lower of the two closes; LONG: the higher). Strictest
  possible reading; when in doubt, reject.
- `midpoint` — the level is `(close₁ + close₂) / 2`.
- `average` — with exactly two reference candles this is numerically the
  same as `midpoint`; it is accepted as a config value (reserved for future
  patterns that average more than two closes) and behaves like `midpoint`.
  Flagged rather than silently invented — see Open Decision D2.

## 6. Support / Resistance (configurable, never guessed)

```yaml
support_resistance:
  method: swing_high_low       # the only method implemented initially
  left_bars: 20
  right_bars: 20
  proximity_percent: 0.25
```

- `strategy/sr_levels.py` defines a small interface
  (`SRDetector.find_levels(candles) -> list[SRLevel]`, where `SRLevel` has a
  price, side, kind, and the bar it was confirmed at). `swing_high_low` is
  the first implementation; **fractals, daily high/low, weekly high/low, and
  pivot points** plug in behind the same interface later without touching
  the scanner or pipeline.
- A swing high is a bar whose high exceeds the `left_bars` highs before it
  and the `right_bars` highs after it (mirror for swing lows). Only
  **confirmed** swings are used.
- **Inherent limitation (not a bug):** with `right_bars: 20`, a swing is
  only confirmed 20 bars after it prints. The S/R level a pattern reacts to
  is therefore always at least `right_bars` old. That is the correct,
  non-repainting behavior — but it means very recent highs/lows are not yet
  "levels". Lower `right_bars` for more responsive (and noisier) levels.

## 7. TradingView screenshots — capabilities and limits (READ THIS)

Playwright drives a real browser against your saved TradingView layout:

1. Launch headless Chromium with a stored login session
   (`storage_state.json`, exported once via a helper command).
2. Open `https://www.tradingview.com/chart/<your-layout-id>/?symbol=MEXC:<SYM>&interval=<TF>`
   — the symbol and timeframe switch via URL parameters; the layout's saved
   appearance (colors, scales, your zoom level ≈ last 50–100 candles) is
   preserved.
3. Wait for the chart canvas to render plus a configurable settle delay.
4. Screenshot the chart container; save PNG to `screenshots/out/` and record
   the path in the signal snapshot.

### What is technically NOT reliable — and the honest alternatives

**T1. Drawing annotations programmatically on tradingview.com is not
dependable.** TradingView's chart is a closed canvas application with no
public browser-side API. There is no supported way for Playwright to say
"draw a line at price 118,730". Simulating mouse-drawn trendlines requires a
price→pixel mapping that is not exposed (the price axis is also canvas), so
coordinate-based drawing breaks on any zoom/scale difference and any UI
update. **I will not build the alert path on top of that.** Three practical
options instead, selectable in config:

| Option | How the required annotations (3 candles, S/R, Entry, SL, TP2, TP3) get on the image | Trade-off |
|---|---|---|
| **A. `pine_overlay`** (default) | A *display-only* Pine indicator on your saved layout re-marks the pattern with the same deterministic rules and draws level lines. It makes **zero decisions** — Python remains the sole detector; Pine only paints. | Free, native TradingView look. Rare risk: TradingView's MEXC feed differing from the MEXC API on a boundary tick could make the overlay not paint a setup Python alerted (the alert itself is unaffected — the message always carries all prices). |
| **B. `legend`** | Clean layout screenshot + a Python-rendered (Pillow) legend panel stamped onto the image: direction, entry, SL, TP2, TP3, S/R, the three candle timestamps. | 100% consistent with Python's numbers, zero TradingView coupling — but levels are text in a panel, **not horizontal lines positioned on the price scale** (impossible without the price→pixel mapping, see above). |
| **C. `chart_img`** | chart-img.com's TradingView snapshot API accepts price-anchored drawing primitives — Python's exact entry/SL/TP/S-R values become real lines/zones on a TradingView-rendered chart. | The only option with *exact, Python-positioned* line annotations. Requires a paid API key; external dependency. |

Recommendation: **A + B combined** as default (overlay for visuals, legend
stamp for guaranteed numeric accuracy), with **C** available when exact
drawn levels matter more than the free tier.

**T2. Automated login sessions are fragile.** TradingView sessions expire
and occasionally trigger captchas; headless browsers can be flagged.
Mitigations: persistent `storage_state`, session-health check at startup and
before each capture, automatic re-try, and a Telegram *operational* warning
("session expired — re-export storage state") so you learn about it
immediately, not from silently image-less alerts. Also note plainly:
automating the TradingView website sits in a gray zone of their ToS; option
C (chart-img) is the fully sanctioned route if that concerns you.

**T3. Screenshot latency.** A browser capture takes ~5–15 s per signal.
Signals are queued, so a burst of simultaneous setups delays later
screenshots; alerts still go out in order. `on_failure` policy stays:
`send_without_image` (default) or `skip_alert`.

**T4. Data-feed nuance.** Python detects on MEXC's official API data;
TradingView renders its own MEXC feed. They agree in practice, but the
authoritative numbers are always the ones in the Telegram text, which come
from the API data Python validated.

## 8. Telegram alert format

```
🚨 TAMAD STRATEGY

Pair:        BTCUSDT
Direction:   SHORT
Timeframe:   15m

Entry:       118,250
Stop Loss:   118,730
TP2 (2R):    117,290
TP3 (3R):    116,810
Risk Reward: 1:2 / 1:3

Detection Time: 2026-07-10 14:30:05 UTC

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

📊 Open live chart:
https://www.tradingview.com/chart/?symbol=MEXC:BTCUSDT.P&interval=15
```

- Screenshot attached as the photo, this text as the caption.
- The checklist reflects the actual `ValidationReport` (it will always read
  7/7 — anything less is rejected before reaching Telegram; the checklist
  is confirmation of *what* was verified).
- The TradingView link is always included, with symbol and interval
  pre-selected.

## 9. Logging & persistence

**Valid signals** — full snapshot stored on send (your requested feature,
included from the start):

| field group | contents |
|---|---|
| identity | dedup key, exchange, pair, timeframe, direction |
| pattern | full OHLC + open-time of Candles 1, 2, 3 |
| levels | equal-close level, S/R type + level, entry, SL, risk, TP2, TP3 |
| delivery | detection time, sent time, screenshot file path, screenshot attached y/n |
| audit | serialized validation report (all 7 checks) |

This table is the future backtesting/win-rate dataset; nothing in the core
scanner needs to change to build analytics on top of it.

**Rejected signals** — every candidate that matched the color+equal-close
pre-filter but failed any rule is stored with per-rule reasons, e.g.
`BTCUSDT | REJECTED | third_candle_rule: candle 3 closed above resistance`.
(Note: "rejected" rows are logged for *near-miss candidates*, not for every
symbol scanned — logging all ~750 non-matches per sweep would be noise.)

**Operational logs** — structured, rotating: sweep timings, API failures,
rate-limit backoffs, screenshot/session failures, Telegram delivery.

## 10. Configuration sketch (v2 `config/config.yaml`)

```yaml
exchange:
  name: MEXC
  tv_prefix: MEXC

scanner:
  timeframes: ["15", "30", "60"]
  symbols: { mode: all, whitelist: [] }
  history_bars: 150
  boundary_settle_seconds: 5
  max_signal_age_seconds: 600

mexc:
  base_url: https://contract.mexc.com
  requests_per_second: 8
  max_concurrency: 8
  retries: 3

strategy:
  equal_close:
    tolerance_percent: 0.05
    comparison_mode: strict          # strict | midpoint | average
  support_resistance:
    method: swing_high_low           # fractals / daily_hl / weekly_hl / pivots later
    left_bars: 20
    right_bars: 20
    proximity_percent: 0.25
  risk_reward_targets: [2.0, 3.0]

screenshots:
  provider: playwright               # playwright | chart_img | disabled
  annotations: pine_overlay_plus_legend   # pine_overlay | legend | chart_img
  on_failure: send_without_image     # send_without_image | skip_alert
  output_dir: screenshots/out
  playwright: { chart_url: ..., storage_state_path: ..., render_wait_seconds: 6 }

telegram: { bot_token: ${TELEGRAM_BOT_TOKEN}, chat_id: ${TELEGRAM_CHAT_ID} }
database: { path: database/tamad.sqlite3 }
logging:  { level: INFO, file: logs/tamad.log }
```

## 11. Reliability (24/7, self-recovering)

- Scheduler, scanner sweeps, screenshot capture, and Telegram delivery are
  isolated `asyncio` tasks; any per-symbol or per-signal exception is
  caught, logged, and never stops the loop.
- Network interruptions: exponential backoff with jitter on MEXC and
  Telegram calls; a fully failed sweep is abandoned (freshness rule makes
  late processing pointless) and the next boundary starts clean.
- Duplicate protection unchanged from v1: dedup key
  `(exchange, symbol, timeframe, direction, candle3 open time)` claimed
  atomically in SQLite *before* sending; survives restarts; a failed
  Telegram send releases the claim so the signal isn't lost.
- Process supervision via Docker `restart: unless-stopped` (or systemd).
- `/health` endpoint kept (uptime, last sweep per TF, queue depth, counters).

## 12. Testing plan

- Keep all passing v1 rule/validation/dedup/telegram tests (they test pure
  Python logic that is unchanged).
- New: S/R swing detection (synthetic series with known swings; confirmation
  lag; proximity edge cases), sweep engine with a fake MEXC client (forming
  candle dropped, bar processed exactly once, restart resume), rate limiter,
  scheduler boundary math, alert formatting (checklist + link), snapshot
  persistence round-trip.

## 13. Open decisions — please confirm before implementation

- **D1 — Annotation strategy default.** Proposed: Pine display-only overlay
  **plus** stamped legend (option A+B, free), with chart-img (option C) as a
  config switch for exact drawn levels. OK?
- **D2 — `average` comparison mode.** With two candles, `average` ≡
  `midpoint`. Implement it as an accepted alias with identical behavior
  (documented), or drop it from the enum until a pattern exists where it
  differs?
- **D3 — Rejection logging scope.** Proposed: persist rejections only for
  *near-miss candidates* (passed candle-colors + equal-close pre-filter,
  failed a later rule). Logging every non-matching symbol/bar (~216k rows/day)
  would drown the debugging signal. OK?
- **D4 — v1 webhook removal.** The FastAPI webhook endpoint and the
  detection Pine script are deleted outright (git history preserves them).
  Any reason to keep a webhook fallback?

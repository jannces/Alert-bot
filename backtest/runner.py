"""Backtest runner: compare confirmation filters on real MEXC history.

Usage (from the project root; the machine must reach contract.mexc.com):

    python -m backtest.runner                       # 90 days, 15m, majors
    python -m backtest.runner --days 60 --tf 60
    python -m backtest.runner --symbols BTC_USDT ETH_USDT SOL_USDT

Prints one row per filter variant: signal count, TP2/TP3 win rates and
expectancies (in R), and timeouts. Breakeven win rates before costs are
33.4% (2R) and 25.0% (3R). Read the caveats printed at the end before
drawing conclusions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from backtest import filters as f
from backtest.data import HistoricalDataLoader
from backtest.simulator import Outcome, VariantResult, detect_signals, simulate_exit
from config.settings import MexcSettings
from mexc.client import MexcClient
from strategy.models import Candle
from strategy.tamad_strategy import ComparisonMode

logger = logging.getLogger(__name__)

# Liquid crypto USDT perpetuals (deliberately no tokenized stocks/indices —
# those are barely-moving instruments the live scanner shouldn't alert on).
DEFAULT_SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "XRP_USDT", "DOGE_USDT",
    "BNB_USDT", "ADA_USDT", "LINK_USDT", "AVAX_USDT", "SUI_USDT",
    "LTC_USDT", "DOT_USDT", "NEAR_USDT", "APT_USDT", "ARB_USDT",
    "OP_USDT", "ATOM_USDT", "FIL_USDT", "UNI_USDT", "AAVE_USDT",
    "TON_USDT", "TRX_USDT", "BCH_USDT", "ETC_USDT", "INJ_USDT",
    "SEI_USDT", "TIA_USDT", "WLD_USDT", "ORDI_USDT", "PEPE_USDT",
    "SHIB_USDT", "WIF_USDT", "JUP_USDT", "RENDER_USDT", "FET_USDT",
    "GALA_USDT", "SAND_USDT", "CRV_USDT", "LDO_USDT", "ENA_USDT",
]

# Bars of history each signal must have behind it so every variant (incl.
# EMA-200) can be evaluated — keeps the comparison apples-to-apples.
WARMUP_BARS = 210


def build_variants() -> dict[str, list[f.ConfirmationFilter]]:
    return {
        "baseline (no confirmations)": [],
        "wick sweep": [f.wick_sweep],
        "EMA-200 trend": [f.make_ema_trend(200)],
        "volume surge x1.5": [f.make_volume_surge(1.5, 20)],
        "RSI extreme 60/40": [f.make_rsi_extreme(14, 60.0, 40.0)],
        "min range 0.10%": [f.make_min_range(0.10)],
        "S/R swing 20/20 @0.25%": [f.make_sr_filter(20, 20, 0.25)],
        "wick sweep + EMA-200": [f.wick_sweep, f.make_ema_trend(200)],
        "wick sweep + min range": [f.wick_sweep, f.make_min_range(0.10)],
        "wick + EMA-200 + min range": [
            f.wick_sweep, f.make_ema_trend(200), f.make_min_range(0.10),
        ],
        "wick + volume surge": [f.wick_sweep, f.make_volume_surge(1.5, 20)],
    }


def run_variants(
    data: dict[str, list[Candle]],
    tolerance_pct: float,
    mode: ComparisonMode,
    horizon_bars: int,
    optimistic: bool = False,
) -> list[VariantResult]:
    variants = build_variants()
    results = {name: VariantResult(name) for name in variants}

    for symbol, candles in data.items():
        for signal in detect_signals(candles, tolerance_pct, mode, WARMUP_BARS):
            history = candles[: signal.index + 1]
            ctx = f.FilterContext(
                candles=history,
                direction=signal.direction,
                level=signal.level,
                levels=signal.levels,
            )
            outcome_2r, _ = simulate_exit(
                candles, signal.index, signal.direction,
                signal.levels.stop_loss, signal.levels.tp2, horizon_bars,
                optimistic=optimistic,
            )
            outcome_3r, _ = simulate_exit(
                candles, signal.index, signal.direction,
                signal.levels.stop_loss, signal.levels.tp3, horizon_bars,
                optimistic=optimistic,
            )
            for name, confirmations in variants.items():
                if not all(check(ctx) for check in confirmations):
                    continue
                r = results[name]
                r.signals += 1
                if outcome_2r is Outcome.TIMEOUT:
                    r.timeouts_2r += 1
                else:
                    r.resolved_2r += 1
                    r.wins_2r += outcome_2r is Outcome.WIN
                if outcome_3r is not Outcome.TIMEOUT:
                    r.resolved_3r += 1
                    r.wins_3r += outcome_3r is Outcome.WIN
    return list(results.values())


def print_report(results: list[VariantResult], days: int, tf: int, symbols: int) -> None:
    print(f"\nTamad confirmation backtest — {symbols} symbols, {tf}m, last {days} days")
    print("(2R breakeven win rate: 33.4%; 3R breakeven: 25.0% — before fees)\n")
    header = (
        f"{'variant':<28} {'signals':>7} {'WR@2R':>7} {'exp@2R':>7} "
        f"{'WR@3R':>7} {'exp@3R':>7} {'t/o':>5}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        wr2 = "  n/a" if r.win_rate_2r is None else f"{r.win_rate_2r:6.1%}"
        wr3 = "  n/a" if r.win_rate_3r is None else f"{r.win_rate_3r:6.1%}"
        e2 = "  n/a" if r.expectancy_2r is None else f"{r.expectancy_2r:+7.2f}"
        e3 = "  n/a" if r.expectancy_3r is None else f"{r.expectancy_3r:+7.2f}"
        print(
            f"{r.name:<28} {r.signals:>7} {wr2:>7} {e2:>7} {wr3:>7} {e3:>7} "
            f"{r.timeouts_2r:>5}"
        )

    print(
        "\nCaveats: gross results (no fees/funding/slippage); intrabar\n"
        "stop-vs-target ambiguity resolved as a LOSS (conservative); one\n"
        "market regime only; small signal counts (<50) are noise, not proof.\n"
        "A variant is interesting when expectancy is positive AND the signal\n"
        "count is large enough to trade regularly."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--tf", type=int, default=15, choices=[15, 30, 60])
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    parser.add_argument("--tolerance", type=float, default=0.1,
                        help="equal-close tolerance %% (match config.yaml)")
    parser.add_argument(
        "--mode", default="outer", choices=["strict", "midpoint", "outer"]
    )
    parser.add_argument("--horizon", type=int, default=400,
                        help="bars before an unresolved trade times out")
    parser.add_argument(
        "--ambiguity", default="conservative",
        choices=["conservative", "optimistic"],
        help="same-bar stop+target resolution; run both to bracket the truth",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = MexcClient(MexcSettings())
    loader = HistoricalDataLoader(client)
    data: dict[str, list[Candle]] = {}
    try:
        for symbol in args.symbols:
            try:
                data[symbol] = await loader.load(symbol, args.tf, args.days)
            except Exception as exc:  # noqa: BLE001 - skip unfetchable symbols
                logger.warning("skipping %s: %s", symbol, exc)
    finally:
        await client.aclose()

    if not data:
        raise SystemExit("no data loaded — is contract.mexc.com reachable?")

    results = run_variants(
        data, args.tolerance, ComparisonMode(args.mode), args.horizon,
        optimistic=args.ambiguity == "optimistic",
    )
    print(f"\n[mode={args.mode}, tolerance={args.tolerance}%, ambiguity={args.ambiguity}]")
    print_report(results, args.days, args.tf, len(data))


if __name__ == "__main__":
    asyncio.run(main())

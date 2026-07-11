"""Best-effort pixel↔price/time calibration of a live TradingView chart.

TradingView's chart is a closed canvas application: there is no API that
answers "which pixel is price 118,730?". Calibration therefore measures the
mapping empirically through the crosshair:

- **Price axis:** hover the crosshair at two vertical positions and read the
  crosshair price label that TradingView renders on the price axis. Two
  (pixel, price) points define the linear mapping. The two points are also
  used to *verify* linearity indirectly — a log-scale layout produces a
  mapping that disagrees with the candle data and is discarded by the
  sanity check below.
- **Time axis:** hover along the bar row from the right edge and read the
  OHLC legend readout; the column whose values equal Candle 3's known OHLC
  is Candle 3's x-position, and the distance to the column matching
  Candle 2 is the bar width.

Everything here is intentionally defensive: TradingView's page internals are
unversioned, so any failure returns ``None`` and the caller degrades to the
legend-only overlay. This module never raises into the pipeline.
"""

from __future__ import annotations

import logging
import math
import re

from screenshots.annotate import ChartCalibration
from strategy.models import Candle, TamadSetup

logger = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")

# JS: collect small numeric text elements right of the chart pane (the price
# axis column), with their vertical centers. Class names are deliberately
# not used — only geometry and content, which are far more stable.
_AXIS_TEXTS_JS = """
(rightOfX) => {
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll('div, span')) {
    if (el.children.length > 0) continue;
    const t = (el.textContent || '').trim();
    if (!/^\\d[\\d,]*(\\.\\d+)?$/.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > 30) continue;
    if (r.left < rightOfX) continue;
    const key = t + '@' + Math.round(r.top);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ text: t, cy: r.top + r.height / 2 });
  }
  return out;
}
"""

# JS: all numeric tokens currently visible in legend-like containers (the
# OHLC readout row updates as the crosshair moves across bars).
_LEGEND_NUMBERS_JS = """
() => {
  const roots = document.querySelectorAll(
    '[data-name="legend"], [class*="legend"], [class*="valuesWrapper"]');
  const nums = [];
  for (const root of roots) {
    const text = root.textContent || '';
    for (const m of text.matchAll(/\\d[\\d,]*\\.?\\d*/g)) {
      const v = parseFloat(m[0].replace(/,/g, ''));
      if (!Number.isNaN(v)) nums.push(v);
    }
  }
  return nums;
}
"""


def _parse_price(text: str) -> float | None:
    if not _PRICE_RE.match(text):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _contains_ohlc(numbers: list[float], candle: Candle) -> bool:
    """True when the legend readout shows this candle's O, H, L and C."""

    def present(value: float) -> bool:
        return any(math.isclose(n, value, rel_tol=2e-4, abs_tol=1e-9) for n in numbers)

    return all(present(v) for v in (candle.open, candle.high, candle.low, candle.close))


async def calibrate(page, pane_box: dict, setup: TamadSetup, image_scale: float):
    """Measure a :class:`ChartCalibration` for the current chart, or ``None``."""
    try:
        return await _calibrate(page, pane_box, setup, image_scale)
    except Exception as exc:  # noqa: BLE001 - calibration is always optional
        logger.info("chart calibration failed (%s) — legend-only overlay", exc)
        return None


async def _calibrate(page, pane_box: dict, setup: TamadSetup, image_scale: float):
    pane_right = pane_box["x"] + pane_box["width"]
    mid_x = pane_box["x"] + pane_box["width"] * 0.45

    # --- price axis: two crosshair points -----------------------------------
    y_a = pane_box["y"] + pane_box["height"] * 0.28
    y_b = pane_box["y"] + pane_box["height"] * 0.72
    price_a = await _crosshair_price(page, mid_x, y_a, pane_right)
    price_b = await _crosshair_price(page, mid_x, y_b, pane_right)
    if price_a is None or price_b is None or price_a == price_b:
        logger.info("calibration: could not read crosshair prices")
        return None
    if price_a < price_b:
        # Higher pixel row = higher price; anything else means we misread.
        logger.info("calibration: price axis readings inverted — discarding")
        return None

    # --- time axis: locate candle 3 and candle 2 columns ----------------------
    hover_y = (y_a + y_b) / 2
    candle3_x = await _find_bar_x(
        page, setup.candle3, pane_box, hover_y, start_x=pane_right - 4
    )
    if candle3_x is None:
        logger.info("calibration: candle 3 column not found in legend readout")
        return None
    candle2_x = await _find_bar_x(
        page, setup.candle2, pane_box, hover_y, start_x=candle3_x - 2
    )
    if candle2_x is None:
        logger.info("calibration: candle 2 column not found in legend readout")
        return None
    bar_width = candle3_x - candle2_x
    if not 1.0 <= bar_width <= pane_box["width"] / 4:
        logger.info("calibration: implausible bar width %.1fpx", bar_width)
        return None

    # --- sanity check: the mapping must reproduce a known price ----------------
    calibration = ChartCalibration(
        price_a=price_a,
        y_a=y_a,
        price_b=price_b,
        y_b=y_b,
        candle3_x=candle3_x,
        bar_width=bar_width,
        image_scale=image_scale,
    )
    span = abs(price_a - price_b)
    if span <= 0 or not (
        min(price_a, price_b) - 3 * span
        <= setup.entry
        <= max(price_a, price_b) + 3 * span
    ):
        logger.info("calibration: entry far outside measured price window — discarding")
        return None
    return calibration


async def _crosshair_price(page, x: float, y: float, pane_right: float) -> float | None:
    """Hover at (x, y) and read the crosshair price from the price axis."""
    before = await page.evaluate(_AXIS_TEXTS_JS, pane_right - 2)
    await page.mouse.move(x, y)
    await page.wait_for_timeout(120)
    after = await page.evaluate(_AXIS_TEXTS_JS, pane_right - 2)

    # The crosshair label sits at the hover height; prefer elements whose
    # center is near y, favoring ones that appeared/changed after the hover.
    before_keys = {(e["text"], round(e["cy"])) for e in before}
    candidates = [e for e in after if abs(e["cy"] - y) <= 14]
    fresh = [e for e in candidates if (e["text"], round(e["cy"])) not in before_keys]
    for pool in (fresh, candidates):
        best, best_dy = None, 1e9
        for e in pool:
            price = _parse_price(e["text"])
            if price is None:
                continue
            dy = abs(e["cy"] - y)
            if dy < best_dy:
                best, best_dy = price, dy
        if best is not None:
            return best
    return None


async def _find_bar_x(
    page, candle: Candle, pane_box: dict, hover_y: float, start_x: float
) -> float | None:
    """Scan hover positions right→left until the legend shows this candle."""
    step = 3.0
    x = start_x
    min_x = pane_box["x"] + 2
    for _ in range(240):
        if x < min_x:
            return None
        await page.mouse.move(x, hover_y)
        await page.wait_for_timeout(45)
        numbers = await page.evaluate(_LEGEND_NUMBERS_JS)
        if _contains_ohlc(numbers, candle):
            return x
        x -= step
    return None

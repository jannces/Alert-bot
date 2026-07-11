"""Python-drawn annotations on captured TradingView screenshots (D1).

The overlay is rendered with Pillow from the *validated* setup values, so
what you see always matches what Python detected — TradingView never draws
strategy graphics.

Positioning price-anchored lines requires a pixel↔price/time mapping
(:class:`ChartCalibration`) derived at capture time. When calibration is
unavailable, rendering degrades to a legend panel with all trade details —
accurate numbers, no positioned lines.

This module is pure (bytes in → bytes out) and fully unit-testable.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from strategy.models import Direction, TamadSetup

logger = logging.getLogger(__name__)

# Colors match TradingView conventions: blue entry, red stop, green targets.
_COL_ENTRY = (41, 98, 255, 255)
_COL_STOP = (242, 54, 69, 255)
_COL_TARGET = (8, 153, 129, 255)
_COL_SR = (255, 152, 0, 255)
_COL_HILITE_FILL = (255, 152, 0, 45)
_COL_HILITE_EDGE = (255, 152, 0, 200)
_COL_PANEL_BG = (15, 15, 20, 215)
_COL_PANEL_TEXT = (240, 240, 240, 255)


@dataclass(frozen=True, slots=True)
class ChartCalibration:
    """Linear pixel↔price/time mapping measured on the live chart page.

    ``(price_a, y_a)`` / ``(price_b, y_b)`` are two crosshair calibration
    points on the price axis (page coordinates); ``candle3_x`` is the x of
    Candle 3's column and ``bar_width`` the horizontal bar spacing.
    ``image_scale`` converts page coordinates to screenshot pixels (the
    capture uses a device scale factor > 1 for crispness).

    Only valid for *linear* price scales — the calibrator refuses log scales.
    """

    price_a: float
    y_a: float
    price_b: float
    y_b: float
    candle3_x: float
    bar_width: float
    image_scale: float = 1.0

    def is_valid(self) -> bool:
        return (
            self.price_a != self.price_b
            and self.y_a != self.y_b
            and self.bar_width > 0
            and self.image_scale > 0
        )

    def price_to_y(self, price: float) -> float:
        slope = (self.y_b - self.y_a) / (self.price_b - self.price_a)
        return (self.y_a + (price - self.price_a) * slope) * self.image_scale

    def x_for_bars_back(self, bars_back: int) -> float:
        return (self.candle3_x - bars_back * self.bar_width) * self.image_scale


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


def _format_price(value: float) -> str:
    if value >= 1000:
        text = f"{value:,.2f}"
    elif value >= 1:
        text = f"{value:.4f}"
    else:
        text = f"{value:.10f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _draw_hline(
    draw: ImageDraw.ImageDraw,
    y: float,
    width: int,
    color: tuple,
    label: str,
    font: ImageFont.ImageFont,
    dashed: bool = False,
) -> None:
    y = round(y)
    if dashed:
        for x in range(0, width, 14):
            draw.line([(x, y), (min(x + 8, width), y)], fill=color, width=2)
    else:
        draw.line([(0, y), (width, y)], fill=color, width=2)
    # Right-aligned price tag on the line.
    pad = 4
    box = draw.textbbox((0, 0), label, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x0 = width - tw - 2 * pad - 6
    y0 = y - th // 2 - pad
    draw.rectangle([x0, y0, x0 + tw + 2 * pad, y0 + th + 2 * pad], fill=color)
    draw.text((x0 + pad, y0 + pad), label, font=font, fill=(255, 255, 255, 255))


def _draw_legend(
    draw: ImageDraw.ImageDraw, setup: TamadSetup, font: ImageFont.ImageFont, note: str | None
) -> None:
    lines = [
        f"TAMAD {setup.direction.value}  ·  {setup.pair}  ·  {setup.timeframe_label}  ·  MEXC Perp",
        f"Entry     {_format_price(setup.entry)}",
        f"Stop Loss {_format_price(setup.stop_loss)}",
        f"TP2 (2R)  {_format_price(setup.tp2)}",
        f"TP3 (3R)  {_format_price(setup.tp3)}",
    ]
    if setup.sr is not None:
        side = "Resistance" if setup.direction is Direction.SHORT else "Support"
        lines.append(f"{side}  {_format_price(setup.sr.price)} ({setup.sr.kind.value})")
    if note:
        lines.append(note)

    pad, gap = 12, 6
    boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    text_w = max(b[2] - b[0] for b in boxes)
    line_h = max(b[3] - b[1] for b in boxes) + gap
    x0, y0 = 12, 12
    draw.rectangle(
        [x0, y0, x0 + text_w + 2 * pad, y0 + len(lines) * line_h + 2 * pad - gap],
        fill=_COL_PANEL_BG,
        outline=_COL_HILITE_EDGE,
        width=1,
    )
    for i, line in enumerate(lines):
        draw.text((x0 + pad, y0 + pad + i * line_h), line, font=font, fill=_COL_PANEL_TEXT)


def render(
    image_png: bytes,
    setup: TamadSetup,
    calibration: ChartCalibration | None,
    mode: str,
) -> bytes:
    """Annotate a chart screenshot with the validated trade picture.

    ``mode``: ``python_overlay`` (full annotations; degrades to legend when
    calibration is missing/invalid), ``legend_only``, or ``none``.
    """
    if mode == "none":
        return image_png

    image = Image.open(io.BytesIO(image_png)).convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    font = _font(max(14, height // 55))

    calibrated = (
        mode == "python_overlay" and calibration is not None and calibration.is_valid()
    )
    if calibrated:
        # Highlight the three strategy candles.
        c1, _, c3 = setup.candles
        top = calibration.price_to_y(max(c.high for c in setup.candles))
        bottom = calibration.price_to_y(min(c.low for c in setup.candles))
        x_left = calibration.x_for_bars_back(2) - calibration.bar_width * 0.6
        x_right = calibration.x_for_bars_back(0) + calibration.bar_width * 0.6
        draw.rectangle(
            [x_left, min(top, bottom) - 4, x_right, max(top, bottom) + 4],
            fill=_COL_HILITE_FILL,
            outline=_COL_HILITE_EDGE,
            width=2,
        )

        sr_label = "S/R" if setup.sr is None else setup.sr.kind.value.replace("_", " ").upper()
        levels: list[tuple[float, tuple, str, bool]] = [
            (setup.entry, _COL_ENTRY, f"ENTRY {_format_price(setup.entry)}", False),
            (setup.stop_loss, _COL_STOP, f"SL {_format_price(setup.stop_loss)}", True),
            (setup.tp2, _COL_TARGET, f"TP2 {_format_price(setup.tp2)}", True),
            (setup.tp3, _COL_TARGET, f"TP3 {_format_price(setup.tp3)}", True),
        ]
        if setup.sr is not None:
            levels.insert(
                0, (setup.sr.price, _COL_SR, f"{sr_label} {_format_price(setup.sr.price)}", False)
            )
        for price, color, label, dashed in levels:
            y = calibration.price_to_y(price)
            if 0 <= y <= height:
                _draw_hline(draw, y, width, color, label, font, dashed=dashed)
            else:
                logger.debug("annotation level %s is off-screen (y=%.0f)", label, y)
        note = None
    else:
        note = "(levels listed here; chart lines unavailable this capture)"
        if mode == "python_overlay":
            logger.info(
                "no chart calibration for %s — falling back to legend-only overlay",
                setup.symbol,
            )

    _draw_legend(draw, setup, font, note)

    combined = Image.alpha_composite(image, overlay).convert("RGB")
    out = io.BytesIO()
    combined.save(out, format="PNG")
    return out.getvalue()

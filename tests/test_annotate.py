"""Tests for the Pillow annotation overlay."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from screenshots.annotate import ChartCalibration, render
from tests.fixtures import make_short_setup


@pytest.fixture()
def chart_png() -> bytes:
    image = Image.new("RGB", (800, 400), (18, 22, 30))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.fixture()
def calibration() -> ChartCalibration:
    # 115.0 at y=50, 95.0 at y=350 → linear; candle 3 near the right edge.
    return ChartCalibration(
        price_a=115.0,
        y_a=50.0,
        price_b=95.0,
        y_b=350.0,
        candle3_x=700.0,
        bar_width=10.0,
        image_scale=1.0,
    )


def as_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


class TestCalibrationMath:
    def test_price_to_y_is_linear(self, calibration):
        assert calibration.price_to_y(115.0) == pytest.approx(50.0)
        assert calibration.price_to_y(95.0) == pytest.approx(350.0)
        assert calibration.price_to_y(105.0) == pytest.approx(200.0)

    def test_image_scale_multiplies_coordinates(self, calibration):
        from dataclasses import replace

        scaled = replace(calibration, image_scale=2.0)
        assert scaled.price_to_y(105.0) == pytest.approx(400.0)

    def test_bars_back(self, calibration):
        assert calibration.x_for_bars_back(0) == 700.0
        assert calibration.x_for_bars_back(2) == 680.0

    def test_validity(self, calibration):
        assert calibration.is_valid()
        broken = ChartCalibration(100, 50, 100, 350, 700, 10)
        assert not broken.is_valid()


class TestRender:
    def test_full_overlay_modifies_the_image(self, chart_png, calibration):
        result = render(chart_png, make_short_setup(), calibration, "python_overlay")
        assert result != chart_png
        image = as_image(result)
        assert image.size == (800, 400)
        assert image.format == "PNG"

    def test_missing_calibration_degrades_to_legend(self, chart_png):
        result = render(chart_png, make_short_setup(), None, "python_overlay")
        assert result != chart_png  # legend panel was drawn
        assert as_image(result).size == (800, 400)

    def test_legend_only_mode(self, chart_png, calibration):
        result = render(chart_png, make_short_setup(), calibration, "legend_only")
        assert result != chart_png
        assert as_image(result).format == "PNG"

    def test_mode_none_returns_input_unchanged(self, chart_png, calibration):
        assert render(chart_png, make_short_setup(), calibration, "none") == chart_png

    def test_offscreen_levels_do_not_crash(self, chart_png):
        # Mapping squeezed so the TP levels land far outside the image.
        tight = ChartCalibration(110.0, 100.0, 109.9, 110.0, 700.0, 10.0)
        result = render(chart_png, make_short_setup(), tight, "python_overlay")
        assert as_image(result).size == (800, 400)

    def test_setup_without_sr_renders(self, chart_png, calibration):
        result = render(
            chart_png, make_short_setup(sr=None), calibration, "python_overlay"
        )
        assert as_image(result).format == "PNG"

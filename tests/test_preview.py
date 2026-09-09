"""`mea_preview` correctness: max_pool preserves peaks, npz roundtrips,
render_heatmap_png emits valid PNG, thumb respects target width. Version 1.1."""
import io
import numpy as np
import pytest
from PIL import Image

from mea_preview import (
    max_pool, make_axes, pack_npz,
    render_heatmap_png, make_thumb_png, VIRIDIS_LUT,
)


class TestMaxPool:
    def test_output_shape_is_floor_divided(self):
        m = np.arange(24 * 12, dtype=np.int16).reshape(24, 12)
        pooled, prev_rows, prev_cols = max_pool(m, pool_rt=6, pool_dt=3)
        assert (prev_rows, prev_cols) == (4, 4)
        assert pooled.shape == (4, 4)

    def test_crops_ragged_input(self):
        """Non-multiple sizes get cropped before pooling."""
        m = np.zeros((25, 13), dtype=np.int16)
        pooled, prev_rows, prev_cols = max_pool(m, pool_rt=6, pool_dt=3)
        assert (prev_rows, prev_cols) == (4, 4)

    def test_narrow_peak_preserved_in_pool_window(self):
        """§17 rationale: max-pool preserves narrow peaks; slicing would
        drop them. Verify with a synthetic 1-cell spike."""
        m = np.zeros((12, 12), dtype=np.int16)
        m[5, 7] = 30000
        pooled, _, _ = max_pool(m, pool_rt=6, pool_dt=6)
        # spike at (5, 7) falls in pool block (0, 1)
        assert pooled[0, 1] == 30000

    def test_operates_on_absolute_value(self):
        """max_pool takes abs first so both polarities produce meaningful
        preview (fw 2.52 negative-going files still get bright peaks)."""
        m = np.zeros((6, 6), dtype=np.int16)
        m[0, 0] = -1000
        m[0, 3] = 500
        pooled, _, _ = max_pool(m, pool_rt=3, pool_dt=3)
        assert pooled[0, 0] == 1000
        assert pooled[0, 1] == 500

    def test_returns_float32(self):
        m = np.ones((6, 6), dtype=np.int16)
        pooled, _, _ = max_pool(m, pool_rt=3, pool_dt=3)
        assert pooled.dtype == np.float32


class TestMakeAxes:
    def test_axes_computed_from_geometry(self):
        rt, dt = make_axes(prev_rows=100, prev_cols=50, pool_rt=12, pool_dt=6,
                           sample_rate_khz=150.0, trig_repetition_ms=21.0,
                           chunk_averages=6)
        # dt_per_spec_s = 21/1000 * 6 = 0.126 s per spectrum
        # rt_axis[1] = 0.126 * 12 = 1.512 s
        assert rt[0] == pytest.approx(0.0)
        assert rt[1] == pytest.approx(1.512, rel=1e-4)
        # ms_per_drift = 1/150 kHz = 0.00667 ms
        # dt_axis[1] = 0.00667 * 6 = 0.04 ms
        assert dt[0] == pytest.approx(0.0)
        assert dt[1] == pytest.approx(1.0 / 150.0 * 6, rel=1e-4)

    def test_returns_none_none_when_geometry_missing(self):
        rt, dt = make_axes(10, 10, 12, 6, sample_rate_khz=None,
                           trig_repetition_ms=21.0, chunk_averages=6)
        assert rt is None and dt is None


class TestPackNpzRoundtrip:
    def test_matrix_and_axes_survive_roundtrip(self):
        pooled = np.arange(20, dtype=np.float32).reshape(4, 5)
        rt = np.array([0.0, 1.5, 3.0, 4.5], dtype=np.float32)
        dt = np.array([0.0, 0.04, 0.08, 0.12, 0.16], dtype=np.float32)
        blob = pack_npz(pooled, rt, dt)
        with np.load(io.BytesIO(blob)) as z:
            assert np.array_equal(z["matrix"], pooled)
            assert np.array_equal(z["rt_axis_s"], rt)
            assert np.array_equal(z["dt_axis_ms"], dt)

    def test_axes_optional(self):
        pooled = np.ones((3, 3), dtype=np.float32)
        blob = pack_npz(pooled, None, None)
        with np.load(io.BytesIO(blob)) as z:
            assert "matrix" in z.files
            assert "rt_axis_s" not in z.files
            assert "dt_axis_ms" not in z.files


class TestRenderHeatmapPng:
    def test_returns_valid_png(self):
        rng = np.random.default_rng(0)
        pooled = rng.random((100, 60), dtype=np.float32) * 1000
        png = render_heatmap_png(pooled)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")

    def test_pixel_dimensions_match_input(self):
        pooled = np.zeros((50, 30), dtype=np.float32)
        pooled[10:15, 10:12] = 5000  # a peak
        png = render_heatmap_png(pooled)
        img = Image.open(io.BytesIO(png))
        assert img.size == (30, 50)  # (width, height) = (cols, rows)

    def test_flat_input_produces_black_image(self):
        """When percentile clip has no range, output is uniform (viridis[0])."""
        pooled = np.zeros((20, 20), dtype=np.float32)
        png = render_heatmap_png(pooled)
        img = Image.open(io.BytesIO(png))
        rgb = np.array(img)
        expected_color = VIRIDIS_LUT[0]
        assert np.array_equal(rgb[0, 0], expected_color)

    def test_rt0_at_bottom_of_image(self):
        """origin='lower' convention: RT=0 (row 0 of matrix) → bottom pixel."""
        pooled = np.zeros((10, 6), dtype=np.float32)
        pooled[0, :] = 5000  # RT=0 row is bright
        png = render_heatmap_png(pooled)
        img = np.array(Image.open(io.BytesIO(png)))
        # bright row should be at the BOTTOM of the image
        assert img[-1, 3, 1] > img[0, 3, 1]  # green channel higher at bottom


class TestMakeThumbPng:
    def test_target_width_respected_when_input_larger(self):
        pooled = np.zeros((400, 800), dtype=np.float32)
        heat = render_heatmap_png(pooled)
        thumb = make_thumb_png(heat, target_width=260)
        img = Image.open(io.BytesIO(thumb))
        assert img.size[0] == 260

    def test_passthrough_when_already_small(self):
        pooled = np.zeros((100, 100), dtype=np.float32)
        heat = render_heatmap_png(pooled)
        thumb = make_thumb_png(heat, target_width=260)
        # small enough — passed through unchanged
        assert thumb == heat

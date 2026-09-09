"""Preview generation per DESIGN §5 step 8 / §17. Version 1.1.

Ingest calls max_pool + pack_npz (interactive layer only, per DESIGN §5d
two-stage split). render_previews.py fills heatmap_png / thumb_png later,
reading only preview_npz — never re-decompresses mea_file.
"""
from __future__ import annotations
import io
import numpy as np
from PIL import Image


# Viridis LUT built from 9 anchor colors sampled from matplotlib's viridis.
# Perceptually uniform, colorblind-friendly. Interpolated to 256 entries.
_VIRIDIS_ANCHORS = np.array([
    (68, 1, 84),
    (72, 40, 120),
    (62, 74, 137),
    (49, 104, 142),
    (38, 130, 142),
    (31, 158, 137),
    (53, 183, 121),
    (109, 205, 89),
    (253, 231, 37),
], dtype=np.float32)


def _build_viridis_lut() -> np.ndarray:
    n = 256
    lut = np.zeros((n, 3), dtype=np.uint8)
    steps = len(_VIRIDIS_ANCHORS) - 1
    for i in range(n):
        pos = i / (n - 1) * steps
        j = int(pos)
        frac = pos - j
        if j >= steps:
            lut[i] = _VIRIDIS_ANCHORS[-1]
        else:
            lut[i] = (
                _VIRIDIS_ANCHORS[j] * (1 - frac) + _VIRIDIS_ANCHORS[j + 1] * frac
            ).astype(np.uint8)
    return lut


VIRIDIS_LUT = _build_viridis_lut()


def max_pool(matrix: np.ndarray, pool_rt: int, pool_dt: int):
    """§17: max-pool over |matrix| — preserves narrow peaks (slicing would drop them).
    Returns (pooled_f32, prev_rows, prev_cols)."""
    n_spec, n_drift = matrix.shape
    r = (n_spec // pool_rt) * pool_rt
    c = (n_drift // pool_dt) * pool_dt
    abs_m = np.abs(matrix[:r, :c]).astype(np.float32)
    pooled = abs_m.reshape(r // pool_rt, pool_rt, c // pool_dt, pool_dt).max(axis=(1, 3))
    return pooled, pooled.shape[0], pooled.shape[1]


def make_axes(prev_rows, prev_cols, pool_rt, pool_dt,
              sample_rate_khz, trig_repetition_ms, chunk_averages):
    """Physical axes for the pooled preview.
    rt (retention time, s) per prev_row = (trig_ms/1000) * chunk_averages * pool_rt.
    dt (drift time, ms) per prev_col = (1/sample_rate_khz) * pool_dt.
    Missing header inputs -> return None,None (viewer displays index axes)."""
    if not (sample_rate_khz and trig_repetition_ms and chunk_averages):
        return None, None
    dt_per_spec_s = (float(trig_repetition_ms) / 1000.0) * float(chunk_averages)
    rt_axis_s = np.arange(prev_rows, dtype=np.float32) * (dt_per_spec_s * pool_rt)
    ms_per_drift = 1.0 / float(sample_rate_khz)
    dt_axis_ms = np.arange(prev_cols, dtype=np.float32) * (ms_per_drift * pool_dt)
    return rt_axis_s, dt_axis_ms


def pack_npz(pooled: np.ndarray, rt_axis_s, dt_axis_ms) -> bytes:
    buf = io.BytesIO()
    if rt_axis_s is not None and dt_axis_ms is not None:
        np.savez_compressed(buf, matrix=pooled, rt_axis_s=rt_axis_s, dt_axis_ms=dt_axis_ms)
    else:
        np.savez_compressed(buf, matrix=pooled)
    return buf.getvalue()


def render_heatmap_png(pooled: np.ndarray,
                       log_scale: bool = True,
                       clip: tuple[float, float] = (1.0, 99.5)) -> bytes:
    """Fixed recipe matching GC-IMS-PEAK/readGAS.plot_heatmap:
      1. log1p(img - img.min())         # RIP suppression
      2. vmin,vmax = percentile(subsample, (1.0, 99.5))
      3. clip + normalize -> viridis LUT
    Native preview resolution (1:1 pixel:datapoint per §17); RT=0 at the
    bottom edge of the image (imshow origin='lower' convention). No axes/
    colorbar/title in the blob — the Tk viewer overlays them at display
    time using the npz axes."""
    img = pooled.astype(np.float32)
    if log_scale:
        img = np.log1p(img - img.min())
    step_r = max(1, img.shape[0] // 1000)
    step_c = max(1, img.shape[1] // 1000)
    sub = img[::step_r, ::step_c]
    vmin, vmax = np.percentile(sub, clip)
    if vmax > vmin:
        normed = np.clip((img - vmin) / (vmax - vmin), 0.0, 1.0)
    else:
        normed = np.zeros_like(img)
    idx = (normed * 255).astype(np.uint8)
    rgb = VIRIDIS_LUT[idx]
    # RT=0 at bottom (matches reference imshow origin='lower').
    rgb = np.flipud(rgb)
    pil = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_thumb_png(png_bytes: bytes, target_width: int = 260) -> bytes:
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    if w <= target_width:
        return png_bytes
    th = max(1, int(h * (target_width / w)))
    thumb = img.resize((target_width, th), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

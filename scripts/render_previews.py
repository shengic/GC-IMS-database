"""Regenerate mea_preview.heatmap_png and thumb_png from preview_npz. Version 1.0.0.

Implements the DESIGN §5 step 8 backfill pattern: reads only preview_npz
(and rip_drift_index for RIP-normalized X axis), touches only the two
PNG columns + png_render_ver. Never touches mea_file, header_json,
run_telemetry, or the human fields (description/note/category_id, §10).

Two styles:
  --style figure  (default): matplotlib figure — RIP-normalized X,
      RT-s Y, viridis, colorbar, title. Matches GC-IMS-PEAK/readGAS.
  --style raw:              native-resolution pixel dump, no axes,
      per DESIGN §5c. Smaller (~300-500 KB), axes drawn at display time.

Selection:
  default            : rows where heatmap_png IS NULL
  --mea-id N         : only that measurement
  --render-ver-below N : rows with png_render_ver < N (recipe upgrade path)
  --force            : re-render every row

Usage:
    python scripts/render_previews.py                     # fill in missing
    python scripts/render_previews.py --force             # rebuild all
    python scripts/render_previews.py --style raw --force # switch to pixel dump
    python scripts/render_previews.py --mea-id 42
"""
from __future__ import annotations
import argparse
import io
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MultipleLocator, FormatStrFormatter  # noqa: E402
import numpy as np
import pymysql

sys.path.insert(0, str(Path(__file__).parent))
from mea_preview import make_thumb_png, render_heatmap_png  # noqa: E402

RENDER_VER = 1
THUMB_WIDTH = 260


def render_figure(pooled, rt_axis_s, dt_axis_ms, rip_pooled_col,
                  sample_name="", machine_type="",
                  colormap="viridis", clip=(1.0, 99.5),
                  figsize=(8, 9), dpi=150, log_scale=True) -> bytes:
    """readGAS.plot_heatmap-style figure with axes/colorbar/title.
    RIP-normalized X (drift/dt_at_rip), RT-s Y, viridis default."""
    img = pooled.astype(np.float32)
    if log_scale:
        img = np.log1p(img - img.min())
    step_r = max(1, img.shape[0] // 1000)
    step_c = max(1, img.shape[1] // 1000)
    sub = img[::step_r, ::step_c]
    vmin, vmax = np.percentile(sub, clip)

    if dt_axis_ms[rip_pooled_col] > 0:
        drift_norm = dt_axis_ms / dt_axis_ms[rip_pooled_col]
        xlabel = "Drift relative to RIP (RIP at 1.0)"
    else:
        drift_norm = dt_axis_ms
        xlabel = "Drift time [ms]"

    # OOM guard from readGAS: downsample to ~1.5x output pixels before imshow.
    target_h = int(figsize[1] * dpi * 1.5)
    target_w = int(figsize[0] * dpi * 1.5)
    step_r2 = max(1, img.shape[0] // target_h)
    step_c2 = max(1, img.shape[1] // target_w)
    img_ds = img[::step_r2, ::step_c2] if (step_r2 > 1 or step_c2 > 1) else img

    fig, ax = plt.subplots(figsize=figsize, constrained_layout=True)
    im = ax.imshow(
        img_ds, aspect="auto", origin="lower",
        extent=[float(drift_norm[0]), float(drift_norm[-1]),
                float(rt_axis_s[0]), float(rt_axis_s[-1])],
        cmap=colormap, vmin=vmin, vmax=vmax, interpolation="nearest",
    )
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Retention time [s]")
    ax.xaxis.set_major_locator(MultipleLocator(0.5))
    ax.xaxis.set_minor_locator(MultipleLocator(0.1))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.1f"))

    title = f"GC-IMS  {sample_name}" if sample_name else "GC-IMS"
    if machine_type:
        title += f"  ({machine_type})"
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="log1p(Intensity - min)" if log_scale else "Intensity")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    plt.close(fig)
    return buf.getvalue()


def _load_npz(blob: bytes):
    with np.load(io.BytesIO(blob)) as z:
        matrix = z["matrix"]
        rt = z["rt_axis_s"] if "rt_axis_s" in z.files else None
        dt = z["dt_axis_ms"] if "dt_axis_ms" in z.files else None
    return matrix, rt, dt


def _select_rows(cur, args):
    if args.mea_id is not None:
        cur.execute("""
            SELECT m.mea_id, m.sample_name, m.rip_drift_index,
                   p.preview_npz, p.pool_dt,
                   COALESCE(i.machine_type, '')
            FROM mea_preview p
            JOIN measurement m ON m.mea_id = p.mea_id
            LEFT JOIN instrument i ON i.instrument_id = m.instrument_id
            WHERE m.mea_id = %s
        """, (args.mea_id,))
    elif args.force:
        cur.execute("""
            SELECT m.mea_id, m.sample_name, m.rip_drift_index,
                   p.preview_npz, p.pool_dt,
                   COALESCE(i.machine_type, '')
            FROM mea_preview p
            JOIN measurement m ON m.mea_id = p.mea_id
            LEFT JOIN instrument i ON i.instrument_id = m.instrument_id
        """)
    elif args.render_ver_below is not None:
        cur.execute("""
            SELECT m.mea_id, m.sample_name, m.rip_drift_index,
                   p.preview_npz, p.pool_dt,
                   COALESCE(i.machine_type, '')
            FROM mea_preview p
            JOIN measurement m ON m.mea_id = p.mea_id
            LEFT JOIN instrument i ON i.instrument_id = m.instrument_id
            WHERE p.png_render_ver IS NULL OR p.png_render_ver < %s
        """, (args.render_ver_below,))
    else:
        cur.execute("""
            SELECT m.mea_id, m.sample_name, m.rip_drift_index,
                   p.preview_npz, p.pool_dt,
                   COALESCE(i.machine_type, '')
            FROM mea_preview p
            JOIN measurement m ON m.mea_id = p.mea_id
            LEFT JOIN instrument i ON i.instrument_id = m.instrument_id
            WHERE p.heatmap_png IS NULL
        """)
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="shengic")
    ap.add_argument("--password", default="sirirat")
    ap.add_argument("--database", default="gc-ims_database")
    ap.add_argument("--style", choices=("figure", "raw"), default="figure")
    ap.add_argument("--colormap", default="viridis")
    ap.add_argument("--clip", nargs=2, type=float, default=(1.0, 99.5))
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--mea-id", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="re-render every row")
    ap.add_argument("--render-ver-below", type=int, default=None,
                    help="rows with png_render_ver < N")
    args = ap.parse_args()

    conn = pymysql.connect(
        host=args.host, port=args.port, user=args.user, password=args.password,
        database=args.database, charset="utf8mb4", autocommit=False,
        connect_timeout=5,
    )

    with conn.cursor() as cur:
        rows = _select_rows(cur, args)
    print(f"selected {len(rows)} rows  (style={args.style}  recipe_ver={RENDER_VER})")

    t0 = time.perf_counter()
    ok = fail = 0
    for i, (mea_id, sample, rip_idx, npz_blob, pool_dt, machine) in enumerate(rows, 1):
        try:
            matrix, rt_axis, dt_axis = _load_npz(npz_blob)
            if args.style == "figure":
                if rt_axis is None or dt_axis is None:
                    raise ValueError("npz missing axes; --style raw required")
                rip_pooled_col = int(rip_idx) // int(pool_dt) if rip_idx is not None else 0
                if rip_pooled_col >= len(dt_axis):
                    rip_pooled_col = len(dt_axis) - 1
                png = render_figure(
                    matrix, rt_axis, dt_axis, rip_pooled_col,
                    sample_name=sample or "", machine_type=machine or "",
                    colormap=args.colormap, clip=tuple(args.clip),
                    log_scale=not args.no_log,
                )
            else:
                png = render_heatmap_png(matrix,
                                         log_scale=not args.no_log,
                                         clip=tuple(args.clip))
            thumb = make_thumb_png(png, target_width=THUMB_WIDTH)

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE mea_preview
                       SET heatmap_png=%s, thumb_png=%s, png_render_ver=%s
                     WHERE mea_id=%s
                """, (png, thumb, RENDER_VER, mea_id))
            conn.commit()
            ok += 1
            line = f"[{i:3d}/{len(rows)}] ok  mea_id={mea_id:>4d}  " \
                   f"heatmap={len(png):>7,}B  thumb={len(thumb):>6,}B  {sample!r}"
            try:
                print(line)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode())
        except Exception as e:
            conn.rollback()
            fail += 1
            print(f"[{i:3d}/{len(rows)}] failed  mea_id={mea_id}  {type(e).__name__}: {e}")

    total = time.perf_counter() - t0
    print(f"\ntotal {total:.1f}s   ok={ok}  failed={fail}")
    conn.close()


if __name__ == "__main__":
    main()

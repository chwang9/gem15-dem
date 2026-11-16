#!/usr/bin/env python3
"""GEM-15 GeoTIFF -> 预览 PNG：自然地形配色 + 山体阴影 + 极简出版级要素。

制图约定（沿用用户既有规范）：
- 标题区只有区域名，不拼 zoom / source / time 等元数据副标题；
- 罗盘式指北针（右上）、图形比例尺（左下，非纯数字）；
- nodata 输出为透明，不产生 0 值黑边；
- 配色按数据自身 2%–98% 分位拉伸，低地绿 -> 山地棕 -> 高处灰白，克制无辉光。

--plain 可关闭注记（只要数据本体）；--bg white 改白底。

v1.2.0（实测后改造）：**先按 --maxside 降采样读取，再渲染**。
旧实现把整个 GeoTIFF 读进内存、在全分辨率上算 colorize / gradient / hillshade，
最后才 resize 成 1600 px；中间数组峰值随输入面积线性放大，实测：
  3000² 输入：585 MB / 2.93 s（旧） vs 210 MB / 1.57 s（新）
  6000² 输入：2160 MB / 19.98 s（旧） vs 224 MB / 1.97 s（新）  -> 峰值 9.6x、耗时 10.1x
新实现峰值几乎不随输入尺寸变化，且山体阴影在降采样后的数据上计算，
不会把全分辨率高频噪声缩小成交叉网格纹（视觉验收更自然）。
Resampling.average 会正确跳过 nodata：实测 6000² 合成图上 average / nearest /
read_masks 的 nodata 像元数均为 2809，bilinear 只有 2704（会侵蚀边界），故用 average。
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from PIL import Image, ImageDraw, ImageFont

from gem15_common import GemError, check_help, log, parse_args, warn

BG_MODES = ("transparent", "white")
CLI_FLAGS = ("help", "h", "plain")
USAGE = ("用法: python preview_dem.py dem.tif out.png [--title 名称] "
         "[--maxside 1600] [--bg transparent|white] [--plain]")

# 归一化高程（0–1）色标：真实、低饱和
STOPS = [
    (0.00, (56, 108, 84)), (0.18, (110, 155, 96)),
    (0.36, (176, 190, 122)), (0.55, (214, 199, 150)),
    (0.72, (176, 151, 116)), (0.86, (154, 146, 136)),
    (0.95, (214, 214, 210)), (1.00, (250, 250, 250)),
]


def colorize(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0.0, 1.0)
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    for i in range(len(STOPS) - 1):
        v0, c0 = STOPS[i]
        v1, c1 = STOPS[i + 1]
        m = (t >= v0) & (t < v1) if i < len(STOPS) - 2 else (t >= v0)
        if not m.any():
            continue
        f = ((t[m] - v0) / (v1 - v0))[..., None]
        out[m] = np.array(c0, dtype=np.float32) + f * (
            np.array(c1, dtype=np.float32) - np.array(c0, dtype=np.float32))
    return out


def hillshade(a: np.ndarray, res_y: float, res_x: float,
              az: float = 315.0, alt: float = 45.0) -> np.ndarray:
    gy, gx = np.gradient(a, res_y, res_x)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(az), math.radians(alt)
    return (math.sin(alt) * np.cos(slope)
            + math.cos(alt) * np.sin(slope) * np.cos(az - aspect))


def _font(px: int):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
              r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, px)
            except Exception:                        # noqa: BLE001
                continue
    warn("未找到中文字体，注记降级为内置位图字体（仍可显示英文数字）")
    return ImageFont.load_default()


def _nice_length(target: float) -> float:
    if target <= 0:
        return 1.0
    exp = math.floor(math.log10(target))
    base = 10 ** exp
    for f in (5, 2, 1):
        if f * base <= target:
            return float(f * base)
    return float(base)


def _label(meters: float) -> str:
    return f"{meters / 1000:g} km" if meters >= 1000 else f"{meters:g} m"


def annotate(img: Image.Image, title: str, m_per_px: float) -> Image.Image:
    w, h = img.size
    d = ImageDraw.Draw(img)
    s = max(15, int(min(w, h) * 0.030))
    f = _font(s)
    fs = _font(max(11, int(s * 0.72)))
    ink = (38, 42, 45, 255)
    halo = (255, 255, 255, 225)
    pad = int(s * 0.9)

    # 标题：只有区域名，无副标题；单次描边，避免叠字糊掉中文笔画
    if title:
        d.text((pad, pad), title, font=f, fill=ink,
               stroke_width=2, stroke_fill=halo)

    # 罗盘式指北针（右上）
    r = max(18, int(min(w, h) * 0.038))
    cx, cy = w - pad - r - s, pad + r + s // 2
    d.ellipse((cx - r, cy - r, cx + r, cy + r),
              outline=ink, width=max(2, s // 10))
    d.polygon([(cx, cy - r * 0.78), (cx - r * 0.28, cy + r * 0.12),
               (cx, cy), (cx + r * 0.28, cy + r * 0.12)], fill=ink)
    d.text((cx - s * 0.22, cy - r - s * 1.35), "N", font=fs, fill=ink,
           stroke_width=1, stroke_fill=halo)

    # 图形比例尺（左下）
    target_m = m_per_px * w * 0.18
    length_m = _nice_length(target_m)
    length_px = max(40, int(round(length_m / m_per_px)))
    x0, y0 = pad, h - pad - s * 2
    bh = max(5, s // 3)
    for i in range(4):
        x1 = x0 + int(length_px * i / 4)
        x2 = x0 + int(length_px * (i + 1) / 4)
        blk = ink if i % 2 == 0 else (255, 255, 255, 235)
        d.rectangle((x1, y0, x2, y0 + bh), fill=blk, outline=ink, width=1)
    d.text((x0, y0 - s * 1.25), "0", font=fs, fill=ink,
           stroke_width=1, stroke_fill=halo)
    d.text((x0 + length_px - s * 0.4, y0 - s * 1.25), _label(length_m),
           font=fs, fill=ink, stroke_width=1, stroke_fill=halo)
    return img


def render(tif: Path, png: Path, *, title: str = "", maxside: int = 1600,
           bg: str = "transparent", do_annotate: bool = True,
           p2: float | None = None, p98: float | None = None) -> dict:
    # v1.3.0：旧实现把任何非法值都当 transparent，--bg blue 静默生效
    if bg not in BG_MODES:
        raise GemError(f"--bg 只能是 {' 或 '.join(BG_MODES)}，实际 {bg}")
    # v1.5.0：maxside=0/-1 会把降采样图退化成 1x1，实测 np.gradient 抛
    # ValueError「Shape of array too small」→ 未预期错误 exit 1。前置成 GemError。
    if not isinstance(maxside, int) or maxside < 2:
        raise GemError(f"--maxside 需为不小于 2 的整数，实际 {maxside!r}")
    with rasterio.open(tif) as ds:
        src_h, src_w = ds.height, ds.width
        # v1.13.0：源图只有 1 行/1 列时 np.gradient(a, edge_order=1) 抛
        # ValueError（实测 (1,N)/(N,1)/(1,1) 全崩、traceback exit 1）。
        # 前置成 GemError，与 --maxside 同类契约。
        if min(src_h, src_w) < 2:
            raise GemError(
                f"预览需要至少 2 行 2 列的 DEM，实际 {src_w}x{src_h}"
                "（单行/单列无法计算山体阴影；请改用正常范围的输出）")
        # 先降采样再渲染：out_shape 直接读成最终尺寸，不产生全分辨率中间数组。
        k = min(1.0, maxside / max(src_h, src_w))
        h2, w2 = max(1, int(round(src_h * k))), max(1, int(round(src_w * k)))
        a = np.asarray(ds.read(1, out_shape=(h2, w2),
                               resampling=Resampling.average), dtype=np.float32)
        tr = ds.transform * Affine.scale(src_w / w2, src_h / h2)
        crs = ds.crs
        res_x, res_y = abs(tr.a), abs(tr.e)
        if crs is not None and crs.is_geographic:
            lat_c = tr.f + h2 / 2 * tr.e
            mx = res_x * 111320.0 * max(math.cos(math.radians(lat_c)), 1e-6)
            my = res_y * 110574.0
        else:
            mx = res_x
            my = res_y
        if (h2, w2) != (src_h, src_w):
            log(f"预览降采样: {src_w}x{src_h} -> {w2}x{h2}"
                f"（避免全分辨率渲染的中间数组）")
    invalid = ~np.isfinite(a)
    valid = a[~invalid]
    if valid.size == 0:
        warn("预览图输入全为 nodata（仍输出全透明图）")
        lo, hi = 0.0, 1.0
    else:
        lo = float(p2 if p2 is not None else np.percentile(valid, 2))
        hi = float(p98 if p98 is not None else np.percentile(valid, 98))
        if hi - lo < 1e-3:
            hi = lo + 1.0
    a0 = np.where(invalid, lo, a)
    t = (np.clip(a0, lo, hi) - lo) / (hi - lo)
    rgb = colorize(t)
    sh = hillshade(a0, my, mx)
    rgb *= (0.40 + 0.60 * np.clip(sh, 0, 1))[..., None]
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)

    if bg == "white":
        rgb[invalid] = (255, 255, 255)
        img = Image.fromarray(rgb, "RGB").convert("RGBA")
    else:
        alpha = np.where(invalid, 0, 255).astype(np.uint8)
        img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    w, h = img.size
    if max(w, h) > maxside:      # 兜底：round 可能带来 1 像素超出
        k2 = maxside / max(w, h)
        img = img.resize((int(w * k2), int(h * k2)), Image.LANCZOS)
        mx /= k2
    if do_annotate:
        # a 已经是最终分辨率，mx 即每输出像素对应的米数（不再乘缩放系数）
        img = annotate(img, title, mx)
    png.parent.mkdir(parents=True, exist_ok=True)
    img.save(png)
    stats = {"png": str(png), "size": list(img.size), "mode": img.mode,
             "source_pixels": [src_w, src_h], "render_pixels": [w2, h2],
             "nodata_ratio": round(float(invalid.mean()), 5),
             "stretch_m": [round(lo, 2), round(hi, 2)], "annotated": do_annotate}
    log(f"预览图: {png} {img.size} {img.mode} nodata {stats['nodata_ratio'] * 100:.1f}% "
        f"拉伸 {lo:.1f}~{hi:.1f} m")
    return stats


def main() -> None:
    import sys
    pos, opts, flags = parse_args(sys.argv[1:],
                                  ("--bg", "--title", "--maxside"), CLI_FLAGS)
    if check_help(pos, flags, USAGE):
        sys.exit(0)
    if len(pos) < 2:
        print(USAGE)
        sys.exit(1)
    # v1.10.0：黑盒实测 GemError 从 main 裸抛 -> traceback exit 1；
    # --maxside abc 抛 int() ValueError。统一包装成 [错误] + exit 2。
    try:
        try:
            maxside = int(opts.get("maxside", 1600))
        except (TypeError, ValueError) as exc:
            raise GemError(
                f"--maxside 需要整数，实际 {opts.get('maxside')!r}") from exc
        render(Path(pos[0]), Path(pos[1]), title=opts.get("title", ""),
               bg=opts.get("bg", "transparent"),
               maxside=maxside,
               do_annotate="plain" not in flags)
    except GemError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()

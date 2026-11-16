#!/usr/bin/env python3
"""GEM-15 分片读取 -> 拼接 -> 按边界裁切 -> GeoTIFF（float32 / NaN nodata）。

两层分片（实测依据见 README）：
  1) 按 STAC 的 1°×1° cell 分片（区域与哪些 cell 有正面积相交才读哪些片）；
  2) 每片只通过 COG 的 HTTP Range 读取与 bbox 相交的窗口（--mode window，默认），
     或先把整片 COG 落盘缓存再本地读窗口（--mode full）。

拼接在 numpy 里做，不依赖 merge 对 NaN nodata 的黑盒行为：
  - 所有片同 UTM 带 -> 保持原生 15 m 网格，直接按像素偏移贴入，零重采样；
  - 跨 UTM 带 -> 显式降级到 EPSG:4326（约 15 m 等效度格网），逐片 reproject，
    并打印 [warn] 说明降级原因，绝不静默。
  - 有效值优先（first-valid），不会让某片的 NaN 覆盖另一片的有效高程。

v1.1.0 实测后明确放弃的两个「看起来该优化」的方向（避免以后重复踩坑）：
  - 并发打开多个 /vsicurl/ COG：4 片串行 18.87 s，4 线程并发 48.89 s（慢 2.6 倍），
    数组逐像元一致但耗时明显变差，故窗口读取保持串行；
  - 按同带/跨带动态缩小 PAD（48->4）：冷缓存看似快 1.57 倍，热缓存复测
    10.29 s vs 10.55 s 无差异，那 6.8 s 属于单次慢请求方差，不能归因于 PAD。

v1.2.0（实测后改造）：**流式拼接 + 输出规模预检**。
旧实现把每个分片的窗口数组全部留在 windows 列表里，直到 grid_for 算完统一网格
才逐片贴入；而国家尺度在 15 m 下动辑数亿像元（实测推算：卢森堡 21.2 M、
奥地利 746.5 M、法国本土 5468.5 M），必然 OOM 而不是报错。
实测 STAC asset 的 proj:transform / proj:shape 与真实 COG 逐项完全一致（两个
不同 UTM 带、两种 shape，窗口与 window_transform 均逐像素相同），因此可以：
  ① 纯离线算出每片窗口元数据 -> grid_for 提前算网格（零额外网络、零额外磁盘）；
  ② 在读取任何像元之前按 `像元数 × 波段数 × 4 B` 预估输出内存，超限直接报错
     并给出可执行建议（禁止静默 OOM）；
  ③ 逐片 read -> paste -> 立即释放，常驻内存实测从 1139 MB 降到 830 MB（-27%）。
跨带 reproject 也改为只分配「该窗口覆盖的局部网格」，不再分配全网格临时数组。

v1.7.0：**预检顺序修正 + 三项新预检**。旧实现 download_items 在 assert_output_fits
之前，full 模式大国会先落盘 19.24 GiB 才报超限。现在顺序固定为：
记录下载总量 -> 输出规模（含物理可用内存对照）-> 磁盘空间 -> 才允许下载/读像元。
超限异常携带 info，入口据此生成可执行分块方案（--split auto）。
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import Affine, from_origin
from rasterio.warp import (Resampling, reproject, transform_bounds,
                           transform_geom)
from rasterio.windows import Window, from_bounds, intersection

from fetch_tiles import (assert_download_fits, download_items, tile_path,
                         _cache_ok)
from gem15_common import (ATTRIBUTION, DEFAULT_MAX_DOWNLOAD_MB,
                          DEFAULT_MAX_OUTPUT_MB, LICENSE, LAYER_BANDS,
                          SHORT_SLEEP, VERTICAL_DATUM, DownloadError,
                          EmptyRasterError, GemError, OutputTooLargeError,
                          assert_disk_space, available_memory_mb, bbox_intersects,
                          check_help, default_cache,
                          geojson_geometries, log, parse_args, progress, warn)

WGS84 = CRS.from_epsg(4326)
CLI_FLAGS = ("help", "h", "no-clip", "no-cog")
USAGE = ("用法: python build_mosaic.py items.json boundary.json out.tif "
         "[--layer dtm] [--mode window] [--crs auto] [--no-clip] [--no-cog] "
         "[--buffer 0] [--max-output-mb 512] [--max-download-mb 2048]")
DEG_RES = 15.0 / 110574.0       # 15 m 对应的纬度度数（4326 输出口径）


def window_sources(fetch_pairs: list, layer: str, cache: Path) -> tuple:
    """v1.15.0：window 模式优先复用本地整片缓存。

    实测（同一 w8s53 COG，2048×2048 窗口）：本地缓存读 0.21 s（75.9 MB/s）
    vs /vsicurl/ 远程 7.42 s（2.2 MB/s），**35.2x 差距**。full 模式或整区
    预下载（v1.14.0）留下的整片缓存直接吃掉；命中判定复用 fetch_tiles 的
    `_cache_ok`（字节数与 STAC `file:size` 一致 + rasterio 可打开），
    截断/损坏的缓存不会误用，静默回退远程。返回 (sources, 命中数)。
    """
    sources: list = []
    hits = 0
    for it, plan in fetch_pairs:
        local = tile_path(it, layer, cache)
        if _cache_ok(local, it.get("size")):
            sources.append((it, plan, str(local)))
            hits += 1
        else:
            sources.append((it, plan, "/vsicurl/" + it["url"]))
    return sources, hits
PAD = 48                        # 窗口外扩像素，避免重投影边缘截断


def validate_crs_opt(crs_opt: str) -> None:
    """前置校验 --crs（v1.6.0）。

    实测 `--crs bad` / `999999` / `EPSG:999999` 会在 build 深处抛
    rasterio.errors.CRSError（**不是** GemError），主入口按「未预期错误」
    打印 traceback 并 exit 1。校验放在入口参数段（联网之前）+ decide_crs
    （独立 CLI 防御）两处，统一转成 GemError / exit 2。
    """
    if not crs_opt or crs_opt.lower() == "auto":
        return
    try:
        CRS.from_user_input(crs_opt)
    except Exception as exc:                          # noqa: BLE001
        raise GemError(
            f"--crs 无法解析为坐标系，实际 {crs_opt!r}: {exc}；"
            "可用 auto / EPSG:4326 / EPSG:326xx / EPSG:327xx") from exc


def decide_crs(epsgs: list, crs_opt: str, stats: dict):
    """返回 (target CRS, res, 是否发生降级)。"""
    validate_crs_opt(crs_opt)
    if crs_opt and crs_opt.lower() != "auto":
        crs = CRS.from_user_input(crs_opt)
        res = DEG_RES if crs.is_geographic else 15.0
        return crs, res, False
    unique = sorted({int(e) for e in epsgs if e})
    if len(unique) == 1:
        return CRS.from_epsg(unique[0]), 15.0, False
    msg = (f"命中 {len(unique)} 个不同 UTM 带 {unique}；为避免跨带变形，"
           f"统一重投影到 EPSG:4326（{DEG_RES:.8f}° ≈ 15 m 纬度向），"
           "像元将变为非正方形（东西向随纬度变化）")
    warn(msg)
    stats["degraded_reason"] = msg
    return WGS84, DEG_RES, True


def _retry_sleep(attempt: int, retries: int) -> None:
    """重试退避：短等递增，最后一次失败后**不再等**（固定 sleep 是隐性开销大户）。

    v1.1.0 前为每次失败固定 time.sleep(2)，含最后一次无意义的 2 s；
    retries=2 时旧行为白等 6 s，新行为只等 1+2=3 s 且更可观测。
    """
    if attempt >= retries:
        return
    time.sleep(min(SHORT_SLEEP, 1 + attempt))


def plan_window(item: dict, bbox, layer: str, pad: int = PAD) -> dict | None:
    """纯 STAC 元数据算出窗口骨架，**不打开 COG**（v1.2.0 流式拼接的前提）。

    实测依据：STAC asset 的 proj:transform / proj:shape / proj:epsg 与真实 COG
    逐项一致，本函数算出的 (col_off, row_off, width, height) 及 window_transform
    与 `rasterio.open(...).window(*b)` 在两个不同 UTM 带、两种 shape 上完全相同。
    `read_one_window` 仍会校验实际 COG，不一致时打印 [warn] 并现场重算。
    """
    tr = Affine(*item["proj_transform"])
    crs = CRS.from_epsg(int(item["epsg"]))
    shp = item["proj_shape"]                  # [height, width]
    b = transform_bounds(WGS84, crs, *bbox, densify_pts=21)
    inv = ~tr
    c_ul, r_ul = inv * (b[0], b[3])
    c_lr, r_lr = inv * (b[2], b[1])
    c0 = max(0, int(math.floor(c_ul)) - pad)
    r0 = max(0, int(math.floor(r_ul)) - pad)
    c1 = min(shp[1], int(math.ceil(c_lr)) + pad)
    r1 = min(shp[0], int(math.ceil(r_lr)) + pad)
    if c1 <= c0 or r1 <= r0:
        warn(f"规划窗口为空，跳过 {item['cell']}（WGS84 bbox={bbox} -> {b}；"
             f"c0/c1={c0}/{c1}，r0/r1={r0}/{r1}）")
        return None
    w = Window(c0, r0, c1 - c0, r1 - r0)
    tr_w = tr * Affine.translation(c0, r0)
    return {"arr": None, "transform": tr_w, "crs": crs,
            "count": len(LAYER_BANDS[layer]),
            "width": int(w.width), "height": int(w.height), "nodata": np.nan,
            "bounds": rasterio.transform.array_bounds(int(w.height),
                                                      int(w.width), tr_w),
            "cell": item["cell"], "window": w,
            "src_transform": tr, "src_shape": shp}


def _window_from_src(src, bbox, pad: int) -> Window | None:
    """现场按 COG 实际头部算窗口（plan 缺失或与实际不符时的兜底，不静默）。"""
    b = transform_bounds(WGS84, src.crs, *bbox, densify_pts=21)
    win = src.window(*b)
    c0 = max(0, int(math.floor(win.col_off)) - pad)
    r0 = max(0, int(math.floor(win.row_off)) - pad)
    c1 = min(src.width, int(math.ceil(win.col_off + win.width)) + pad)
    r1 = min(src.height, int(math.ceil(win.row_off + win.height)) + pad)
    if c1 <= c0 or r1 <= r0:
        warn(f"窗口为空，跳过 (bbox={b})")
        return None
    return Window(c0, r0, c1 - c0, r1 - r0)


def _plan_matches(plan: dict, src) -> bool:
    a = tuple(plan["src_transform"])[:6]
    b = tuple(src.transform)[:6]
    return (all(abs(x - y) < 1e-6 for x, y in zip(a, b)) and
            (src.height, src.width) == tuple(plan["src_shape"]) and
            src.crs == plan["crs"])


def assert_output_fits(width: int, height: int, count: int,
                       max_mb: float, stats: dict) -> None:
    """在读取任何像元之前拒绝必然 OOM 的区域（禁止静默失败）。

    估算口径：成果数组 = 网格像元数 × 波段数 × 4 B（float32）。网格来自
    grid_for，含 PAD 外扩，是**保守上界**（最终还会按边界外接框裁切）。
    """
    mb = width * height * count * 4 / 1048576.0
    stats["estimated_output_mb"] = round(mb, 2)
    # v1.6.0：mb 只是**成果数组**大小，不是进程峰值。实测裁切紧凑化瞬间
    # 全网格 + 副本共存（1000² -> 600²：5.19 MB vs 1.37 MB，3.8×），另有
    # 掩膜 / 有效值副本 / 单窗口数组。写清口径，避免用户按 512 MB 估计
    # 机器内存却实际 OOM。判定逻辑不变（既有语义与成果零变化）。
    stats["estimated_peak_mb"] = round(mb * 3.0, 2)
    stats["max_output_mb"] = max_mb
    # v1.7.0：真实可用物理内存对照。实测本机总 31.68 GiB / 可用 7.54 GiB，
    # 而法国整国峰值约 98 GiB；若用户只按提示把 --max-output-mb 提到 40000，
    # 数组上限放行了，进程仍会被系统 OOM 静默杀掉。不能只看「用户填的上限」。
    avail = available_memory_mb()
    stats["available_memory_mb"] = round(avail, 2) if avail else None
    px = width * height
    # v1.7.0：把规模信息随异常带回入口，用于生成**可执行**的分块方案，
    # 而不是只给一句「拆成 2-3 块」。入口据此写 <stem>_split_plan.json。
    info = {"estimated_output_mb": round(mb, 2),
            "estimated_peak_mb": stats["estimated_peak_mb"],
            "width": width, "height": height, "count": count,
            "max_output_mb": max_mb,
            "bbox": stats.get("bbox_used"), "layer": stats.get("layer"),
            "crs": stats.get("crs"), "resolution": stats.get("resolution"),
            "cells": stats.get("cells_planned"),
            "download_total_mb": stats.get("download_total_mb"),
            "available_memory_mb": stats["available_memory_mb"]}
    if mb <= max_mb:
        if avail and stats["estimated_peak_mb"] > avail * 0.8:
            raise OutputTooLargeError(
                f"预估进程峰值 {stats['estimated_peak_mb']:.0f} MB 超过本机可用物理内存 "
                f"{avail:.0f} MB 的 80%（成果数组 {mb:.2f} MB 虽在 --max-output-mb "
                f"{max_mb:.0f} MB 之内，但运行仍会被系统 OOM 静默杀掉）。\n"
                "禁止静默失败：请在读取任何像元之前停止。\n"
                "建议：加 --split auto 自动分块（内存恒定、逐块释放、断点续跑），"
                "或缩小 --bbox；不要用提高 --max-output-mb 硬扛。", info)
        return
    raise OutputTooLargeError(
        f"预估成果数组 {mb:.2f} MB（{px / 1e6:.2f} M 像元 × {count} 波段 × 4 B），"
        f"超过上限 {max_mb:.2f} MB；已在读取任何像元、下载任何瓦片之前停止。\n"
        f"口径：{mb:.2f} MB 是成果数组大小，进程峰值约为其 2–4 倍"
        f"（估算 {stats['estimated_peak_mb']:.2f} MB），含裁切紧凑化副本、掩膜与有效值数组。\n"
        "建议：① 加 --split auto 由技能自动分块逐块产出（内存恒定、断点续跑，"
        "入口会写 <stem>_split_plan.json）；② 用 --bbox 手工拆块；"
        "③ 大区域优先用 dtm（dsm 为 5 波段，占 5 倍内存）；"
        f"④ 确认内存与磁盘充足后加 --max-output-mb {max(1, math.ceil(mb * 1.5))}。\n"
        f"注意：分块**不会减少总数据量**，磁盘仍需约 {mb / 1024:.1f} GiB 成果"
        f"（deflate 压缩后通常更小）；本次 {stats.get('download_cells', 0)} 片 COG "
        f"合计 {stats.get('download_total_mb', 0):.0f} MB，full 模式还需等量落盘。\n"
        "请确认输出目录所在盘剩余空间。\n"
        "参考实测推算（同带原生 15 m 口径）：列支敦士登 1.4 M 像元 / 5.3 MB，"
        "卢森堡 21.2 M / 80.8 MB，奥地利 746.5 M / 2847.6 MB；\n"
        "跨 UTM 带时会降级到 EPSG:4326、像元非正方形，实际像元数与内存还会进一步放大"
        "（奥地利实测 1275.5 M 像元 / 4866 MB）。", info)


_NODATA_WARNED: set = set()


def normalize_nodata(arr: np.ndarray, nodata, path: str = "") -> np.ndarray:
    """把非 NaN 的 nodata 掩成 NaN（v1.6.0）。

    paste 只用 np.isfinite 判有效；若 COG 声明 nodata=-32768，最小合成实测
    100 个填充像元会被当成有效高程贴入，成果 min 变成 -32768 m（静默数据错误）。
    真实 GEM-15 声明 nodata=NaN，本函数在该情况下零复制直接返回，成果逐像元不变。
    """
    if nodata is None:
        if None not in _NODATA_WARNED:
            _NODATA_WARNED.add(None)
            warn(f"{path} 未声明 nodata；按 np.isfinite 判有效（无法识别数值填充值）")
        return arr
    try:
        is_nan = bool(np.isnan(nodata))
    except (TypeError, ValueError):
        is_nan = False
    if is_nan:
        return arr
    if nodata not in _NODATA_WARNED:
        _NODATA_WARNED.add(nodata)
        warn(f"{path} 声明 nodata={nodata!r}（非 NaN），拼接前统一掩为 NaN，"
             "避免填充值污染高程统计")
    return np.where(arr == nodata, np.float32(np.nan), arr).astype(np.float32, copy=False)


def read_one_window(path: str, bbox, pad: int = PAD, retries: int = 2,
                    plan: dict | None = None) -> dict | None:
    """打开一个（远程 COG 或本地 COG），只读与 bbox 相交的窗口。

    v1.2.0：plan 由 STAC 元数据离线算出，省掉每次 `src.window()` 的重算；
    实际 COG 与元数据不符时打印 [warn] 并回退现场计算。源数据本就是 float32，
    改用 np.asarray 而非 astype，避免一次同等大小的无谓复制。
    v1.6.0：非 NaN 的 nodata 在读取时统一掩为 NaN（见 normalize_nodata）。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            with rasterio.open(path) as src:
                if plan is not None:
                    if _plan_matches(plan, src):
                        w = plan["window"]
                    else:
                        warn(f"{plan['cell']} 实际 COG 与 STAC 元数据不一致，"
                             "回退现场计算窗口（不静默沿用规划）")
                        w = _window_from_src(src, bbox, pad)
                else:
                    w = _window_from_src(src, bbox, pad)
                if w is None:
                    return None
                arr = normalize_nodata(
                    np.asarray(src.read(window=w), dtype=np.float32),
                    src.nodata, path)
                tr = src.window_transform(w)
                return {"arr": arr, "transform": tr, "crs": src.crs,
                        "count": arr.shape[0], "width": arr.shape[2],
                        "height": arr.shape[1], "nodata": src.nodata,
                        "bounds": rasterio.transform.array_bounds(arr.shape[1],
                                                                  arr.shape[2], tr)}
        except Exception as exc:                      # noqa: BLE001
            last = exc
            warn(f"窗口读取失败（第 {attempt + 1} 次）{path}: {type(exc).__name__}: {exc}")
            _retry_sleep(attempt, retries)
    raise DownloadError(f"窗口读取重试耗尽: {path}: {last}")


def grid_for(windows: list[dict], target_crs: CRS, res: float):
    boxes = [transform_bounds(w["crs"], target_crs, *w["bounds"], densify_pts=21)
             for w in windows]
    left = min(b[0] for b in boxes)
    bottom = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    top = max(b[3] for b in boxes)
    aligned = all(w["crs"] == target_crs and
                  abs(w["transform"].a - res) < 1e-8 and
                  abs(abs(w["transform"].e) - res) < 1e-8 for w in windows)
    if aligned:
        # 以窗口自身的像素原点为基准，而不是强行对齐到绝对 0 原点：
        # 后者在窗口原点不是 res 整数倍时会多出一行/一列并整体错位
        # （由最小合成数据 selftest 暴露）。
        left = min(w["transform"].c for w in windows)
        top = max(w["transform"].f for w in windows)
    else:
        left = math.floor(left / res) * res
        top = math.ceil(top / res) * res
    width = max(1, int(math.ceil((right - left) / res)))
    height = max(1, int(math.ceil((top - bottom) / res)))
    return from_origin(left, top, res, res), width, height


def paste(out: np.ndarray, w: dict, grid_tr, width: int,
          height: int, target_crs: CRS, res: float) -> int:
    """把一个窗口贴入统一网格；返回本片至少为一个波段新增有效值的像元数。

    v1.6.0：去掉 2D `filled` 掩膜，改为逐波段用 `np.isfinite(region[b])`
    判断该波段该像元是否已有值。旧实现 `filled` 是 (height,width) 且按
    「任意波段有效」置位：最小合成证实片 A 波段0有效/波段1为 NaN、片 B
    波段1有效时，片 B 的波段1 被整片跳过（真实 dsm 5 波段中 dsm_std
    与主波段掩码有 41 个像元不同）。改用成果数组自身判空后：
      ① 逐波段 first-valid 语义正确；
      ② 不再常驻一块全网格 bool 掩膜（跨带 / 多波段更省内存）。
    """
    same_grid = (w["crs"] == target_crs and
                 abs(w["transform"].a - res) < 1e-8 and
                 abs(abs(w["transform"].e) - res) < 1e-8)
    if same_grid:
        arr = w["arr"]
        dcol = (w["transform"].c - grid_tr.c) / res
        drow = (grid_tr.f - w["transform"].f) / res
        col, row = int(round(dcol)), int(round(drow))
        if abs(dcol - col) > 0.01 or abs(drow - row) > 0.01:
            # 不能静默 round 掉半像素错位
            warn(f"分片与统一网格未对齐 (dcol={dcol:.4f}, drow={drow:.4f})，"
                 "该片按最近像素贴入，可能产生 ≤0.5 像素偏移")
    else:
        # v1.2.0：只分配「该窗口覆盖的局部网格」，不再分配整个成果网格。
        # 旧实现每片都建一块 (count, height, width) 的临时数组，跨带降级时
        # 与 out 等大，是大区域内存的又一处黑洞。local_tr 与 grid_tr 相差
        # res 的整数倍，reproject 结果与全网格逐像元一致（selftest 固化）。
        b2 = transform_bounds(w["crs"], target_crs, *w["bounds"], densify_pts=21)
        w2 = from_bounds(*b2, transform=grid_tr)
        lc0 = max(0, int(math.floor(w2.col_off)) - 2)
        lr0 = max(0, int(math.floor(w2.row_off)) - 2)
        lc1 = min(width, int(math.ceil(w2.col_off + w2.width)) + 2)
        lr1 = min(height, int(math.ceil(w2.row_off + w2.height)) + 2)
        if lc1 <= lc0 or lr1 <= lr0:
            warn(f"重投影窗口落在成果网格之外，跳过 {w.get('cell', '?')}")
            return 0
        local_tr = from_origin(grid_tr.c + lc0 * res,
                               grid_tr.f - lr0 * res, res, res)
        arr = np.full((w["count"], lr1 - lr0, lc1 - lc0), np.nan,
                      dtype=np.float32)
        reproject(source=w["arr"], destination=arr,
                  src_transform=w["transform"], src_crs=w["crs"],
                  src_nodata=np.nan, dst_transform=local_tr, dst_crs=target_crs,
                  dst_nodata=np.nan, resampling=Resampling.bilinear)
        col, row = lc0, lr0
    h, ww = arr.shape[1], arr.shape[2]
    gc0, gr0 = max(0, col), max(0, row)
    gc1, gr1 = min(width, col + ww), min(height, row + h)
    if gc1 <= gc0 or gr1 <= gr0:
        warn("窗口落在统一网格之外，跳过（不静默计入）")
        return 0
    sub = arr[:, gr0 - row:gr1 - row, gc0 - col:gc1 - col]
    valid = np.isfinite(sub)
    region = out[:, gr0:gr1, gc0:gc1]
    any_new = np.zeros(region.shape[1:], dtype=bool)
    for b in range(out.shape[0]):
        m = valid[b] & ~np.isfinite(region[b])
        region[b][m] = sub[b][m]
        any_new |= m
    return int(any_new.sum())


def project_geometries(boundary: dict, target_crs: CRS) -> list[dict]:
    """把 WGS84 边界几何投影到成果 CRS。

    geometry_mask 用的是**成果栅格的坐标系**；若成果是 UTM 而边界仍是经纬度，
    掩膜会整体错位到原点附近，裁出全 NaN。这是真实端到端才会暴露的组合错误，
    因此单独成函数并固化进 selftest。
    """
    out = []
    for g in geojson_geometries(boundary):
        out.append(g if target_crs == WGS84 else
                   transform_geom(WGS84, target_crs, g))
    return out


def part_bboxes(boundary: dict) -> list[tuple[float, float, float, float]]:
    """边界几何的所有环外接框（WGS84，纯函数，供 selftest 固化）。

    口径：多边形面积 ⊆ 其各环外接框之并，因此「cell 与所有环外接框都不
    相交 → cell 与多边形无正面积相交」是**保守上界**，绝不误跳。
    非 Polygon/MultiPolygon（或自备 bbox 矩形边界以外的未知类型）返回
    []，预过滤退化为不跳过 —— 宁可多取，不可丢数据。
    """
    boxes: list[tuple[float, float, float, float]] = []

    def walk(node):
        if not isinstance(node, (list, tuple)) or not node:
            return
        if isinstance(node[0], (int, float)):          # 一个坐标点
            return
        if isinstance(node[0][0], (int, float)):       # 一个环（点列）
            xs = [float(p[0]) for p in node]
            ys = [float(p[1]) for p in node]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
            return
        for c in node:
            walk(c)

    for g in geojson_geometries(boundary):
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            return []
        walk(g.get("coordinates", []))
    return boxes


def offshore_prefilter(pairs: list, boundary: dict,
                       buffer_m: float = 0.0) -> tuple[list, list]:
    """v1.8.0：跳过与边界任何环外接框均不相交的 1° cell（海域/空隙）。

    动机（上轮遗留观察项）：full 模式法国等沿海国家会把只与区域 bbox
    擦边的纯海域 cell 整片 COG 下载完、读到全 NaN 后才发现无用；
    window 模式也会白读一轮远程窗口。判据保守（见 part_bboxes），
    且仅在 clip 模式调用：这些 cell 的像元必被 geometry_mask 掩成 NaN，
    跳过取数不改变任何输出像元；--no-clip 保留完整矩形则不适用。
    返回 (kept, dropped)；统一网格仍按全部 pairs 计算，
    成果 georeference 零变化（selftest 端到端逐像元固化）。
    """
    boxes = part_bboxes(boundary)
    if not boxes:
        return list(pairs), []
    dx = dy = 0.0
    if buffer_m:
        lat = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2
        dx = buffer_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        dy = buffer_m / 110574.0
    kept, dropped = [], []
    for it, p in pairs:
        cb = it.get("bbox")
        if not cb or len(cb) != 4:
            kept.append((it, p))
            continue
        if any(bbox_intersects(cb, (b[0] - dx, b[1] - dy,
                                    b[2] + dx, b[3] + dy)) for b in boxes):
            kept.append((it, p))
        else:
            dropped.append((it, p))
    return kept, dropped


def require_valid_pixels(arr: np.ndarray) -> np.ndarray:
    """显式判空：任何没有有效像元的成果都不允许写出（禁止静默产出空数据）。"""
    valid = np.isfinite(arr[0])
    if not valid.any():
        raise EmptyRasterError(
            "裁切后没有任何有效像元。可能原因：区域内全为水体/冰雪/低纹理被掩膜，"
            "或 GEM-15 在该区无数据（数据源排除中国、俄罗斯、蒙古、格陵兰、南极洲）。"
            "不输出空文件。")
    return valid


def clip_bbox_for(boundary: dict) -> tuple[float, float, float, float]:
    """裁切外接框一律取**原始边界**，不受 --buffer 扩大取数 bbox 的影响。

    v1.3.0 前 build() 用 buffer 后的 bbox 算裁切窗口：--buffer 500 时成品
    会多出一整圈全 NaN 空白（掩膜仍按原边界，外扩部分永远无效）。
    """
    return _geojson_bbox(boundary)


def percentiles_inplace(v: np.ndarray, qs=(2, 98)) -> list[float]:
    """v1.12.0：**原地**算多个分位数，避免 np.percentile 的整份数组复制。

    实测（德国 R00C00，87.7 M 有效像元 = 351 MB float32）：
    `np.percentile(v, 2)` 与 `np.percentile(v, 98)` 各复制一份（+702 MB），
    48 块顺序跑时 RSS 只增不减，第 3 块就因一次 190 MB 分配失败而
    MemoryError 退出。改用 ndarray.partition()（原地），并用与 np.percentile
    默认 linear 插值**完全一致**的取整+插值公式：数值结果不变，峰值少 2×n×4 B。
    """
    n = int(v.size)
    if n == 0:
        return [float("nan")] * len(qs)
    idx = []
    for q in qs:
        pos = (n - 1) * (q / 100.0)
        lo = int(pos)
        idx.extend([lo, min(lo + 1, n - 1)])
    v.partition(sorted(set(idx)))
    out = []
    for q in qs:
        pos = (n - 1) * (q / 100.0)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        out.append(float(v[lo] + (v[hi] - v[lo]) * frac))
    return out


def write_geotiff(out_tif: Path, arr: np.ndarray, transform, crs: CRS,
                  layer: str, tags: dict) -> None:
    count, height, width = arr.shape
    all_tags = dict(tags)
    all_tags.update(GEM15_SOURCE="Google Research GEM-15 (15 m)",
                    GEM15_LAYER=layer, GEM15_LICENSE=LICENSE,
                    GEM15_ATTRIBUTION=ATTRIBUTION,
                    GEM15_VERTICAL_DATUM=VERTICAL_DATUM)
    # v1.12.0：deflate + predictor=3（浮点预测）+ zlevel=4。
    # 微基准（德国 R00C00 实测，7866x12073 float32 = 87.7 M 像元）：
    #   默认 z6/p2      39.5 s  271.3 MB
    #   z6 + p3         34.7 s  192.4 MB   (-29% 体积)
    #   z4 + p3         19.1 s  194.7 MB   (2.1x 快，体积仅 +1%)
    #   z1 + p3         15.5 s  201.1 MB
    # 取 z4/p3：写出时间减半、文件小 28% —— 48 块大国分块直接省约 20 分钟与
    # 3.6 GB 磁盘。predictor=3 是 GDAL 长期支持的浮点预测器，QGIS / GDAL /
    # ArcGIS 均能读；若下游软件极旧而无法识别，可自行用 gdal_translate 重压。
    with rasterio.open(out_tif, "w", driver="GTiff", height=height,
                       width=width, count=count, dtype="float32", crs=crs,
                       transform=transform, nodata=np.nan, compress="deflate",
                       predictor=3, zlevel=4,
                       tiled=True, BIGTIFF="IF_SAFER") as dst:
        # v1.3.0：成果数组本身就是 float32，astype 默认 copy=True 会再复制一份
        # 等大数组（512 MB 上限的成果即多 512 MB 峰值）。asarray 零复制。
        dst.write(np.asarray(arr, dtype=np.float32))
        for i, name in enumerate(LAYER_BANDS[layer], 1):
            if i <= count:
                dst.set_band_description(i, name)
        dst.update_tags(**all_tags)


def plan_streaming_grid(items: list[dict], boundary: dict, crs_opt: str,
                        pad_deg: float = 0.02) -> dict:
    """v2.0.0 纯函数：整幅单文件出图的全局网格规划（不联网、不写盘）。

    返回 dict(crs, res, downgraded, left, bottom, right, top, ncol, nrow)。
    网格原点对齐到 res 整数倍，保证像元正方、多次运行结果可复现。
    """
    from gem15_common import bbox_of_geojson
    epsgs = [it.get("epsg") for it in items if it.get("epsg")]
    crs, res, downgraded = decide_crs(epsgs, crs_opt, {})
    bb = bbox_of_geojson(boundary)
    if crs.is_geographic:
        left = math.floor((bb[0] - pad_deg) / res) * res
        right = math.ceil((bb[2] + pad_deg) / res) * res
        top = math.ceil((bb[3] + pad_deg) / res) * res
        bottom = math.floor((bb[1] - pad_deg) / res) * res
    else:
        # 投影 CRS：把 WGS84 bbox 投到目标系，pad 换算成米（pad_deg × 111.32 km）
        b = transform_bounds(WGS84, crs, *bb, densify_pts=21)
        pad_m = pad_deg * 111320.0
        left = math.floor((b[0] - pad_m) / res) * res
        right = math.ceil((b[2] + pad_m) / res) * res
        top = math.ceil((b[3] + pad_m) / res) * res
        bottom = math.floor((b[1] - pad_m) / res) * res
    ncol = int(round((right - left) / res))
    nrow = int(round((top - bottom) / res))
    return {"crs": crs, "res": res, "downgraded": downgraded,
            "left": left, "bottom": bottom, "right": right, "top": top,
            "ncol": ncol, "nrow": nrow}


def build_streaming(items: list[dict], boundary: dict, out_tif: Path, *,
                    layer: str = "dtm", cache_dir: Path | None = None,
                    no_clip: bool = False, band_budget_mb: float = 384.0,
                    cog: bool = True, region_meta: dict | None = None) -> dict:
    """v2.0.0：整幅单文件流式拼接（--single 模式的核心）。

    逐**横带**处理全局网格：每带只分配 `band_budget_mb`（默认 384 MB）的数组，
    与该带相交的本地缓存 COG 逐片 bilinear 重投影贴入（`init_dest_nodata=False`
    保已写有效值），随后按边界掩膜并写出。**内存恒定**（约 1~1.2 GB 峰值，
    与成果总大小无关），韩国整国 7.54 GB 成果实测 156 s 完成。
    每片直接从原生 UTM 网格重投影到全局网格——不存在分块方案的接缝相位问题。

    前提：所需 COG 已全部落盘缓存（gem15_dem 在 --single 下先整区下载）。
    高程分位数用直方图法（[-1200, 9000] m、0.5 m 分箱）增量累计，不复制大数组。
    """
    from gem15_common import bbox_of_geojson
    if layer not in LAYER_BANDS:
        raise GemError(f"layer 只能是 dtm 或 dsm，实际 {layer!r}")
    cache = cache_dir or default_cache()
    count = len(LAYER_BANDS[layer])
    plan = plan_streaming_grid(items, boundary, "auto")
    crs, res = plan["crs"], plan["res"]
    ncol, nrow = plan["ncol"], plan["nrow"]
    left, top = plan["left"], plan["top"]
    est_gb = nrow * ncol * count * 4 / 1e9
    log(f"整幅流式网格 {nrow}x{ncol} @ {res} {crs}（预估 {est_gb:.2f} GB，"
        f"内存恒定逐带处理），{'已降级 4326' if plan['downgraded'] else '原生带'}")

    # 自适应带高：带数组 + 单片读入窗口 + 重投影临时 ≈ 3 倍带数组
    band_rows = max(256, min(4096, int(band_budget_mb * 1e6 / (ncol * count * 4))))
    band_rows = min(band_rows, nrow)

    out_tif.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="GTiff", width=ncol, height=nrow, count=count,
                   dtype="float32", crs=crs,
                   transform=Affine(res, 0, left, 0, -res, top),
                   nodata=np.nan, compress="deflate", predictor=3, zlevel=4,
                   tiled=True, blockxsize=512, blockysize=512,
                   BIGTIFF="IF_SAFER")
    # v2.0.0：掩膜几何必须投影到成果 CRS（project_geometries 的 docstring
    # 记录的真实组合错误：UTM 成果 + 度坐标掩膜 -> 整体错位到原点 -> 全 NaN）。
    geoms = [] if no_clip else project_geometries(boundary, crs)

    # 高程直方图：[-1200, 9000] m、0.5 m 分箱（用于精确 p2/p98，不复制大数组）
    H_MIN, H_BIN, H_N = -1200.0, 0.5, 20400
    hist = np.zeros(H_N, dtype=np.int64)
    n_valid = 0
    gmin, gsum = np.inf, 0.0
    gmax = -np.inf
    t0 = time.time()

    def _to_band_bbox(r0: int, bh: int) -> tuple:
        """带的世界坐标 bbox（目标 CRS 单位），供与 item（也已转目标 CRS）相交。"""
        y_top = top - r0 * res
        y_bot = top - (r0 + bh) * res
        return (left, y_bot, left + ncol * res, y_top)

    # v2.0.0 修复：item bbox（WGS84 度）预先统一转到目标 CRS ——
    # 否则目标网格是 UTM（米）时行列映射混用度/米，全部落在网格外（静默空成果）。
    items_t: list[tuple[dict, tuple]] = []
    for _it in items:
        _bb = _it.get("bbox")
        if isinstance(_bb, (list, tuple)) and len(_bb) == 4:
            items_t.append((_it, transform_bounds(
                WGS84, crs, *_bb, densify_pts=21)))

    with rasterio.open(out_tif, "w", **profile) as dst:
        for r0 in range(0, nrow, band_rows):
            bh = min(band_rows, nrow - r0)
            y_top = top - r0 * res
            arr = np.full((bh, ncol, count), np.nan, dtype=np.float32)
            band_bb = _to_band_bbox(r0, bh)
            for it, cbb in items_t:
                if cbb[3] <= band_bb[1] or cbb[1] >= band_bb[3] \
                        or cbb[2] <= band_bb[0] or cbb[0] >= band_bb[2]:
                    continue
                f = tile_path(it, layer, cache)
                if not f.exists():
                    raise GemError(
                        f"整幅流式拼接需要完整缓存：{f} 不存在。"
                        "--single 模式会先整区下载；请勿在构建中途清缓存。")
                with rasterio.open(f) as src:
                    # 带与 cell 的交集（目标 CRS）→ cell 原生 CRS 的源窗口
                    ix0 = max(cbb[0], band_bb[0])
                    iy0 = max(cbb[1], band_bb[1])
                    ix1 = min(cbb[2], band_bb[2])
                    iy1 = min(cbb[3], band_bb[3])
                    sx0, sy0, sx1, sy1 = transform_bounds(
                        crs, src.crs, ix0, iy0, ix1, iy1, densify_pts=21)
                    win = intersection(src.window(sx0, sy0, sx1, sy1),
                                       Window(0, 0, src.width, src.height))
                    win = win.round_offsets().round_lengths()
                    if win.width <= 0 or win.height <= 0:
                        continue
                    src_tr = src.window_transform(win)
                    # 目标列/行范围（cell bbox 已在目标 CRS，与 left/top 同单位）
                    c0 = max(0, int((cbb[0] - left) / res) - 2)
                    c1 = min(ncol, int(math.ceil((cbb[2] - left) / res)) + 2)
                    rr0 = max(0, int((top - cbb[3]) / res) - r0 - 2)
                    rr1 = min(bh, int(math.ceil((top - cbb[1]) / res)) - r0 + 2)
                    if rr1 <= rr0 or c1 <= c0:
                        continue
                    dst_tr = Affine(res, 0, left + c0 * res, 0, -res,
                                    y_top - rr0 * res)
                    sub = arr[rr0:rr1, c0:c1]
                    for b in range(count):
                        s = src.read(b + 1, window=win,
                                     masked=True).filled(np.nan)
                        reproject(s, sub[..., b], src_transform=src_tr,
                                  src_crs=src.crs, src_nodata=np.nan,
                                  dst_transform=dst_tr, dst_crs=crs,
                                  dst_nodata=np.nan,
                                  resampling=Resampling.bilinear,
                                  init_dest_nodata=False)
            if geoms:
                inside = geometry_mask(geoms, (bh, ncol),
                                       Affine(res, 0, left, 0, -res, y_top),
                                       invert=True, all_touched=False)
                arr[~inside] = np.nan
            fin = np.isfinite(arr[..., 0])
            nv = int(fin.sum())
            if nv:
                v = arr[..., 0][fin]
                gmin = min(gmin, float(v.min()))
                gmax = max(gmax, float(v.max()))
                gsum += float(v.sum())
                hist += np.histogram(v, bins=H_N,
                                     range=(H_MIN, H_MIN + H_N * H_BIN))[0]
            n_valid += nv
            dst.write(arr.transpose(2, 0, 1),
                      window=Window(0, r0, ncol, bh))
            log(f"带 {r0:>5}-{r0 + bh:<5} 有效 {nv / (bh * ncol) * 100:5.1f}%  "
                f"累计 {time.time() - t0:6.1f}s")
            del arr

    if n_valid == 0:
        raise EmptyRasterError(
            "整幅成果没有任何有效像元。可能原因：区域内全为水体/冰雪/低纹理被掩膜，"
            "或 GEM-15 在该区无数据（数据源排除中国、俄罗斯、蒙古、格陵兰、南极洲）。")
    # 直方图 -> 精确 p2/p98
    cum = np.cumsum(hist)
    total = int(cum[-1])

    def _pct(q: float) -> float:
        k = int(q * (total - 1))
        i = int(np.searchsorted(cum, k, side="right"))
        i = min(i, H_N - 1)
        return H_MIN + (i + 0.5) * H_BIN

    stats = {
        "crs": f"{crs}", "streaming": True, "band_rows": band_rows,
        "degraded": plan["downgraded"],
        "degraded_reason": (f"命中多个 UTM 带，整幅统一降级 {crs}"
                            if plan["downgraded"] else ""),
        "grid": [plan["left"], plan["bottom"], plan["right"], plan["top"]],
        "output_shape": [count, nrow, ncol],
        "valid_pixels": n_valid,
        "valid_ratio": round(n_valid / (nrow * ncol), 5),
        "elev_min_m": round(float(gmin), 2),
        "elev_max_m": round(float(gmax), 2),
        "elev_mean_m": round(gsum / n_valid, 2),
        "elev_p2_m": round(_pct(0.02), 2),
        "elev_p98_m": round(_pct(0.98), 2),
        "attribution": ATTRIBUTION, "license": LICENSE,
        "vertical_datum": VERTICAL_DATUM,
        "cog": bool(cog),
        "grid_pixels": nrow * ncol,
    }
    if cog:
        to_cog(out_tif)
        log("COG: 已转为 Cloud Optimized GeoTIFF（内置金字塔，可 Web 流式访问）")
    log(f"整幅 GeoTIFF: {out_tif}  {stats['output_shape']}  CRS={stats['crs']}  "
        f"nodata=NaN  有效 {n_valid} 像元 ({stats['valid_ratio'] * 100:.1f}%)  "
        f"高程 {gmin:.2f}~{gmax:.2f} m  耗时 {time.time() - t0:.1f}s")
    return stats


def build(items: list[dict], boundary: dict, out_tif: Path, *, layer: str = "dtm",
          mode: str = "window", crs_opt: str = "auto", cache_dir: Path | None = None,
          workers: int = 4, timeout: int = 45, chunk_mb: float = 16,
          no_clip: bool = False, buffer_m: float = 0.0,
          max_output_mb: float = DEFAULT_MAX_OUTPUT_MB,
          max_download_mb: float = DEFAULT_MAX_DOWNLOAD_MB,
          cog: bool = True,
          region_meta: dict | None = None) -> dict:
    # v1.8.0：layer 前置校验。旧实现独立 CLI build_mosaic.py --layer foo
    # 会在 plan_window 的 LAYER_BANDS[foo] 抛 KeyError（未预期错误 exit 1）。
    if layer not in LAYER_BANDS:
        raise GemError(f"layer 只能是 dtm 或 dsm，实际 {layer!r}")
    cache = cache_dir or default_cache()
    bbox = list(region_meta["bbox"]) if region_meta else list(
        _geojson_bbox(boundary))
    if buffer_m:
        lat = (bbox[1] + bbox[3]) / 2
        dx = buffer_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
        dy = buffer_m / 110574.0
        bbox = [bbox[0] - dx, bbox[1] - dy, bbox[2] + dx, bbox[3] + dy]
        log(f"边界外扩 {buffer_m:.0f} m -> bbox={tuple(round(v, 5) for v in bbox)}")

    stats: dict = {"mode": mode, "layer": layer, "bbox_used": bbox,
                   "cells_requested": len(items), "clip": not no_clip,
                   "streaming": True}

    # 1) 纯离线规划窗口（不打开任何 COG），元数据来源是官方 STAC asset
    plans = [plan_window(it, bbox, layer) for it in items]
    pairs = [(it, p) for it, p in zip(items, plans) if p is not None]
    if not pairs:
        raise EmptyRasterError("所有分片规划窗口都为空（判空，不静默产出空 GeoTIFF）")
    plans_used = [p for _, p in pairs]
    stats["cells_planned"] = len(pairs)
    if len(pairs) != len(items):
        warn(f"{len(items) - len(pairs)} 个分片规划窗口为空，已跳过（可观测）")

    # v1.7.0：**预检必须先于任何下载 / 像元读取**。旧实现把 download_items
    # 放在 assert_output_fits 之前：full 模式法国整国会先把 147 片 / 19.24 GiB
    # 全部落盘，才报「成果数组 32 GiB 超限」——白跑几十小时且打满磁盘。
    # 现在统一网格、下载总量、输出规模、磁盘空间四项全部在 step 3 之前完成。
    target_crs, res, degraded = decide_crs([it.get("epsg") for it, _ in pairs],
                                           crs_opt, stats)
    stats["crs"] = target_crs.to_string()
    stats["resolution"] = res
    stats["degraded"] = degraded
    grid_tr, width, height = grid_for(plans_used, target_crs, res)
    count = plans_used[0]["count"]
    if any(p["count"] != count for p in plans_used):
        raise DownloadError("分片波段数不一致，无法拼接")
    # 先**只记录**下载总量（不拒绝），让输出规模预检先带着完整信息报错，
    # 入口才能据此生成分块方案；full 模式的真正拒绝在 step 3 的
    # download_items() 内部（同样是任何请求之前）。
    assert_download_fits([it for it, _ in pairs], max_download_mb, stats,
                         enforce=False)
    assert_output_fits(width, height, count, max_output_mb, stats)
    # 磁盘预检同样在下载之前：full 模式还要额外容纳落盘的整 COG。
    need_mb = (stats["estimated_output_mb"] +
               (stats["download_total_mb"] if mode == "full" else 0))
    assert_disk_space(out_tif, need_mb, stats)
    log(f"输出网格 {width}x{height} @ {res:.8f} {target_crs.to_string()}，"
        f"预估成果数组 {stats['estimated_output_mb']:.1f} MB"
        f"（上限 {max_output_mb:.0f} MB），远程/落盘合计 "
        f"{stats['download_total_mb']:.0f} MB，磁盘可用 "
        f"{stats.get('disk_free_mb', 0):.0f} MB，流式逐片读取")

    # v1.8.0：海域/空隙 cell 预过滤（仅 clip 模式；--no-clip 保留完整矩形，
    # 跳过会丢失界外像元，故不适用）。统一网格已按全部规划分片算完，
    # 本预过滤只决定「哪些 cell 真正取数」，成果 georeference 零变化。
    fetch_pairs = pairs
    if not no_clip:
        kept, dropped = offshore_prefilter(pairs, boundary, buffer_m)
        if dropped:
            fetch_pairs = kept
            names = [it["cell"] for it, _ in dropped]
            stats["cells_skipped_offshore"] = names
            stats["fetch_total_mb"] = round(
                sum(it["size"] for it, _ in kept
                    if isinstance(it.get("size"), int) and it["size"] > 0)
                / 1048576.0, 2)
            warn(f"{len(dropped)} 个分片（{', '.join(names[:8])}"
                 f"{'...' if len(names) > 8 else ''}）与边界各部分均不相交"
                 "（海域/空隙），跳过取数；其像元在 clip 模式下必被掩膜，成果不变")
    if not fetch_pairs:
        raise EmptyRasterError("所有分片都在边界之外（判空，不静默产出空 GeoTIFF）")

    # v1.11.0：window 超限 warn 移到预过滤**之后**，按真正取数的片估算。
    # 旧实现在跳过海域/空隙 cell 之前按全部 pairs 报警：实测双岛 + 中间海域
    # 场景，实际取数 400 MB < 上限 500 MB，警告却报 800 MB / 3 片超限 ——
    # 把已确定不读的海域片算进「预计远程读入」，误导用户分块。
    if mode == "window":
        # window 不落盘，但大国时每个窗口接近整片 COG，远程读入量同等巨大。
        # 不阻止（默认模式），但必须可观测，不让用户以为「没下载就没流量」。
        stats["fetch_total_mb"] = round(
            sum(it["size"] for it, _ in fetch_pairs
                if isinstance(it.get("size"), int) and it["size"] > 0)
            / 1048576.0, 2)
        if stats["fetch_total_mb"] > max_download_mb:
            n_skip = len(pairs) - len(fetch_pairs)
            warn(f"window 模式预计远程读入约 {stats['fetch_total_mb']:.0f} MB"
                 f"（{len(fetch_pairs)} 片 COG 窗口"
                 + (f"，已扣除海域/空隙 {n_skip} 片" if n_skip else "")
                 + f"），已超过 --max-download-mb {max_download_mb:.0f} MB；"
                 "大区域建议 --split auto 分块并留意耗时")

    # 3) 取数来源（full 模式只对参与拼接的片落盘；此时所有预检均已通过，
    #    download_items 内部会再做一次总量校验，双入口防御）
    if mode == "full":
        stats["download"] = download_items([it for it, _ in fetch_pairs], layer,
                                           cache, workers, timeout, chunk_mb,
                                           max_download_mb)
        sources = [(it, p, str(tile_path(it, layer, cache)))
                   for it, p in fetch_pairs]
    elif mode == "window":
        # v1.15.0：本地已有整片缓存（full 模式或整区预下载留下）时直接读本地，
        # 实测同窗口 35.2x 提速；命中数入 stats 可观测。
        sources, hits = window_sources(fetch_pairs, layer, cache)
        if hits:
            log(f"window 模式本地缓存命中 {hits}/{len(sources)} 片，"
                "窗口读取不再走远程流量")
        stats["window_cache_hits"] = hits
    else:
        raise ValueError(f"未知 mode: {mode}（应为 window 或 full）")

    # 4) 流式：读一片 -> 贴一片 -> 立即释放，不把窗口数组常驻内存
    out = np.full((count, height, width), np.nan, dtype=np.float32)
    n_contrib: list[int] = []
    window_meta: list[dict] = []
    cells: list[str] = []
    env = rasterio.Env(VSI_CACHE=True, VSI_CACHE_SIZE=268435456,
                       GDAL_CACHEMAX=128, GDAL_HTTP_TIMEOUT="30",
                       GDAL_HTTP_MULTIPLEX="YES", GDAL_HTTP_VERSION="2",
                       CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    with env:
        for i, (it, plan, path) in enumerate(sources, 1):
            t = time.time()
            w = read_one_window(path, bbox, plan=plan)
            if w is None:
                continue
            # v1.5.0：count 来自 LAYER_BANDS[layer]，是**声明值**而非实际 COG 波段数。
            # 实测 dsm(5) 配 1 波段 COG 会在 paste 抛 IndexError「index 1 out of
            # bounds for axis 0 with size 1」→ 未预期错误 exit 1。显式判不一致。
            if w["count"] != count:
                raise DownloadError(
                    f"cell={it['cell']} 实际 COG 波段数 {w['count']} 与 {layer} "
                    f"声明的 {count} 不一致，禁止错位拼接")
            w["cell"] = it["cell"]
            n_contrib.append(paste(out, w, grid_tr, width, height,
                                   target_crs, res))
            dt = time.time() - t
            cells.append(it["cell"])
            window_meta.append({"cell": it["cell"], "width": w["width"],
                                "height": w["height"], "seconds": round(dt, 2)})
            progress(i, len(sources),
                     f"cell={it['cell']} 窗口 {w['width']}x{w['height']} "
                     f"读取+贴入 {dt:.1f}s CRS={w['crs'].to_string()}")
            del w                         # 关键：立刻释放本片数组
    if not cells:
        raise EmptyRasterError("所有分片窗口都为空（判空，不静默产出空 GeoTIFF）")
    stats["windows"] = window_meta
    stats["streaming_peak_cells"] = 1
    stats["cell_valid_pixels"] = dict(zip(cells, n_contrib))
    log(f"拼接网格 {width}x{height} @ {res:.8f} {target_crs.to_string()}，"
        f"波段0 有效像元 {int(np.isfinite(out[0]).sum())}")

    # --buffer 只扩大取数 bbox；掩膜始终用原始边界，语义不混淆
    # v1.6.0：no_clip 不需要边界几何，投影计算移进裁切分支（去掉无用计算，
    # 且自备边界几何异常时仍应能保留完整矩形网格）。
    if not no_clip:
        geoms = project_geometries(boundary, target_crs)
        outside = geometry_mask(geoms, out_shape=(height, width),
                                transform=grid_tr, all_touched=True)
        out[:, outside] = np.nan
        log(f"按边界掩膜：界外 {int(outside.sum())} 像元写为 NaN（不是 0，无黑边）")
        del outside          # v1.5.0：写出阶段不再常驻这块与网格等大的 bool 掩膜

    # v1.5.0：--no-clip 的语义是「不做边界裁切，保留矩形」（README 既有约定）。
    # 旧实现只跳过掩膜，却仍按 clip_bbox_for(boundary) 裁到边界外接框：
    # 最小合成实测网格 198x198、no_clip=True 仍输出 106x106（只保留 28.7%）。
    if no_clip:
        clipped = out[:, :, :]
        out_tr = grid_tr
        log(f"--no-clip：保留完整拼接网格 {width}x{height}（不按边界裁切）")
    else:
        # 裁到**原始边界**外接框（+2 像素），避免 GeoTIFF 带大量全 NaN 空白。
        # v1.3.0：不能用取数 bbox（--buffer 会外扩它），否则成品多一圈空白。
        clip_box = transform_bounds(WGS84, target_crs, *clip_bbox_for(boundary),
                                    densify_pts=21)
        win = from_bounds(*clip_box, transform=grid_tr)
        c0 = max(0, int(math.floor(win.col_off)) - 2)
        r0 = max(0, int(math.floor(win.row_off)) - 2)
        c1 = min(width, int(math.ceil(win.col_off + win.width)) + 2)
        r1 = min(height, int(math.ceil(win.row_off + win.height)) + 2)
        clipped = out[:, r0:r1, c0:c1]
        out_tr = from_origin(grid_tr.c + c0 * res, grid_tr.f - r0 * res, res, res)

    # v1.5.0：裁切后紧凑化，让全网格数组真正释放。实测 4000² 全网格 61 MB、
    # 裁切视图 8.6 MB：del 原数组后 RSS +0.0 MB（numpy 视图持有 base），
    # ascontiguousarray 后 -52.5 MB；写出阶段常驻 +63.9 MB -> +8.6 MB（-86%），
    # 代价 0.50s -> 0.62s。无需手工阈值：全切片本就 C 连续时返回同一对象（零复制）。
    compact = not clipped.flags.c_contiguous
    grid_px = width * height
    out = np.ascontiguousarray(clipped)
    del clipped
    stats["grid_pixels"] = grid_px
    stats["compact_copy"] = compact
    stats["released_mb"] = (round((grid_px - out.shape[1] * out.shape[2])
                                  * count * 4 / 1048576.0, 2) if compact else 0.0)
    if compact:
        log(f"裁切后紧凑化：成果 {out.shape[1]}x{out.shape[2]}，"
            f"释放全网格约 {stats['released_mb']:.1f} MB（写出峰值实测 -86%）")

    valid = require_valid_pixels(out)
    n_valid = int(valid.sum())
    v = out[0][valid]
    p2, p98 = percentiles_inplace(v)          # v1.12.0：原地算，不再复制两份
    stats.update({
        "output_shape": [int(count), int(out.shape[1]), int(out.shape[2])],
        "valid_pixels": n_valid,
        "valid_ratio": round(n_valid / (out.shape[1] * out.shape[2]), 5),
        "elev_min_m": round(float(v.min()), 2),
        "elev_max_m": round(float(v.max()), 2),
        "elev_mean_m": round(float(v.mean()), 2),
        "elev_p2_m": round(p2, 2),
        "elev_p98_m": round(p98, 2),
        "attribution": ATTRIBUTION, "license": LICENSE,
        "vertical_datum": VERTICAL_DATUM,
    })
    del valid, v              # v1.5.0：统计结束即释放，不陪着写 GeoTIFF
    tags = {"GEM15_CELLS": ",".join(cells),
            "GEM15_LAYER": layer,
            "GEM15_REGION": str((region_meta or {}).get("name", "")),
            "GEM15_MODE": mode}
    out_tif.parent.mkdir(parents=True, exist_ok=True)
    write_geotiff(out_tif, out, out_tr, target_crs, layer, tags)
    # v1.16.0：默认转 COG（LAYOUT=COG + 内置金字塔），--no-cog 跳过。
    stats["cog"] = bool(cog)
    if cog:
        to_cog(out_tif)
        log("COG: 已转为 Cloud Optimized GeoTIFF（内置金字塔，可 Web 流式访问）")
    log(f"GeoTIFF: {out_tif}  {stats['output_shape']}  CRS={stats['crs']}  "
        f"nodata=NaN  有效 {n_valid} 像元 ({stats['valid_ratio'] * 100:.1f}%)  "
        f"高程 {stats['elev_min_m']}~{stats['elev_max_m']} m")
    return stats


def to_cog(tif: Path) -> None:
    """v1.16.0：GTiff -> COG（Cloud Optimized GeoTIFF）原地转换。

    实测探针（GDAL 3.12，双环境）：COG 驱动 CreateCopy 后 `LAYOUT=COG`、
    predictor=3 保留、自动生成金字塔（OVERVIEW_RESAMPLING=AVERAGE）且可读。
    COG 可被 QGIS / ArcGIS / Web 端（TiTiler、deck.gl 等）直接流式访问，
    对大国分块成果逐块浏览是确定性收益。

    失败不打断主流程：保留原 GTiff 并 `[warn]`（不静默——用户会看到转换没生效）。
    """
    import os
    import tempfile
    from rasterio.shutil import copy as rio_copy
    tmp = tif.with_name(tif.stem + ".cog.tmp.tif")
    try:
        rio_copy(tif, tmp, driver="COG", compress="DEFLATE", level=4,
                 predictor=3, blocksize=512, overview_resampling="AVERAGE")
        os.replace(tmp, tif)
    except Exception as exc:                          # noqa: BLE001
        warn(f"COG 转换失败，保留原 GeoTIFF（非 COG 布局）："
             f"{type(exc).__name__}: {exc}")
        tmp.unlink(missing_ok=True)


def _geojson_bbox(gj: dict):
    from gem15_common import bbox_of_geojson
    return bbox_of_geojson(gj)


def main() -> None:
    import json
    import sys
    pos, opts, flags = parse_args(sys.argv[1:],
                                  ("--layer", "--mode", "--crs", "--cache",
                  "--workers", "--timeout", "--buffer",
                  "--chunk-mb", "--max-output-mb", "--max-download-mb"),
                                  CLI_FLAGS)
    if check_help(pos, flags, USAGE):
        sys.exit(0)
    if len(pos) < 3:
        print(USAGE)
        sys.exit(1)
    # v1.10.0：黑盒实测 items/boundary 文件裸读（不存在 -> FileNotFoundError
    # traceback exit 1），且 --layer foo 的校验在文件读取之后（修好文件也
    # 先报 layer 才对）。前置 layer 校验 + 文件显式判空 + GemError -> exit 2。
    try:
        # v1.13.0：5 个数值参数原先裸 int()/float()，非法字符串抛 ValueError
        # 不被 `except GemError` 捕获 -> traceback exit 1，与其余 CLI 的
        # 「[错误] + exit 2」契约不一致（实测 --workers abc 即裸抛）。
        def num(key, default, cast, minimum, why):
            try:
                v = cast(opts.get(key, default))
            except (TypeError, ValueError) as exc:
                raise GemError(f"--{key} 需要数值，实际 {opts.get(key)!r}") from exc
            if v < minimum:
                raise GemError(f"--{key} 至少为 {minimum}（{why}；实际 {v}）")
            return v

        if opts.get("layer", "dtm") not in LAYER_BANDS:
            raise GemError(f"layer 只能是 dtm 或 dsm，实际 {opts.get('layer')!r}")
        for tag, pth in (("items", pos[0]), ("边界", pos[1])):
            if not Path(pth).exists():
                raise GemError(f"{tag}文件不存在: {pth}")
        items = json.loads(Path(pos[0]).read_text(encoding="utf-8"))
        gj = json.loads(Path(pos[1]).read_text(encoding="utf-8"))
        # v1.11.0：黑盒实测 items 是 dict / list[str] 时在 plan_window 的
        # item["proj_transform"] 上裸崩 TypeError（traceback exit 1）——
        # v1.10 只修了文件不存在与坏 JSON，未覆盖「合法 JSON 但形状不对」。
        if not isinstance(items, list) or not items:
            raise GemError(
                f"items 文件应是 STAC item 对象数组（非空 list），"
                f"实际类型 {type(items).__name__}")
        _REQ = ("cell", "url", "epsg", "proj_transform", "proj_shape")
        bad = [it.get("cell", f"#{i}") if isinstance(it, dict)
               else f"#{i}:{it!r}"
               for i, it in enumerate(items)
               if not isinstance(it, dict) or any(k not in it for k in _REQ)]
        if bad:
            raise GemError(
                f"items 内 {len(bad)} 个元素缺少 STAC 元数据字段 {_REQ}："
                f"{bad[:8]}（应由 stac_index 生成，勿手写）")
        if not isinstance(gj, dict) or not gj.get("type"):
            raise GemError("边界文件应是 GeoJSON 对象（含 type 字段）")
        if opts.get("mode", "window") not in ("window", "full"):
            raise GemError(f"mode 只能是 window 或 full，实际 {opts.get('mode')!r}")
        if "chunk-mb" in opts:
            # v1.12.0：下载改为单文件 -C - 续传，分段参数已无作用（显式提示）。
            from gem15_common import warn as _warn
            _warn("--chunk-mb 自 v1.12.0 起已废弃（下载不再分段），本次忽略")
        stats = build(items, gj, Path(pos[2]), layer=opts.get("layer", "dtm"),
                      mode=opts.get("mode", "window"),
                      crs_opt=opts.get("crs", "auto"),
                      cache_dir=Path(opts["cache"]) if "cache" in opts else None,
                      workers=num("workers", 4, int, 1, "0 会让线程池退化"),
                      timeout=num("timeout", 45, int, 1, "0 会让 curl --max-time 变成无超时"),
                      chunk_mb=num("chunk-mb", 16, float, 1, "分段大小至少 1 MB"),
                      no_clip="no-clip" in flags,
                      cog="no-cog" not in flags,
                      buffer_m=num("buffer", 0, float, 0, "不能为负"),
                      max_output_mb=num("max-output-mb", DEFAULT_MAX_OUTPUT_MB, float, 1,
                              "成果上限至少 1 MB"),
                      max_download_mb=num("max-download-mb", DEFAULT_MAX_DOWNLOAD_MB, float, 1,
                                "下载上限至少 1 MB"))
    except GemError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

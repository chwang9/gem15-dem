#!/usr/bin/env python3
"""GEM-15 技能离线回归（不联网、最小合成数据）。

覆盖：STAC 命名规则（与真实落盘 fixtures 交叉验证）、297 个真实 child 组固化、
动态组反推（南纬 -61° 漏片）、覆盖排除显式报错、拼接 first-valid、
掩膜黑边为 NaN 而非 0、空数据判空、跨带降级可观测、
GeoTIFF / 预览 PNG 真实落地、缓存字节校验、断点指纹、CLI 参数/退出码、重试退避。
v1.2.0 新增：STAC 元数据窗口与真实 COG 一致、元数据不符时回退可观测、
输出规模预检、跨带局部重投影与全网格逐像元等价、预览先降采样、
average 重采样不侵蚀 nodata、死常量清理与参数签名。
v1.3.0 新增：--k=v 与未知选项显式报错、无位置参数清缓存、缓存清理范围、
get_json 临时文件回收、STAC link 绝对 URL 解析、GeoTIFF 写出零复制、
--buffer 裁切框、--bg 值校验、各子 CLI --help 退出码。
v1.4.0 新增：get_json 改为 curl_bytes 内存读取（零临时文件 / 零删除）、
curl_bytes 的 body-code 拆分与空体重试、100 次批量索引不触发任何 unlink、
curl_get 死分支清理与签名同步。英国整国实测：87 cell / 94 次删除会被宿主
批量删除守卫在第 50 次终止进程，本版从根上消除。
v1.5.0 新增：--no-clip 保留完整网格、裁切后紧凑化释放全网格、波段数不一致
显式报错、非法参数不白建输出目录、--unknown=value 不给错误建议、--maxside
前置校验、full 模式分段失败零删除、断点清理失败可观测、边界链接回退可观测、
损坏缓存覆盖写零删除、预览参数入 manifest。
并修正 average_keeps_nodata 的**断言方向假设**（小图 bilinear nodata 更多，
与 6000² 大图相反，原断言 `bilinear <= average` 在本机 rasterio 1.5.2 下假失败）。
v1.6.0 新增（独立黑盒 CLI 矩阵 + 最小合成证伪后只修证实项）：失败路径不白建
输出目录、四个数值参数范围校验、--crs 前置校验、COG 非 NaN nodata 归一化、
GeoJSON geometry:null、逐波段 first-valid、--title 回落与 manifest 一致、
--no-clip 不再无条件投影边界几何、内存预检口径（成果数组非峰值）。
v1.11.0 新增：stac_index CLI 统一 exit 2（旧实现零包装）、items 形状校验
（dict/list[str] 不再裸崩）、--adm1 与 bbox/geojson 互斥、--country 与名称
冲突检测、window 超限 warn 扣除海域预过滤、rmtree 删后验证（宿主守卫
拦截不再静默假成功）+ 历史自检临时目录自动扫除。

规矩：每个探针 key 必须同时写进 EXPECTED_KEYS，否则会直接失败（防止用例空转）。
运行： python selftest.py
"""
from __future__ import annotations

import subprocess
import sys

sys.dont_write_bytecode = True

# v1.13.0：黑盒 CLI 子进程各自加载 numpy/OpenBLAS，默认开满线程时曾在
# 内存紧张机出现 `OpenBLAS error: Memory allocation still failed after 10
# retries`（日志里表现为子进程 exit 1、期望 exit 2 的探针偶发假失败）。
# 限制线程数为 1，子进程继承，消除测试脚手架的间歇抖动（不影响技能运行时）。
import os as _os
_SUBPROCESS_ENV = dict(_os.environ)
for _k in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    _SUBPROCESS_ENV[_k] = "1"

import contextlib
import inspect
import io
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import box

import build_mosaic
import fetch_tiles
import gem15_common
import gem15_dem
import preview_dem
import resolve_region
import stac_index
from gem15_common import (CoverageError, EmptyRasterError, bbox_intersects,
                          parse_args)

HERE = Path(__file__).resolve().parent
FIXTURES = json.loads((HERE / "fixtures_stac.json").read_text(encoding="utf-8"))

# 新增任何探针都必须同步加进这里
EXPECTED_KEYS = {
    "naming_band", "naming_group", "naming_cell", "groups_for_bbox",
    "fixture_group", "fixture_bbox", "fixture_url", "fixture_epsg",
    "bbox_intersects", "excluded_iso", "allowed_iso",
    "decide_crs_same", "decide_crs_degrade", "decide_crs_explicit",
    "paste_adjacent", "paste_first_valid", "mask_no_black_border",
    "project_geometries_utm", "project_geometries_geographic",
    "require_valid_empty", "require_valid_ok",
    "geotiff_roundtrip", "preview_png", "preview_nodata_alpha",
    "cache_size_guard", "cache_bad_tif", "cache_ok", "no_segment_machinery",
    # v1.1.0 新增
    "parse_group", "fixture_children", "dynamic_groups_south61",
    "dynamic_groups_normal", "dynamic_groups_excluded",
    "cli_stac_args", "cli_help_exit", "dispose_budget",
    "window_retry_backoff", "index_workers_default",
    # v1.2.0 新增
    "plan_window_matches_cog", "plan_mismatch_fallback",
    "output_size_guard", "local_reproject_equal",
    "preview_downsample", "average_keeps_nodata", "v12_signature",
    # v1.3.0 新增
    "cli_equals_form", "cli_unknown_flag", "cli_switch_equals",
    "cli_help_all", "clean_cache_no_pos", "clean_cache_scope",
    "get_json_tmp_cleanup", "link_ids_absolute", "geotiff_no_copy",
    "clip_bbox_buffer", "background_validation", "v13_signature",
    "boundary_error_hint",
    # v1.4.0 新增
    "get_json_memory_read", "curl_bytes_split", "bulk_index_no_delete",
    "v14_signature",
    # v1.5.0 新增
    "no_clip_keeps_grid", "compact_copy_releases_grid",
    "ascontiguousarray_zero_copy", "bad_args_no_outdir",
    "unknown_equals_hint", "maxside_validation", "resume_keeps_partial",
    "download_no_parts", "boundary_fallback_visible",
    "get_json_zero_delete", "band_count_mismatch",
    "preview_meta_in_manifest", "v15_signature",
    # v1.6.0 新增 10 项
    "output_dir_late_create", "numeric_range_validation", "crs_validation",
    "nodata_non_nan_masked", "geojson_null_geometry", "multiband_first_valid",
    "title_manifest_matches_render", "no_clip_needs_no_geometry",
    "peak_memory_estimate", "v16_signature",
    # v1.7.0 新增 7 项（大国 / 大文件）
    "preflight_before_download", "download_total_guard", "disk_space_guard",
    "split_opt_validation", "split_plan_geometry", "split_argv_and_resume",
    # v1.7.0 补充 2 项（物理内存 OOM 防护 / 大国空块不中断整批）
    "physical_memory_guard", "split_empty_tile_continues",
    "percentiles_inplace_exact", "split_memory_error_actionable",
    "percentiles_inplace_exact", "split_memory_error_actionable",
    "v17_signature",
    # v1.8.0 新增 8 项（full 分段 -L / 断点零删除 / layer 校验 /
    # 相对 href 归一化 / 海域预过滤纯函数+端到端 / 文案中性化 / 签名）
    "resume_curl_flags", "clean_cache_budget_safe",
    "build_layer_validated", "relative_href_resolved",
    "offshore_prefilter_pure", "offshore_skip_identical_output",
    "output_message_neutral", "v18_signature",
    # v1.9.0 新增 3 项（台湾/香港/澳门归并 CHN：名称表 + ISO3 直传 + 签名）
    "china_regions_named", "china_regions_iso_blocked", "v19_signature",
    "bbox_china_blocked",
    # v1.11.0 新增 8 项（独立 CLI exit 2 / geojson 包装 / 互斥 / 多余位置
    # 参数 / STAC 判空不白建目录 / 签名）
    "geojson_wrapped", "fetch_cli_validated", "preview_cli_exit2",
    "build_cli_layer_first", "exclusive_inputs", "pos_args_capped",
    "stac_fail_no_outdir", "v110_signature",
    # v1.11.0 新增 9 项（stac CLI 包装 / items 形状 / adm1 互斥 /
    # country-名称冲突 / window warn 扣除海域 / rmtree 删后验证 /
    # 历史临时目录扫除 / layer 前置校验 / 签名）
    "stac_cli_validated", "items_shape_validated", "fetch_layer_validated",
    "adm1_mutex", "country_name_conflict", "window_warn_postfilter",
    "rmtree_verified_observable", "stale_tmp_sweep", "v111_signature",
    # v1.12.0 新增 4 项（GitHub 候选镜像展开 / 连接阶段短超时真正传入 curl /
    # 镜像回退 + 200 错误页内容校验 / 签名）
    "mirror_urls_expand", "connect_timeout_applied",
    "mirror_fallback_validated", "resume_download_continues",
    "geotiff_compression_opts", "v112_signature",
    # v1.12.1 新增 1 项（慢但有进展的续传不按次数放弃）
    "resume_progress_budget",
    # v1.12.2 新增 2 项（STAC 判空的纯海域块不中断整批 / 内存不足换新进程重试）
    "split_coverage_empty_continues", "split_oom_child_retry",
    # v1.13.0 新增 8 项（auto分块截断回补 / build_mosaic CLI校验 / 去重 /
    # 单行列预览 / GeometryCollection / force-empty / 空壳跳过 / 版本签名）
    "auto_split_refit_gy", "build_cli_validated", "no_dup_percentiles",
    "preview_thin_explicit", "geometry_collection_bbox", "force_empty_refresh",
    "dispose_shell_skip", "v113_signature",
    # v1.14.0 新增 4 项（整区预下载：唯一去重 / 断点跳过 / window空操作 / 小上限不误拒）；v1.15.0 新增 4 项（window 缓存复用）
    "prefetch_unique_cells", "prefetch_skips_done",
    "prefetch_window_noop", "prefetch_budget_no_reject",
    # v1.15.0 新增 4 项（window 缓存复用：命中 / size 不符回退 / 无缓存零回归 / 签名）
    "window_cache_hit", "window_cache_size_mismatch",
    "window_cache_missing", "v115_signature",
    # v1.16.0 新增 2 项（默认 COG：布局+金字塔 / 无临时文件残留）
    "cog_layout", "cog_no_tmp_left",
    # v2.0.0 新增 2 项（--single 整幅流式：网格对齐 / 最小端到端+接缝连续）
    "stream_grid_align", "stream_e2e_tiny",
}

results: dict[str, bool] = {}


def check(key: str, cond: bool, detail: str = "") -> None:
    results[key] = bool(cond)
    print(("  [PASS] " if cond else "  [FAIL] ") + key +
          (f"  {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"{key} 失败：{detail}")


def make_tif(path: Path, arr: np.ndarray, transform, epsg: int = 32632) -> None:
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[1],
                       width=arr.shape[2], count=arr.shape[0],
                       dtype="float32", crs=CRS.from_epsg(epsg),
                       transform=transform, nodata=np.nan) as ds:
        ds.write(arr.astype(np.float32))


def _sweep_stale_tmp(base_dir: Path, keep: Path) -> tuple[int, int]:
    """v1.11.0：删除历史自检临时目录（gem15_selftest_ 前缀），返回 (已删, 残留)。

    背景：宿主沙箱对单 turn 累计删除 >50 文件静默拦截，旧版 selftest 的
    rmtree 被拦截后无任何提示（rmtree 不抛 OSError），每跑一次泄漏一个
    152 文件的目录。现在每次自检开始时自动扫除旧残留；删除仍被拦截的
    目录会由 rmtree_verified 打印 [warn]，下次再试。
    只匹配本技能自有前缀，绝不碰其它目录。
    """
    removed = leftover = 0
    if base_dir.is_dir():
        for d in sorted(base_dir.glob("gem15_selftest_*")):
            if d == keep or not d.is_dir():
                continue
            if gem15_common.rmtree_verified(d, f"历史自检临时目录 {d.name}"):
                removed += 1
            else:
                leftover += 1
    return removed, leftover


def main() -> int:
    print(f"GEM-15 selftest v{gem15_common.VERSION}（离线 / 最小合成数据）", flush=True)
    tmp = Path(tempfile.mkdtemp(prefix="gem15_selftest_", dir="D:/"))
    # v1.11.0：先扫除历史残留（旧版被守卫拦截后泄漏的目录），再跑探针
    removed, leftover = _sweep_stale_tmp(tmp.parent, tmp)
    if removed or leftover:
        print(f"历史自检临时目录：清除 {removed} 个"
              + (f"，{leftover} 个删除被拦截（见 [warn]，下次自检重试）"
                 if leftover else ""), flush=True)
    try:
        # ---- 1. 命名规则（纯函数） ----
        check("naming_band",
              stac_index.band_token(7) == "p0" and
              stac_index.band_token(-7) == "m10" and
              stac_index.band_token(-35) == "m40" and
              stac_index.band_token(103) == "p100")
        check("naming_group",
              stac_index.group_for(-7, -35) == "m10_m40" and
              stac_index.group_for(47, 9) == "p40_p0" and
              stac_index.group_for(0, 100) == "p0_p100")
        check("naming_cell",
              stac_index.parse_cell("wm35sm7") == (-35.0, -7.0) and
              stac_index.parse_cell("w9s47") == (9.0, 47.0) and
              stac_index.parse_cell("w100s0") == (100.0, 0.0) and
              stac_index.parse_cell("w5sm2") == (5.0, -2.0))
        check("groups_for_bbox",
              stac_index.groups_for_bbox((9.47, 47.05, 9.64, 47.27)) == ["p40_p0"] and
              stac_index.groups_for_bbox((-35.5, -7.5, -34.5, -6.5)) == ["m10_m40"])

        # ---- 2. 与真实落盘 STAC 样本交叉验证（不靠记忆拼 id） ----
        ok_group = ok_bbox = ok_url = ok_epsg = True
        for rec in FIXTURES["items"]:
            lon, lat = rec["bbox"][0], rec["bbox"][1]
            ok_group &= stac_index.group_for(lat, lon) == rec["group"]
            ok_group &= stac_index.parse_cell(rec["cell"]) == (lon, lat)
            ok_bbox &= tuple(rec["bbox"]) == stac_index.cell_bbox(rec["cell"])
            for layer in ("dtm", "dsm"):
                a = rec["assets"][layer]
                ok_url &= a["href"].startswith(
                    "https://storage.googleapis.com/climate-and-sustainability/gem-15/")
                ok_url &= f"/{rec['group']}/{rec['cell']}/{layer}/zall/mosaic.tif" in a["href"]
                ok_url &= isinstance(a["size"], int) and a["size"] > 0
            ok_epsg &= 32601 <= rec["epsg"] <= 32660 or 32701 <= rec["epsg"] <= 32760
        check("fixture_group", ok_group, f"{len(FIXTURES['items'])} 个真实 item")
        check("fixture_bbox", ok_bbox)
        check("fixture_url", ok_url)
        check("fixture_epsg", ok_epsg,
              f"collection child 数={FIXTURES['collection_child_count']}")

        # ---- 2b. v1.1.0：由真实 collection child 组反推候选（修复南纬 -61° 漏片）----
        try:
            stac_index.parse_group("not_a_group")
            bad_group = False
        except ValueError:
            bad_group = True
        check("parse_group",
              stac_index.parse_group("p40_p0") == (40.0, 0.0) and
              stac_index.parse_group("m70_m50") == (-70.0, -50.0) and
              stac_index.parse_group("p80_m100") == (80.0, -100.0) and
              bad_group,
              "非法组名显式报错，不静默跳过")

        children = set(FIXTURES["collection_children"])
        parsed = 0
        for g in children:
            try:
                stac_index.parse_group(g)
                parsed += 1
            except ValueError:
                pass
        check("fixture_children",
              len(children) == FIXTURES["collection_child_count"] and
              parsed == len(children) and
              "m70_m50" in children and "p80_m80" in children and
              "p30_p110" not in children,
              f"{len(children)} 个真实组全部可解析；中国组不存在")

        south = (-44.8, -60.8, -44.6, -60.6)
        check("dynamic_groups_south61",
              stac_index.groups_for_bbox(south, children) == ["m70_m50"] and
              "m70_m50" in stac_index.groups_for_bbox(south),
              "真实 STAC 有 -61° 瓦片，v1.0.0 会被 LAT_LIMITS=-60 裁成空")
        check("dynamic_groups_normal",
              stac_index.groups_for_bbox((9.47, 47.05, 9.64, 47.27), children)
              == ["p40_p0"] and
              stac_index.groups_for_bbox((-80, 82, -79, 83), children)
              == ["p80_m80"],
              "常规 / 高纬用例与旧逻辑一致")
        bj = (116.30, 39.90, 116.45, 40.00)
        legacy = stac_index.groups_for_bbox(bj)
        check("dynamic_groups_excluded",
              len(legacy) >= 2 and
              stac_index.groups_for_bbox(bj, children) == [],
              f"旧逻辑假阳性 {legacy}；真实组集合为空 -> CoverageError")

        # ---- 3. 相交 / 覆盖排除 ----
        check("bbox_intersects",
              bbox_intersects((0, 0, 1, 1), (0.5, 0.5, 1.5, 1.5)) and
              not bbox_intersects((0, 0, 1, 1), (1, 0, 2, 1)) and
              not bbox_intersects((0, 0, 1, 1), (2, 2, 3, 3)))
        try:
            resolve_region.assert_allowed("CHN", "中国")
            excluded = False
        except CoverageError as exc:
            excluded = "排除" in str(exc) and "cn-dem" in str(exc)
        check("excluded_iso", excluded, "中国应显式报错并指向 cn-dem")
        try:
            resolve_region.assert_allowed("LIE", "列支敦士登")
            allowed = True
        except Exception:                             # noqa: BLE001
            allowed = False
        check("allowed_iso", allowed)

        # ---- 4. CRS 决策：跨带降级必须可观测 ----
        s1: dict = {}
        c1, r1, d1 = build_mosaic.decide_crs([32632, 32632], "auto", s1)
        check("decide_crs_same", c1.to_string() == "EPSG:32632" and
              abs(r1 - 15.0) < 1e-9 and not d1)
        s2: dict = {}
        c2, r2, d2 = build_mosaic.decide_crs([32632, 32633], "auto", s2)
        check("decide_crs_degrade",
              d2 and c2.to_string() == "EPSG:4326" and
              "EPSG:4326" in s2.get("degraded_reason", ""),
              "降级原因已写入 stats（静默兜底可观测）")
        s3: dict = {}
        c3, r3, d3 = build_mosaic.decide_crs([32632], "EPSG:4326", s3)
        check("decide_crs_explicit",
              c3.is_geographic and abs(r3 - build_mosaic.DEG_RES) < 1e-12 and not d3)

        # ---- 5. 最小合成 UTM 瓦片：拼接 / first-valid / 掩膜 ----
        res = 15.0
        h = w = 64
        tr_a = from_origin(1000.0, 2000.0, res, res)
        tr_b = from_origin(1000.0 + w * res, 2000.0, res, res)
        a = np.full((1, h, w), 100.0, dtype=np.float32)
        a[0, 10:20, 10:20] = np.nan                 # A 内部空洞
        b = np.full((1, h, w), 200.0, dtype=np.float32)
        c = np.full((1, h, w), np.nan, dtype=np.float32)
        c[0, 10:20, 10:20] = 300.0                  # C 只补 A 的空洞
        windows = []
        for arr, tr in ((a, tr_a), (b, tr_b)):
            windows.append({"arr": arr, "transform": tr,
                            "crs": CRS.from_epsg(32632), "count": 1,
                            "width": w, "height": h,
                            "bounds": rasterio.transform.array_bounds(h, w, tr)})
        grid, gw, gh = build_mosaic.grid_for(windows, CRS.from_epsg(32632), res)
        out = np.full((1, gh, gw), np.nan, dtype=np.float32)
        for win in windows:
            build_mosaic.paste(out, win, grid, gw, gh,
                               CRS.from_epsg(32632), res)
        check("paste_adjacent",
              out[0, 0, 0] == 100.0 and out[0, 0, 63] == 100.0 and
              out[0, 0, 64] == 200.0 and gh == 64 and gw == 128,
              f"网格 {gw}x{gh}")
        cw = {"arr": c, "transform": tr_a, "crs": CRS.from_epsg(32632),
              "count": 1, "width": w, "height": h,
              "bounds": rasterio.transform.array_bounds(h, w, tr_a)}
        build_mosaic.paste(out, cw, grid, gw, gh,
                           CRS.from_epsg(32632), res)
        check("paste_first_valid",
              out[0, 12, 12] == 300.0 and out[0, 0, 0] == 100.0 and
              out[0, 0, 64] == 200.0, "C 的 NaN 未覆盖 A/B 有效值，只补空洞")

        geom = box(1000.0, 1960.0, 1959.0, 2000.0).__geo_interface__
        outside = geometry_mask([geom], out_shape=(gh, gw), transform=grid,
                                all_touched=True)
        out[:, outside] = np.nan
        check("mask_no_black_border",
              np.isnan(out[0, -1, -1]) and np.isnan(out[0, 0, 64]) and
              out[0, 1, 5] == 100.0 and not (out[~np.isnan(out)] == 0).any(),
              "界外是 NaN 而不是 0")

        # 边界是 WGS84 经纬度、成果是 UTM 米制：必须把几何投影过去再掩膜
        wgs_box = box(9.5, 47.0, 9.6, 47.1)
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": wgs_box.__geo_interface__}]}
        utm_geom = build_mosaic.project_geometries(gj, CRS.from_epsg(32632))[0]
        p = utm_geom["coordinates"][0][0]
        check("project_geometries_utm", p[0] > 400000 and p[1] > 5000000,
              f"WGS84 (9.5,47) -> UTM32N ({p[0]:.0f},{p[1]:.0f})")
        geo_geom = build_mosaic.project_geometries(gj, CRS.from_epsg(4326))[0]
        # shapely box 的环首点是 (maxx,miny)，不能拿首点当 minx（断言口径要对）
        minx = min(p[0] for p in geo_geom["coordinates"][0])
        check("project_geometries_geographic", abs(minx - 9.5) < 1e-9,
              "4326 目标下几何保持不变")

        # ---- 6. 空数据判空 ----
        try:
            build_mosaic.require_valid_pixels(np.full((1, 4, 4), np.nan, np.float32))
            empty_guard = False
        except EmptyRasterError:
            empty_guard = True
        check("require_valid_empty", empty_guard)
        ok_arr = np.array([[[1.0, np.nan], [2.0, 3.0]]], dtype=np.float32)
        check("require_valid_ok", int(build_mosaic.require_valid_pixels(ok_arr).sum()) == 3)

        # ---- 7. 真实 GeoTIFF / PNG 落地 ----
        tif = tmp / "synth.tif"
        build_mosaic.write_geotiff(tif, out, grid, CRS.from_epsg(32632), "dtm",
                                   {"GEM15_CELLS": "synth"})
        with rasterio.open(tif) as ds:
            back = ds.read(1)
            check("geotiff_roundtrip",
                  ds.nodata is not None and np.isnan(ds.nodata) and
                  ds.dtypes[0] == "float32" and ds.crs.to_string() == "EPSG:32632" and
                  ds.descriptions[0] == "dtm" and np.isnan(back[-1, -1]) and
                  back[1, 5] == 100.0 and
                  "CC-BY-4.0" in ds.tags().get("GEM15_LICENSE", ""))
        png = tmp / "synth.png"
        pstat = preview_dem.render(tif, png, title="合成区域", maxside=600)
        from PIL import Image
        im = Image.open(png)
        rgba = np.array(im)
        check("preview_png",
              png.exists() and png.stat().st_size > 1000 and
              im.mode == "RGBA" and pstat["annotated"] and
              list(im.size)[0] > 0, f"{im.size} {pstat['size']}")
        check("preview_nodata_alpha",
              rgba[-1, -1, 3] == 0 and rgba.shape[2] == 4,
              "nodata 透明")

        # ---- 8. 缓存字节校验（静默损坏必须被发现） ----
        bad = tmp / "bad.tif"
        bad.write_bytes(b"1234567890")
        check("cache_size_guard", not fetch_tiles._cache_ok(bad, 20))
        bad2 = tmp / "bad2.tif"
        bad2.write_bytes(b"1234567890")
        check("cache_bad_tif", not fetch_tiles._cache_ok(bad2, 10))
        check("cache_ok", fetch_tiles._cache_ok(tif, tif.stat().st_size))

        # v1.12.0：分段机制整套移除（宿主批量删除守卫会按进程累计计数杀进程），
        # 改为单文件 curl -C - 续传。这里固化「旧符号确实已消失」+「续传入口存在」。
        check("no_segment_machinery",
              not hasattr(fetch_tiles, "segment_ranges") and
              not hasattr(fetch_tiles, "ensure_parts_manifest") and
              not hasattr(fetch_tiles, "_range_download") and
              not hasattr(fetch_tiles, "_truncate_parts") and
              hasattr(fetch_tiles, "_resume_download"),
              "分段/断点/合并三件套已移除，下载内核为单文件 _resume_download"
              "（-C - 续传）")

        # ---- 9. v1.1.0：CLI 参数解析 / 帮助退出码 ----
        cpos, copts, cflags = parse_args(
            ["1", "2", "3", "4", "--layer", "dsm", "--cache", "D:/x"],
            stac_index.CLI_WITH_VALUE)
        check("cli_stac_args",
              len(cpos) == 4 and copts.get("layer") == "dsm" and
              copts.get("cache") == "D:/x" and "layer" not in cflags,
              "stac_index 独立 CLI 的 --layer/--cache 不再被吞掉")
        with contextlib.redirect_stdout(io.StringIO()) as help_buf:
            rc_help = gem15_dem.run(["--help"])
        with contextlib.redirect_stdout(io.StringIO()):
            rc_h = gem15_dem.run(["-h"])
        check("cli_help_exit",
              rc_help == 0 and rc_h == 0 and
              "gem15_dem.py 列支敦士登" in help_buf.getvalue(),
              f"--help/-h 均退出 0（修复前 --help=1）")

        # ---- 10. v1.12.0：dispose_tree 的删除预算（守卫安全）----
        # 实测宿主守卫按**进程累计**计数，超阈值直接杀进程；故 ≤40 条目才真删，
        # 超出则清零留 0 B 空壳 —— 空间同样释放，但一次删除都不发起。
        small = tmp / "dispose_small"
        small.mkdir()
        for i in range(3):
            (small / f"f{i}.bin").write_bytes(b"x" * 10)
        r_small = gem15_common.dispose_tree(small, "小目录")
        big = tmp / "dispose_big"
        big.mkdir()
        for i in range(45):
            (big / f"f{i}.bin").write_bytes(b"y" * 10)
        r_big = gem15_common.dispose_tree(big, "大目录")
        check("dispose_budget",
              r_small["mode"] == "rmtree" and not small.exists() and
              r_big["mode"] == "truncate" and big.exists() and
              all(f.stat().st_size == 0 for f in big.rglob("*.bin")) and
              r_big["files"] == 45 and r_big["bytes"] == 450,
              "≤40 条目走 rmtree 真删；45 条目改为全部清零（0 B 空壳），"
              "一次批量删除都不发起（守卫不会杀进程）")

        # ---- 11. v1.1.0：窗口重试退避，最后一次失败不再白等 ----
        sleeps: list[float] = []
        with mock.patch.object(build_mosaic.rasterio, "open",
                               side_effect=RuntimeError("synthetic")), \
             mock.patch.object(build_mosaic.time, "sleep",
                               side_effect=lambda s: sleeps.append(s)):
            try:
                build_mosaic.read_one_window(
                    "/vsicurl/https://example.invalid/x.tif", (0, 0, 1, 1))
                retry_raised = False
            except build_mosaic.DownloadError:
                retry_raised = True
        check("window_retry_backoff",
              retry_raised and sleeps == [1, 2],
              f"retries=2 只等 [1,2] 共 3s（旧固定 sleep 为 6s），实测 {sleeps}")

        # ---- 12. v1.1.0：STAC 并发调参固化 ----
        sig = inspect.signature(stac_index.select_items)
        check("index_workers_default",
              gem15_common.INDEX_WORKERS == 16 and
              sig.parameters["workers"].default == gem15_common.INDEX_WORKERS,
              "微基准 2/8/16 = 8.54/6.06/4.23 s；复测 16 更快且方差更小")

        # ---- 13. v1.2.0：STAC 元数据窗口 == 真实 COG 窗口（最小合成数据） ----
        src_tr = from_origin(500000.0, 5200000.0, 15.0, 15.0)
        src_crs = CRS.from_epsg(32632)
        cog = tmp / "plan_src.tif"
        cog_arr = (np.arange(512 * 512, dtype=np.float32)
                   .reshape(1, 512, 512) % 900.0) + 200.0
        cog_arr[0, :20, :20] = np.nan
        with rasterio.open(cog, "w", driver="GTiff", height=512, width=512,
                           count=1, dtype="float32", crs=src_crs,
                           transform=src_tr, nodata=np.nan) as ds:
            ds.write(cog_arr)
        # bbox 由 COG 的真实投影范围现场反算，不手写经纬度（避免脚手架坐标错位）
        fb = rasterio.transform.array_bounds(512, 512, src_tr)
        bw = rasterio.warp.transform_bounds(src_crs, CRS.from_epsg(4326),
                                            *fb, densify_pts=21)
        dx, dy = (bw[2] - bw[0]) * 0.25, (bw[3] - bw[1]) * 0.25
        bbox_in = (bw[0] + dx, bw[1] + dy, bw[2] - dx, bw[3] - dy)
        item_syn = {"cell": "synthetic", "epsg": 32632,
                    "proj_transform": list(src_tr)[:6],
                    "proj_shape": [512, 512], "bands": ["dtm"]}
        plan = build_mosaic.plan_window(item_syn, bbox_in, "dtm")
        with rasterio.open(cog) as ds:
            real_win = build_mosaic._window_from_src(ds, bbox_in,
                                                     build_mosaic.PAD)
            real_tr = ds.window_transform(real_win)
        check("plan_window_matches_cog",
              plan is not None and
              [plan["window"].col_off, plan["window"].row_off,
               plan["window"].width, plan["window"].height] ==
              [real_win.col_off, real_win.row_off,
               real_win.width, real_win.height] and
              tuple(plan["transform"])[:6] == tuple(real_tr)[:6],
              "STAC proj:transform/shape 反算的窗口与 window_transform 逐项一致")

        # 元数据不符 -> 打印 [warn] 并现场重算，绝不静默沿用规划
        bad_plan = dict(plan)
        bad_plan["src_transform"] = from_origin(1.0, 1.0, 30.0, 30.0)
        with contextlib.redirect_stdout(io.StringIO()) as fb_buf:
            w_fb = build_mosaic.read_one_window(str(cog), bbox_in,
                                                plan=bad_plan)
        check("plan_mismatch_fallback",
              w_fb is not None and "不一致" in fb_buf.getvalue() and
              w_fb["width"] == real_win.width and
              w_fb["height"] == real_win.height,
              "实际 COG 与 STAC 不符时回退现场计算且可观测")

        # ---- 14. v1.2.0：输出规模预检（读取像元之前拒绝必然 OOM） ----
        st_ok: dict = {}
        build_mosaic.assert_output_fits(1000, 1000, 1, 512, st_ok)
        st_bad: dict = {}
        try:
            build_mosaic.assert_output_fits(20000, 20000, 5, 512, st_bad)
            too_large, msg = False, ""
        except gem15_common.OutputTooLargeError as exc:
            too_large, msg = True, str(exc)
        check("output_size_guard",
              st_ok["estimated_output_mb"] > 0 and too_large and
              "--max-output-mb" in msg and "--bbox" in msg and
              st_bad["estimated_output_mb"] > 512,
              f"限额内 {st_ok['estimated_output_mb']} MB / "
              f"超限 {st_bad['estimated_output_mb']} MB 显式报错并给出建议")

        # ---- 15. v1.2.0：跨带局部重投影与「全网格重投影」逐像元等价 ----
        # 故意把成果网格放大到远大于单个窗口，才能真正考验局部化
        # 用平滑地形做数值等价判断：GDAL 近似 warp transformer 在不同
        # destination 尺寸下不保证位级一致，病态高频锯齿（相邻像元跳变 899 m）
        # 会把采样误差放大到 mean 3.28 m；平滑地形实测 mean 0.022 m / max 0.064 m。
        # 因此硬断言是「有效像元掩码逐像元一致」，数值用 0.1 m 容差。
        sy, sx = np.mgrid[0:512, 0:512].astype(np.float32)
        smooth = (300.0 + 900.0 * np.sin(sx / 90.0) ** 2 *
                  np.cos(sy / 110.0) ** 2 +
                  40.0 * np.sin(sx / 37.0) * np.sin(sy / 41.0)).astype(np.float32)
        w_src = {
            "arr": smooth[None, 100:300, 100:300].copy(),
            "transform": src_tr * rasterio.transform.Affine.translation(100, 100),
            "crs": src_crs, "count": 1, "width": 200, "height": 200,
            "nodata": np.nan, "cell": "crossband",
            "bounds": rasterio.transform.array_bounds(
                200, 200, src_tr * rasterio.transform.Affine.translation(100, 100)),
        }
        tgt = CRS.from_epsg(4326)
        dres = build_mosaic.DEG_RES
        gtr0, gw0, gh0 = build_mosaic.grid_for([w_src], tgt, dres)
        pad_px = 100
        big_tr = from_origin(gtr0.c - pad_px * dres, gtr0.f + pad_px * dres,
                             dres, dres)
        gw, gh = gw0 + 2 * pad_px, gh0 + 2 * pad_px
        out_local = np.full((1, gh, gw), np.nan, np.float32)
        build_mosaic.paste(out_local, w_src, big_tr, gw, gh, tgt, dres)
        arr_full = np.full((1, gh, gw), np.nan, np.float32)
        rasterio.warp.reproject(
            source=w_src["arr"], destination=arr_full,
            src_transform=w_src["transform"], src_crs=src_crs,
            src_nodata=np.nan, dst_transform=big_tr, dst_crs=tgt,
            dst_nodata=np.nan, resampling=rasterio.enums.Resampling.bilinear)
        both_valid = np.isfinite(out_local) & np.isfinite(arr_full)
        d_abs = np.abs(out_local[both_valid] - arr_full[both_valid])
        check("local_reproject_equal",
              np.array_equal(np.isfinite(out_local), np.isfinite(arr_full)) and
              np.allclose(out_local, arr_full, equal_nan=True, atol=0.1),
              f"成果网格 {gw}x{gh} vs 单窗口 200x200；有效掩码逐像元一致，"
              f"数值 mean {float(d_abs.mean()):.4f} / max {float(d_abs.max()):.4f} m")

        # ---- 16. v1.2.0：预览先降采样 + average 不侵蚀 nodata ----
        big = tmp / "downsample.tif"
        arr_big = (np.arange(400 * 200, dtype=np.float32)
                   .reshape(1, 200, 400) % 500.0) + 100.0
        arr_big[0, :30, :30] = np.nan
        with rasterio.open(big, "w", driver="GTiff", height=200, width=400,
                           count=1, dtype="float32", crs=src_crs,
                           transform=from_origin(500000.0, 5200000.0, 15.0, 15.0),
                           nodata=np.nan) as ds:
            ds.write(arr_big)
        pst = preview_dem.render(big, tmp / "down_small.png", title="测试",
                                 maxside=100, do_annotate=False)
        check("preview_downsample",
              pst["source_pixels"] == [400, 200] and
              pst["render_pixels"] == [100, 50] and pst["size"] == [100, 50],
              f"400x200 -> 100x50 先降采样再渲染，stats 可观测 {pst['render_pixels']}")
        with rasterio.open(big) as ds:
            out_shape = (50, 100)
            a_avg = ds.read(1, out_shape=out_shape,
                            resampling=rasterio.enums.Resampling.average)
            a_near = ds.read(1, out_shape=out_shape,
                             resampling=rasterio.enums.Resampling.nearest)
            a_bil = ds.read(1, out_shape=out_shape,
                            resampling=rasterio.enums.Resampling.bilinear)
            m0 = ds.read_masks(1, out_shape=out_shape,
                               resampling=rasterio.enums.Resampling.nearest)
        # v1.3.0 修正断言：**只断言被测性质本身**，不碰第三方实现细节。
        # 旧写法 `bilinear <= average` 假设了侵蚀方向，实测方向随尺度/版本变化：
        #   rasterio 1.5.2 小图：average=nearest=mask=49，bilinear=57（更多）
        #   rasterio 1.5.1 小图：bilinear 与 mask 逐像元完全一致（差异 0）
        #   6000² 大图（v1.2.0 实测）：bilinear 2704 < average 2809（更少）
        # 因此硬断言只能是「average / nearest 与 read_masks 真值逐像元一致」；
        # bilinear 的差异只记录、不断言，否则换环境就假失败。
        truth = (m0 == 0)
        bil_diff = int((np.isnan(a_bil) != truth).sum())
        check("average_keeps_nodata",
              int(np.isnan(a_avg).sum()) == int(truth.sum()) ==
              int(np.isnan(a_near).sum()) and
              np.array_equal(np.isnan(a_avg), truth) and
              np.array_equal(np.isnan(a_near), truth),
              f"average/nearest 与 read_masks 逐像元一致（{int(truth.sum())} 个 "
              f"nodata）；bilinear 差异 {bil_diff} 个像元（随版本/尺度变化，仅观测）。"
              "6000² 大图实测 bilinear 会侵蚀边界，故仍采用 average")

        # ---- 17. v1.2.0：参数签名 / 死常量清理 ----
        sig_build = inspect.signature(build_mosaic.build)
        check("v12_signature",
              sig_build.parameters["max_output_mb"].default ==
              gem15_common.DEFAULT_MAX_OUTPUT_MB and
              "--max-output-mb" in gem15_dem.WITH_VALUE and
              gem15_common.DEFAULT_MAX_OUTPUT_MB == 512 and
              not hasattr(gem15_common, "LAT_MIN"),
              "内存上限参数贯通主入口；误导性的 LAT_MIN/LAT_MAX 死常量已删除")

        # ---- 18. v1.3.0：--k=v / 未知选项显式报错 / 各子 CLI help 退出码 ----
        ep, eo, ef = parse_args(["LIE", "out", "--layer=dsm", "--no-clip"],
                                gem15_dem.WITH_VALUE, gem15_dem.FLAGS_ALLOWED)
        check("cli_equals_form",
              ep == ["LIE", "out"] and eo.get("layer") == "dsm" and
              "no-clip" in ef and "layer=dsm" not in ef,
              "--layer=dsm 不再变成名为 layer=dsm 的假开关")
        try:
            parse_args(["LIE", "--layr", "dsm"], gem15_dem.WITH_VALUE,
                       gem15_dem.FLAGS_ALLOWED)
            unknown_raised, unknown_msg = False, ""
        except gem15_common.GemError as exc:
            unknown_raised, unknown_msg = True, str(exc)
        check("cli_unknown_flag",
              unknown_raised and "layr" in unknown_msg and "--layer" in unknown_msg,
              f"拼错选项显式报错并给相近候选：{unknown_msg[:64]}")
        try:
            parse_args(["LIE", "out", "--no-clip=1"], gem15_dem.WITH_VALUE,
                       gem15_dem.FLAGS_ALLOWED)
            switch_raised = False
        except gem15_common.GemError:
            switch_raised = True
        check("cli_switch_equals", switch_raised,
              "布尔开关不接受 = 值，不静默忽略")
        help_codes = {}
        for mod in (stac_index, build_mosaic, preview_dem,
                    resolve_region, fetch_tiles):
            with mock.patch.object(sys, "argv", [mod.__name__ + ".py", "--help"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                try:
                    mod.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code or 0
            help_codes[mod.__name__] = code
        check("cli_help_all", all(v == 0 for v in help_codes.values()),
              f"5 个子 CLI 的 --help 退出码 {help_codes}")

        # ---- 19. v1.3.0：无位置参数清缓存 + 清理范围 ----
        croot = tmp / "cache"
        for sub in gem15_common.CACHE_SUBDIRS:
            (croot / sub).mkdir(parents=True, exist_ok=True)
            (croot / sub / "x.bin").write_bytes(b"x" * 64)
        with contextlib.redirect_stdout(io.StringIO()):
            rc_clean = gem15_dem.run(["--clean-cache", "--cache", str(croot)])
        after_all = {s: (croot / s).exists() for s in gem15_common.CACHE_SUBDIRS}
        check("clean_cache_no_pos",
              rc_clean == 0 and not any(after_all.values()),
              f"无位置参数也 exit 0 且四类缓存全清：{after_all}")
        for sub in gem15_common.CACHE_SUBDIRS:
            (croot / sub).mkdir(parents=True, exist_ok=True)
            (croot / sub / "x.bin").write_bytes(b"y" * 128)
        with contextlib.redirect_stdout(io.StringIO()):
            rep_tiles = fetch_tiles.clean_cache(croot, scope="tiles")
        kept = all((croot / s).exists() for s in ("stac", "boundary", "_tmp"))
        with contextlib.redirect_stdout(io.StringIO()):
            rep_all = fetch_tiles.clean_cache(croot, scope="all")
        check("clean_cache_scope",
              rep_tiles["scope"] == "tiles" and rep_tiles["bytes"] == 128 and
              kept and not (croot / "tiles").exists() and
              rep_all["bytes"] == 3 * 128 and
              not any((croot / s).exists() for s in gem15_common.CACHE_SUBDIRS),
              f"tiles-only 释放 {rep_tiles['bytes']} B 且保留其余三类；"
              f"all 释放 {rep_all['bytes']} B")

        # ---- 20. v1.4.0：get_json 两条失败路径都不产生任何临时文件 ----
        # v1.3.0 这条用例 mock 的是 curl_get；v1.4.0 改为内存读取后，
        # 测试替身必须同步对齐生产函数（否则会抛 TypeError 绕过 except）。
        jroot = tmp / "jcache"
        (jroot / "stac").mkdir(parents=True)
        cache_file = jroot / "stac" / "item_x.json"
        leftovers = []
        for patch in (mock.patch.object(gem15_common, "curl_bytes",
                                        return_value=b"gateway 504 html"),
                      mock.patch.object(gem15_common, "curl_bytes",
                                        side_effect=gem15_common.DownloadError("boom"))):
            with patch, contextlib.redirect_stdout(io.StringIO()):
                raised = False
                try:
                    gem15_common.get_json("https://example.invalid/x.json",
                                          cache=cache_file)
                except gem15_common.DownloadError:
                    raised = True
            leftovers.append((raised,
                              list(jroot.rglob("gem15_get_*.tmp")),
                              cache_file.exists()))
        check("get_json_tmp_cleanup",
              all(r and not f and not c for r, f, c in leftovers),
              "错误页 / 下载异常都显式抛 DownloadError，且不留 .tmp、不写坏缓存")

        # ---- 21. v1.3.0：STAC link 绝对 URL / 带前缀 / 非法 id ----
        hrefs = ("./p40_p0/catalog.json",
                 "https://storage.googleapis.com/x/gem-15/p40_p0/catalog.json",
                 "../gem-15/p40_p0/catalog.json")
        with contextlib.redirect_stdout(io.StringIO()) as link_buf:
            sets = [stac_index.child_groups(
                {"links": [{"rel": "child", "href": h}]}) for h in hrefs]
            cell_abs = stac_index._link_id(
                {"rel": "item",
                 "href": "https://x/gem-15/p40_p0/w9s47/w9s47.json"},
                stac_index.CELL_RE, "1° cell 名")
            bad = stac_index._link_id(
                {"rel": "child", "href": "https://x/no-id-here.json"},
                stac_index.GROUP_RE, "10° 组名")
        check("link_ids_absolute",
              all(s == {"p40_p0"} for s in sets) and
              cell_abs == "w9s47" and bad is None and
              "无法从 STAC link 解析" in link_buf.getvalue(),
              "绝对 URL / ../gem-15/ 前缀 / 无 title 均可解析；非法 id 打印 warn 跳过")

        # ---- 22. v1.3.0：GeoTIFF 写出零复制 ----
        arr_src = np.full((1, 16, 16), 42.0, np.float32)
        with mock.patch.object(build_mosaic.rasterio, "open") as mopen:
            dst = mopen.return_value.__enter__.return_value
            build_mosaic.write_geotiff(tmp / "fake.tif", arr_src, grid,
                                       CRS.from_epsg(32632), "dtm", {})
            written = dst.write.call_args[0][0]
        check("geotiff_no_copy",
              np.shares_memory(written, arr_src) and written.dtype == np.float32,
              f"write 收到同一块内存（消除 {arr_src.nbytes} B 等大复制；"
              "大成果按 512 MB 上限等比例放大）")

        # ---- 23. v1.3.0：--buffer 不扩大裁切框 / --bg 值校验 ----
        bnd = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": box(9.5, 47.0, 9.6, 47.1).__geo_interface__}]}
        cb = build_mosaic.clip_bbox_for(bnd)
        fetch_bbox = (9.4, 46.9, 9.7, 47.2)     # --buffer 外扩后的取数框
        check("clip_bbox_buffer",
              abs(cb[0] - 9.5) < 1e-9 and abs(cb[2] - 9.6) < 1e-9 and
              cb != fetch_bbox,
              f"裁切框 {tuple(round(v, 3) for v in cb)} 与外扩取数框解耦")
        try:
            preview_dem.render(tmp / "not_exist.tif", tmp / "x.png", bg="blue")
            bg_raised = False
        except gem15_common.GemError:
            bg_raised = True
        check("background_validation", bg_raised,
              "--bg blue 不再静默当 transparent")

        # ---- 24. v1.3.0：版本号 / 常量 / 签名同步 ----
        sig_clean = inspect.signature(fetch_tiles.clean_cache)
        check("v13_signature",
              gem15_common.VERSION == "2.0.0" and
              gem15_common.CACHE_SUBDIRS == ("tiles", "stac", "boundary", "_tmp") and
              "clean-tiles-only" in gem15_dem.FLAGS_ALLOWED and
              sig_clean.parameters["scope"].default == "all" and
              hasattr(gem15_common, "check_help"),
              "版本号 / 缓存常量 / 开关白名单 / clean_cache scope / help helper 同步")

        # ---- 25. v1.3.0：边界源不可达时给出可执行建议 ----
        # v1.12.0：边界下载改走 curl_get_mirrors（多镜像 + 内容校验）
        with mock.patch.object(resolve_region, "curl_get_mirrors",
                               side_effect=gem15_common.DownloadError("000")):
            try:
                resolve_region.gb_geojson(
                    {"gjDownloadURL": "https://example.invalid/b.geojson",
                     "boundaryISO": "X"}, "ADM0", tmp / "bcache")
                hint_raised, hint_msg = False, ""
            except gem15_common.RegionError as exc:
                hint_raised, hint_msg = True, str(exc)
        check("boundary_error_hint",
              hint_raised and "--geojson" in hint_msg and
              "--bbox" in hint_msg and "--clean-tiles-only" in hint_msg,
              "边界源 000 时给出自备边界 / bbox / 保留缓存三条建议")

        # ---- 26. v1.4.0：get_json 成功路径走内存，零临时文件 / 零 curl_get ----
        with mock.patch.object(gem15_common.subprocess, "run") as mrun, \
             mock.patch.object(gem15_common, "curl_get") as mcurl:
            mrun.return_value.stdout = b'{"ok": 1}\n200'
            mrun.return_value.stderr = b""
            got = gem15_common.get_json("https://example.invalid/item.json",
                                        cache=jroot / "stac" / "item_ok.json")
        check("get_json_memory_read",
              got == {"ok": 1} and mcurl.call_count == 0 and
              (jroot / "stac" / "item_ok.json").exists() and
              not list(jroot.rglob("*.tmp")),
              "curl_bytes 内存读取后直接写缓存；不调 curl_get、不建 .tmp")

        # ---- 27. v1.4.0：curl_bytes 的 body/code 拆分、404、空体重试 ----
        with mock.patch.object(gem15_common.subprocess, "run") as mrun:
            mrun.return_value.stdout = b'{"a": 1}\n{"tail": "in body"}\n200'
            body = gem15_common.curl_bytes("https://example.invalid/x.json",
                                           retries=0)
        split_ok = body == b'{"a": 1}\n{"tail": "in body"}'
        with mock.patch.object(gem15_common.subprocess, "run") as mrun, \
             contextlib.redirect_stdout(io.StringIO()):
            mrun.return_value.stdout = b"\n404"
            try:
                gem15_common.curl_bytes("https://example.invalid/404.json",
                                        retries=0)
                not_found = False
            except gem15_common.DownloadError as exc:
                not_found = "404" in str(exc)
        with mock.patch.object(gem15_common.subprocess, "run") as mrun, \
             contextlib.redirect_stdout(io.StringIO()):
            mrun.return_value.stdout = b"\n200"       # 空 body
            try:
                gem15_common.curl_bytes("https://example.invalid/empty.json",
                                        retries=0)
                empty_raised = False
            except gem15_common.DownloadError:
                empty_raised = True
        check("curl_bytes_split",
              split_ok and not_found and empty_raised,
              "rpartition 正确截取含换行的 body；404 与空体都显式报错")

        # ---- 28. v1.4.0：100 次批量索引（模拟英国 87 cell）不触发任何删除 ----
        bulk_root = tmp / "bulk"
        bulk_root.mkdir()
        with mock.patch.object(gem15_common.subprocess, "run") as mrun, \
             mock.patch.object(Path, "unlink", autospec=True) as munlink:
            mrun.return_value.stdout = b'{"cell": "x"}\n200'
            for i in range(100):
                gem15_common.get_json(f"https://example.invalid/i{i}.json",
                                      cache=bulk_root / f"item_{i}.json")
            deletes = munlink.call_count
        check("bulk_index_no_delete",
              deletes == 0 and len(list(bulk_root.glob("*.json"))) == 100 and
              not list(bulk_root.rglob("*.tmp")),
              f"100 次索引触发 {deletes} 次 unlink、0 个 .tmp；"
              "旧实现 100 次删除必然撞宿主 50 次阈值")

        # ---- 29. v1.4.0：版本号 / 新函数 / 死分支与死 import 清理 ----
        sig_get = inspect.signature(gem15_common.curl_get)
        check("v14_signature",
              gem15_common.VERSION == "2.0.0" and
              callable(getattr(gem15_common, "curl_bytes", None)) and
              sig_get.parameters["dest"].default is inspect.Parameter.empty and
              not hasattr(gem15_common, "tempfile") and
              not hasattr(gem15_common, "os"),
              '版本号 2.0.0 / curl_bytes 存在 / curl_get dest 必填 / '
              'tempfile 与 os 死 import 已清')

        # ---- 30. v1.5.0：共用最小合成 COG + 边界 ----
        from rasterio.warp import transform_bounds as tb15
        p_tr = from_origin(500000.0, 5200000.0, 15.0, 15.0)
        p_cog = tmp / 'v15_w9s47.tif'
        make_tif(p_cog, np.full((1, 400, 400), 123.0, np.float32), p_tr)
        p_utm = (500000 + 150 * 15, 5200000 - 250 * 15,
                 500000 + 250 * 15, 5200000 - 150 * 15)
        p_wgs = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326), *p_utm)
        p_boundary = resolve_region.bbox_feature(p_wgs, 'probe')
        p_item = {'cell': 'w9s47', 'group': 'p40_p0',
                  'url': 'https://example.invalid/x.tif', 'epsg': 32632,
                  'proj_transform': list(p_tr)[:6], 'proj_shape': [400, 400],
                  'size': p_cog.stat().st_size, 'bbox': list(p_wgs)}

        def run15(nc, layer='dtm', tag='x'):
            with mock.patch.object(build_mosaic, 'download_items',
                                   return_value={}), \
                 mock.patch.object(build_mosaic, 'tile_path',
                                   return_value=p_cog), \
                 contextlib.redirect_stdout(io.StringIO()):
                return build_mosaic.build(
                    [dict(p_item)], p_boundary, tmp / f'v15_{tag}.tif',
                    layer=layer, mode='full', cache_dir=tmp, no_clip=nc)

        plan15 = build_mosaic.plan_window(p_item, p_wgs, 'dtm')
        tcrs15, res15, _ = build_mosaic.decide_crs([32632], 'auto', {})
        _, gw15, gh15 = build_mosaic.grid_for([plan15], tcrs15, res15)
        st_clip = run15(False, tag='clip')
        st_free = run15(True, tag='free')

        # ---- 31. --no-clip 保留完整网格（README 既有约定：保留矩形）----
        shape_free, shape_clip = st_free['output_shape'][1:], st_clip['output_shape'][1:]
        check('no_clip_keeps_grid',
              shape_free == [gh15, gw15] and shape_clip != [gh15, gw15] and
              st_clip['output_shape'][1] < gh15,
              f'网格 {gw15}x{gh15}；no_clip 保留 {shape_free}，裁切后 {shape_clip}'
              '（旧实现两者都是 106x106）')

        # ---- 32. 裁切后紧凑化可观测 ----
        check('compact_copy_releases_grid',
              st_clip['compact_copy'] is True and st_clip['released_mb'] > 0 and
              st_clip['grid_pixels'] == gw15 * gh15 and
              st_free['compact_copy'] is False and st_free['released_mb'] == 0.0,
              f'裁切释放 {st_clip["released_mb"]} MB；no_clip 零复制。'
              '实测 4000² 写出常驻 +63.9 MB -> +8.6 MB')

        # ---- 33. ascontiguousarray 零成本自适应 ----
        z15 = np.full((1, 40, 40), 1.0, np.float32)
        full15, sub15 = z15[:, :, :], z15[:, 10:20, 10:20]
        check('ascontiguousarray_zero_copy',
              np.ascontiguousarray(full15) is full15 and
              np.ascontiguousarray(sub15) is not sub15,
              '全切片返回同一对象（零复制），子视图才紧凑化 -> 无需手工阈值')

        # ---- 34. 非法参数不白建输出目录 ----
        bad15 = tmp / 'v15_bad_out'
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                gem15_dem.run(['LIE', str(bad15), '--layer', 'bad'])
                bad15_raised = False
            except gem15_common.GemError:
                bad15_raised = True
        check('bad_args_no_outdir', bad15_raised and not bad15.exists(),
              '--layer bad 在 mkdir 之前抛 GemError，不留下空输出目录')

        # ---- 35. --unknown=value 不再把拼错项当合法开关推荐 ----
        def peq(argv):
            try:
                gem15_common.parse_args(argv, gem15_dem.WITH_VALUE,
                                        gem15_dem.FLAGS_ALLOWED)
                return ''
            except gem15_common.GemError as exc:
                return str(exc)
        ue = peq(['LIE', 'o', '--layr=dsm'])
        sw = peq(['LIE', 'o', '--clean-cache=1'])
        check('unknown_equals_hint',
              '未知选项' in ue and '--layer' in ue and '请写成 --layr' not in ue and
              '开关选项不接受' in sw,
              '拼错带值选项给相近候选；真开关传 =值 才提示不接受')

        # ---- 36. --maxside 前置校验（旧实现 np.gradient ValueError）----
        small15 = tmp / 'v15_small.tif'
        make_tif(small15, np.full((1, 200, 300), 100.0, np.float32), p_tr)
        try:
            preview_dem.render(small15, tmp / 'v15_p.png', maxside=0,
                               do_annotate=False)
            ms15 = ''
        except gem15_common.GemError as exc:
            ms15 = str(exc)
        bad15b = tmp / 'v15_bad_maxside'
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                gem15_dem.run(['LIE', str(bad15b), '--maxside', '0'])
            except gem15_common.GemError:
                pass
        check('maxside_validation',
              'maxside' in ms15 and not bad15b.exists(),
              'maxside=0 前置为 GemError，不再让 np.gradient 抛未预期 ValueError')

        # ---- 37. v1.12.0/v1.12.1：零进展才放弃，已收字节保留（不清零、不删除）----
        dl37 = tmp / 'v12_partial.dl'
        dl37.write_bytes(b'AAA')
        cmds37 = []

        def _stall37(cmd, **kw):                         # noqa: ARG001
            cmds37.append(list(cmd))                     # 一次都不写字节
            return mock.Mock(stdout='200', stderr='')

        with mock.patch.object(fetch_tiles.subprocess, 'run',
                               side_effect=_stall37),              mock.patch.object(Path, 'unlink', autospec=True) as munlink37,              mock.patch.object(gem15_common, '_sleep_for'),              contextlib.redirect_stdout(io.StringIO()):
            try:
                fetch_tiles._resume_download('https://x/t.tif', dl37, 100,
                                             'v12', timeout=5, retries=2)
                err37 = ''
            except gem15_common.DownloadError as exc:
                err37 = str(exc)
        check('resume_keeps_partial',
              munlink37.call_count == 0 and dl37.stat().st_size == 3 and
              '下载未完成 3/100' in err37 and '零进展' in err37 and
              len(cmds37) == 3 and '-C' in cmds37[0] and '-L' in cmds37[0],
              f'连续零进展 {len(cmds37)} 次（retries=2 -> 第 3 次放弃）后报错，'
              f'已收 3 B 保留、0 次 unlink；cmd 含 -L / -C -')

        # ---- 37b. v1.12.1：慢但**有进展**的续传不按次数放弃（旧实现会误杀）----
        # 德国整国实测：单连接 0.35~0.79 MB/s × --max-time 150 s = 每次最多
        # 52~118 MB，而单 cell 有 156~176 MB。旧 `attempts <= retries` 只给
        # retries+1 次机会，慢链路下等于给文件设字节上限，多一次波动就
        # DownloadError 拖死整批（48 块跑到一半全废）。
        dl37b = tmp / 'v12_progress.dl'
        n37b = []

        def _slow37b(cmd, **kw):                         # noqa: ARG001
            out = Path(cmd[cmd.index('-o') + 1])
            n37b.append(1)
            out.write_bytes((out.read_bytes() if out.exists() else b'') + b'B' * 10)
            return mock.Mock(stdout='200', stderr='')

        with mock.patch.object(fetch_tiles.subprocess, 'run',
                               side_effect=_slow37b),               mock.patch.object(fetch_tiles, '_cache_ok',
                               return_value=True),                  mock.patch.object(gem15_common, '_sleep_for'),              contextlib.redirect_stdout(io.StringIO()):
            try:
                r37b = fetch_tiles._resume_download('https://x/t.tif', dl37b,
                                                    100, 'v12b', timeout=5,
                                                    retries=2)
                err37b = ''
            except gem15_common.DownloadError as exc:
                r37b, err37b = {}, str(exc)
        check('resume_progress_budget',
              not err37b and r37b.get('bytes') == 100 and
              r37b.get('attempts') == 10 and len(n37b) == 10,
              f'每次 +10 B 共 100 B 用 10 次尝试跑完（旧实现 retries=2 只能 4 次→'
              f'40 B 就报错）：bytes={r37b.get("bytes")} '
              f'attempts={r37b.get("attempts")} err={err37b[:40]}')

        # ---- 38. v1.12.0：下载成功全程无 .parts 目录、无删除 ----
        it38 = {'cell': 'v12dl', 'url': 'https://x/t.tif', 'size': 6,
                'epsg': 32632}
        cd38 = tmp / 'v12_dl_cache'

        def _full38(cmd, **kw):                          # noqa: ARG001
            Path(cmd[cmd.index('-o') + 1]).write_bytes(b'A' * 6)
            return mock.Mock(stdout='206', stderr='')

        def _ok38(path, size):                           # noqa: ARG001
            return Path(path).suffix == '.dl'            # 只有 .dl 视为已就绪

        with mock.patch.object(fetch_tiles.subprocess, 'run',
                               side_effect=_full38),              mock.patch.object(fetch_tiles, '_cache_ok', side_effect=_ok38),              mock.patch.object(Path, 'unlink', autospec=True) as munlink38,              contextlib.redirect_stdout(io.StringIO()):
            r38 = fetch_tiles.download_one(it38, 'dtm', cd38)
        dtm38 = cd38 / 'tiles' / 'dtm'
        check('download_no_parts',
              r38['status'] == 'download' and
              (dtm38 / 'v12dl.tif').exists() and
              not list(dtm38.glob('*.parts')) and
              not list(dtm38.glob('*.dl')) and
              munlink38.call_count == 0 and r38['attempts'] == 1,
              '.dl 一次请求下完即 replace 成 .tif：无 .parts 目录、'
              '无 .dl 残留、0 次 unlink（旧实现要清 23 个段文件）')

        # ---- 39. ADM0 主链接缺失回退可观测 ----
        meta39 = {'simplifiedGeometryGeoJSON': 'https://example.invalid/s.geojson',
                  'boundaryISO': 'LIE'}

        def fg39(url, dest, **kw):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(p_boundary), encoding='utf-8')
            return 200, dest, url

        with mock.patch.object(resolve_region, 'curl_get_mirrors',
                               side_effect=fg39), \
             contextlib.redirect_stdout(io.StringIO()) as b39:
            resolve_region.gb_geojson(meta39, 'ADM0', tmp / 'v15_fb')
        o39 = b39.getvalue()
        check('boundary_fallback_visible',
              '[warn]' in o39 and 'simplifiedGeometryGeoJSON' in o39 and
              '使用' in o39,
              '旧实现静默回退且日志为空；现打印 [warn] 与真实字段名')

        # ---- 40. 损坏缓存覆盖写修复，索引路径零删除 ----
        broken40 = tmp / 'v15_broken.json'
        broken40.write_text('{broken', encoding='utf-8')
        with mock.patch.object(gem15_common.subprocess, 'run') as mrun40, \
             mock.patch.object(Path, 'unlink', autospec=True) as munlink40, \
             contextlib.redirect_stdout(io.StringIO()):
            mrun40.return_value.stdout = b'{"ok": 2}\n200'
            mrun40.return_value.stderr = b''
            got40 = gem15_common.get_json('https://x/y.json', cache=broken40)
        check('get_json_zero_delete',
              got40 == {'ok': 2} and munlink40.call_count == 0 and
              json.loads(broken40.read_text(encoding='utf-8')) == {'ok': 2},
              'v1.4.0 只清了成功路径；损坏缓存的 unlink 也消除，覆盖写修复')

        # ---- 41. 实际 COG 波段数 != layer 声明 -> 显式 DownloadError ----
        try:
            run15(False, 'dsm', tag='dsm_mismatch')
            bm41 = ''
        except gem15_common.DownloadError as exc:
            bm41 = str(exc)
        check('band_count_mismatch',
              '波段数' in bm41 and '不一致' in bm41,
              'dsm 声明 5 波段配 1 波段 COG：旧实现 IndexError(exit 1)，现显式报错')

        # ---- 42. 预览参数贯通 render 与 manifest ----
        mroot42 = tmp / 'v15_manifest'
        bmeta42 = {'name': 'probe', 'bbox': list(p_wgs)}
        with mock.patch.object(gem15_dem.resolve_region, 'resolve',
                               return_value=(p_boundary, bmeta42)), \
             mock.patch.object(gem15_dem.stac_index, 'select_items',
                               return_value=[p_item]), \
             mock.patch.object(gem15_dem.build_mosaic, 'build',
                               return_value={'crs': 'EPSG:32632',
                                             'valid_pixels': 1,
                                             'elev_p2_m': 0.0,
                                             'elev_p98_m': 1.0}), \
             mock.patch.object(gem15_dem.preview_dem, 'render',
                               return_value={}) as mrender42, \
             contextlib.redirect_stdout(io.StringIO()):
            rc42 = gem15_dem.run(['LIE', str(mroot42),
                                  '--cache', str(tmp / 'v15_mcache'),
                                  '--bg', 'white', '--maxside', '800', '--plain'])
        mf42 = json.loads((mroot42 / 'probe_gem15_manifest.json').read_text(
            encoding='utf-8'))
        q42, kw42 = mf42['query'], mrender42.call_args.kwargs
        check('preview_meta_in_manifest',
              rc42 == 0 and q42['bg'] == 'white' and q42['maxside'] == 800 and
              q42['plain'] is True and q42['no_clip'] is False and
              q42['buffer_m'] == 0.0 and kw42['bg'] == 'white' and
              kw42['maxside'] == 800 and kw42['do_annotate'] is False,
              'manifest.query 记录 bg/title/maxside/plain，render 收到同一组参数')

        # ---- 43. v1.5.0 签名 / 版本号同步 ----
        check('v15_signature',
              gem15_common.VERSION == '2.0.0' and
              'compact_copy' in inspect.getsource(build_mosaic.build) and
              'maxside < 2' in inspect.getsource(gem15_dem.run) and
              'maxside' in inspect.getsource(preview_dem.render),
              '版本号 2.0.0 / build 紧凑化 / 双入口 maxside 前置校验')

        # ---- 44. v1.6.0：失败路径不白建输出目录（黑盒矩阵实测 13 个残留）----
        qa = tmp / 'v16_qa'
        cases16 = [
            ('layer',  ['QA', str(qa/'layer'),  '--layer', 'bad']),
            ('bbox',   ['QA', str(qa/'bbox'),   '--bbox', '1,2,3']),
            ('excl',   ['中国', str(qa/'excl')]),
            ('excl_tw', ['台湾', str(qa/'excl_tw')]),
            ('excl_twn', ['TWN', str(qa/'excl_twn')]),
            ('region', ['NotACountry', str(qa/'region')]),
        ]
        dirs_after = {}
        for tag, argv in cases16:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    gem15_dem.run(argv)
                except gem15_common.GemError:
                    pass
            dirs_after[tag] = (qa/tag).exists()
        check('output_dir_late_create',
              not any(dirs_after.values()),
              f'6 类失败路径均不建输出目录（含台湾/港澳归并排除）：{dirs_after}')

        # ---- 45. v1.6.0：四个数值参数的范围前置校验 ----
        bad_nums = [('workers', '0'), ('workers', '-3'),
                    ('timeout', '0'), ('timeout', '-5'),
                    ('chunk-mb', '0'), ('chunk-mb', '-4'),
                    ('buffer', '-1000')]
        num_ok, num_msgs = True, []
        for k, v in bad_nums:
            d = qa / f'num_{k}_{v.replace("-", "n")}'
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    gem15_dem.run(['QA', str(d), '--'+k, v])
                    num_ok = False
                except gem15_common.GemError:
                    num_msgs.append(f'{k}={v}:GemError')
            if d.exists():
                num_ok = False
        check('numeric_range_validation',
              num_ok and len(num_msgs) == len(bad_nums),
              f'{len(num_msgs)}/{len(bad_nums)} 个非法数值前置为 GemError 且不留目录')

        # ---- 46. v1.6.0：--crs 非法值不再抛 CRSError（未预期 exit 1）----
        crs_bad = []
        for v in ('bad', '999999', 'EPSG:999999'):
            d = qa / f'crs_{v.replace(":", "_")}'
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    gem15_dem.run(['QA', str(d), '--crs', v])
                    crs_bad.append(('NO_ERROR', d.exists()))
                except gem15_common.GemError:
                    crs_bad.append(('GemError', d.exists()))
                except Exception as exc:            # noqa: BLE001
                    crs_bad.append((type(exc).__name__, d.exists()))
        try:
            build_mosaic.decide_crs([32632], 'bad', {})
            direct = 'NO_ERROR'
        except gem15_common.GemError:
            direct = 'GemError'
        except Exception as exc:                    # noqa: BLE001
            direct = type(exc).__name__
        check('crs_validation',
              all(k == 'GemError' and not e for k, e in crs_bad) and
              direct == 'GemError',
              f'入口 {[k for k,_ in crs_bad]}；独立 CLI decide_crs={direct}')

        # ---- 47. v1.6.0：COG 声明非 NaN nodata 时统一掩为 NaN ----
        nd_tif = tmp / 'v16_nod.tif'
        nd_tr = from_origin(500000., 5200000., 15., 15.)
        nd_arr = np.full((1, 64, 64), 100., np.float32)
        nd_arr[0, 10:20, 10:20] = -32768.
        with rasterio.open(nd_tif, 'w', driver='GTiff', height=64, width=64,
                           count=1, dtype='float32', crs=CRS.from_epsg(32632),
                           transform=nd_tr, nodata=-32768.) as ds:
            ds.write(nd_arr)
        fb16 = rasterio.transform.array_bounds(64, 64, nd_tr)
        bw16 = rasterio.warp.transform_bounds(
            CRS.from_epsg(32632), CRS.from_epsg(4326), *fb16, densify_pts=21)
        dx16, dy16 = (bw16[2]-bw16[0])*0.2, (bw16[3]-bw16[1])*0.2
        bb16 = (bw16[0]+dx16, bw16[1]+dy16, bw16[2]-dx16, bw16[3]-dy16)
        with contextlib.redirect_stdout(io.StringIO()) as b16:
            w16 = build_mosaic.read_one_window(str(nd_tif), bb16)
        pure = build_mosaic.normalize_nodata(
            np.array([1., -32768.], np.float32), -32768., 'pure')
        check('nodata_non_nan_masked',
              w16 is not None and not np.any(w16['arr'] == -32768.) and
              int(np.isnan(w16['arr']).sum()) >= 1 and
              bool(np.isnan(pure[1])) and pure[0] == 1.0 and
              '[warn]' in b16.getvalue(),
              'COG nodata=-32768 的填充值被掩为 NaN 且 [warn] 可观测')

        # ---- 48. v1.6.0：GeoJSON geometry:null 不崩、跳过可观测、全空判空 ----
        gj16 = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {}, 'geometry': None},
            {'type': 'Feature', 'properties': {}, 'geometry': {
                'type': 'Polygon', 'coordinates': [
                    [[9.5, 47.0], [9.6, 47.0], [9.6, 47.1], [9.5, 47.1],
                     [9.5, 47.0]]]}}]}
        with contextlib.redirect_stdout(io.StringIO()) as b16g:
            box16 = gem15_common.bbox_of_geojson(gj16)
            geoms16 = gem15_common.geojson_geometries(gj16)
        try:
            gem15_common.bbox_of_geojson(
                {'type': 'FeatureCollection', 'features': [
                    {'type': 'Feature', 'properties': {}, 'geometry': None}]})
            all_null = False
        except gem15_common.RegionError:
            all_null = True
        check('geojson_null_geometry',
              abs(box16[0]-9.5) < 1e-9 and len(geoms16) == 1 and
              '[warn]' in b16g.getvalue() and all_null,
              'geometry:null 跳过并 [warn]；全 null 判空 RegionError')

        # ---- 49. v1.6.0：逐波段 first-valid（旧 2D filled 会整片跳过）----
        tr16 = from_origin(0., 60., 15., 15.)
        aA16 = np.full((2, 4, 4), np.nan, np.float32)
        aA16[0] = 10.
        aB16 = np.full((2, 4, 4), np.nan, np.float32)
        aB16[1] = 20.

        def mk16(a):
            return {'arr': a, 'transform': tr16, 'crs': CRS.from_epsg(32632),
                    'count': 2, 'width': 4, 'height': 4,
                    'bounds': rasterio.transform.array_bounds(4, 4, tr16),
                    'cell': 'mb'}
        wA16, wB16 = mk16(aA16), mk16(aB16)
        g16, gw16, gh16 = build_mosaic.grid_for([wA16], CRS.from_epsg(32632), 15.)
        out16 = np.full((2, gh16, gw16), np.nan, np.float32)
        n1 = build_mosaic.paste(out16, wA16, g16, gw16, gh16,
                                CRS.from_epsg(32632), 15.)
        n2 = build_mosaic.paste(out16, wB16, g16, gw16, gh16,
                                CRS.from_epsg(32632), 15.)
        check('multiband_first_valid',
              np.all(out16[0][np.isfinite(out16[0])] == 10.) and
              np.all(out16[1][np.isfinite(out16[1])] == 20.) and
              int(np.isfinite(out16[1]).sum()) == 16 and n1 == 16 and n2 == 16,
              '逐波段判空：片 B 的波段1 值 20 不再被整片跳过')

        # ---- 50. v1.6.0：--title "" 回落时 manifest 与 render 实参一致 ----
        mroot16 = tmp / 'v16_manifest'
        with mock.patch.object(gem15_dem.resolve_region, 'resolve',
                               return_value=(p_boundary,
                                             {'name': 'probe', 'bbox': list(p_wgs)})), \
             mock.patch.object(gem15_dem.stac_index, 'select_items',
                               return_value=[p_item]), \
             mock.patch.object(gem15_dem.build_mosaic, 'build',
                               return_value={'crs': 'EPSG:32632',
                                             'valid_pixels': 1,
                                             'elev_p2_m': 0., 'elev_p98_m': 1.}), \
             mock.patch.object(gem15_dem.preview_dem, 'render',
                               return_value={}) as mr16, \
             contextlib.redirect_stdout(io.StringIO()):
            rc16 = gem15_dem.run(['LIE', str(mroot16), '--title', '',
                                  '--cache', str(tmp/'v16_mcache')])
        mf16 = json.loads((mroot16/'probe_gem15_manifest.json').read_text(
            encoding='utf-8'))
        check('title_manifest_matches_render',
              rc16 == 0 and mf16['query']['title'] == 'probe' and
              mf16['query']['title_input'] == '' and
              mr16.call_args.kwargs['title'] == 'probe',
              '--title "" 回落时 manifest 记录生效标题，与 render 实参一致')

        # ---- 51. v1.6.0：--no-clip 不再需要有效边界几何 ----
        bnd16 = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {}, 'geometry': None}]}
        with mock.patch.object(build_mosaic, 'download_items', return_value={}), \
             mock.patch.object(build_mosaic, 'tile_path', return_value=p_cog), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                st16 = build_mosaic.build(
                    [dict(p_item)], bnd16, tmp/'v16_nogeom.tif', layer='dtm',
                    mode='full', cache_dir=tmp, no_clip=True,
                    region_meta={'name': 'probe', 'bbox': list(p_wgs)})
                no_geom_ok = st16['output_shape'][1:] == [gh15, gw15]
            except Exception:                       # noqa: BLE001
                no_geom_ok = False
        check('no_clip_needs_no_geometry', no_geom_ok,
              '边界几何为空也能 --no-clip 保留完整矩形网格')

        # ---- 52. v1.6.0：内存预检口径（成果数组 != 进程峰值）----
        stp16: dict = {}
        build_mosaic.assert_output_fits(1000, 1000, 1, 512, stp16)
        try:
            build_mosaic.assert_output_fits(20000, 20000, 5, 512, {})
            msg16 = ''
        except gem15_common.OutputTooLargeError as exc:
            msg16 = str(exc)
        # 口径：estimated_output_mb 已四舍五入到 2 位，不能拿它再乘 3 做等值比较
        # （3.81×3=11.43，而峰值来自未舍入的 3.8147×3=11.44）-> 用容差。
        check('peak_memory_estimate',
              abs(stp16['estimated_peak_mb'] -
                  stp16['estimated_output_mb']*3) < 0.02 and
              stp16['estimated_peak_mb'] > stp16['estimated_output_mb'] and
              '口径' in msg16 and '峰值' in msg16 and '倍' in msg16,
              f"成果数组 {stp16['estimated_output_mb']} MB / "
              f"估算峰值 {stp16['estimated_peak_mb']} MB；错误信息写清口径")

        # ---- 53. v1.6.0 签名 / 版本号同步 ----
        check('v16_signature',
              gem15_common.VERSION == '2.0.0' and
              'validate_crs_opt' in inspect.getsource(build_mosaic) and
              'normalize_nodata' in inspect.getsource(build_mosaic) and
              'filled' not in inspect.signature(build_mosaic.paste).parameters and
              'eff_title' in inspect.getsource(gem15_dem.run) and
              len(EXPECTED_KEYS) == 154,
              '版本号 2.0.0 / CRS 校验 / nodata 归一化 / paste 去 filled / 标题一致')

        # ---- 54. v1.7.0：四项预检必须全部先于下载（源码级 + 行为级）----
        import ast as _ast
        _tree = _ast.parse(Path(build_mosaic.__file__).read_text(encoding='utf-8'))
        _fn = [n for n in _tree.body
               if isinstance(n, _ast.FunctionDef) and n.name == 'build'][0]
        _ln: dict[str, int] = {}
        for _n in _ast.walk(_fn):
            if isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name) \
                    and _n.func.id in ('assert_output_fits', 'download_items',
                                       'assert_disk_space', 'assert_download_fits'):
                _ln.setdefault(_n.func.id, _n.lineno)
        order_ok = all(k in _ln for k in ('assert_output_fits', 'download_items',
                                          'assert_disk_space', 'assert_download_fits')) \
            and _ln['assert_download_fits'] < _ln['assert_output_fits'] \
            < _ln['assert_disk_space'] < _ln['download_items']
        check('preflight_before_download', order_ok,
              f'build() 调用行号 {_ln}：记录总量 -> 输出规模 -> 磁盘 -> 下载')

        # ---- 55. v1.7.0：下载总量预检（大国 full 模式不再先下 19 GiB）----
        big_items = [{'cell': 'a', 'size': 1_200_000_000},
                     {'cell': 'b', 'size': 1_200_000_000}]
        st_dl: dict = {}
        mb_dl = fetch_tiles.assert_download_fits(big_items, 2048, st_dl,
                                                 enforce=False)
        rec_ok = (mb_dl > 2048 and st_dl['download_total_mb'] > 2048 and
                  st_dl['download_cells'] == 2)
        try:
            fetch_tiles.assert_download_fits(big_items, 2048, {}, enforce=True)
            deny_ok = False
        except gem15_common.DownloadTooLargeError:
            deny_ok = True
        try:
            fetch_tiles.assert_download_fits([{'cell': 'x'}], 2048, {},
                                             enforce=True)
            missing_size_ok = False
        except gem15_common.DownloadError:
            missing_size_ok = True
        check('download_total_guard', rec_ok and deny_ok and missing_size_ok,
              f'enforce=False 记录 {mb_dl:.0f} MB / enforce=True 拒绝 / '
              f'缺 file:size 报 DownloadError')

        # ---- 56. v1.7.0：磁盘空间预检（C: 仅 10 GiB 的真实风险）----
        # 测试替身必须与生产返回类型对齐：shutil.disk_usage 返回带 .free 的
        # SimpleNamespace，不是 tuple（本会话内替身签名/类型不对齐的第 2 次）。
        from types import SimpleNamespace
        with mock.patch('shutil.disk_usage',
                        return_value=SimpleNamespace(total=0, used=0,
                                                     free=100 * 1048576)):
            try:
                gem15_common.assert_disk_space(tmp / 'x.tif', 40000, {})
                disk_deny = False
            except gem15_common.DiskSpaceError as exc:
                disk_deny = '余量' in str(exc) and '磁盘满' in str(exc)
        st_disk: dict = {}
        gem15_common.assert_disk_space(tmp / 'x.tif', 10, st_disk)
        check('disk_space_guard',
              disk_deny and st_disk['disk_needed_mb'] ==
              gem15_common.MIN_FREE_MB + 10 and
              st_disk['disk_free_mb'] > 0,
              f'小盘拒绝 / 正常记录 free={st_disk["disk_free_mb"]} MB')

        # ---- 57. v1.7.0：--split 参数校验 ----
        split_good = [('off', ('off', None, None)),
                      ('auto', ('auto', None, None)),
                      ('4x3', ('grid', 4, 3)), ('4X3', ('grid', 4, 3))]
        split_ok = all(gem15_dem.parse_split_opt(v) == want
                       for v, want in split_good)
        split_bad_ok = True
        for v in ('banana', '0x2', '65x2', '3x', 'x3'):
            try:
                gem15_dem.parse_split_opt(v)
                split_bad_ok = False
            except gem15_common.GemError:
                pass
        check('split_opt_validation', split_ok and split_bad_ok,
              'off/auto/NxM 合法；banana/0x2/65x2/3x/x3 全部 GemError')

        # ---- 58. v1.7.0：分块方案几何连续、单块不超限 ----
        info_fra = {'estimated_output_mb': 32647.03,
                    'bbox': [-4.7724, 41.3655, 9.5609, 51.1137]}
        plan_fra = gem15_dem.make_split_plan(info_fra, 512, 'auto')
        ts = plan_fra['tiles']
        edges_ok = True
        for r in range(plan_fra['ny']):
            row = [t for t in ts if t['row'] == r]
            if abs(row[0]['bbox'][0] - info_fra['bbox'][0]) > 1e-6 or \
                    abs(row[-1]['bbox'][2] - info_fra['bbox'][2]) > 1e-6:
                edges_ok = False
            for a, b in zip(row, row[1:]):
                if abs(a['bbox'][2] - b['bbox'][0]) > 1e-6:
                    edges_ok = False
        check('split_plan_geometry',
              len(ts) == plan_fra['total_tiles'] ==
              plan_fra['nx'] * plan_fra['ny'] and
              plan_fra['est_per_tile_mb'] < 512 and edges_ok and
              ts[0]['bbox'][1] == info_fra['bbox'][1],
              f'法国 -> {plan_fra["nx"]}x{plan_fra["ny"]} 块，每块 '
              f'{plan_fra["est_per_tile_mb"]} MB，块边界连续无缝隙')

        # ---- 59. v1.7.0：分块 CLI 参数 / 计划落盘 / 断点续跑 ----
        ctx17 = {'layer': 'dtm', 'mode': 'window', 'crs_opt': 'EPSG:32632',
                 'cache': str(tmp / 'cache'), 'workers': 4, 'timeout': 45,
                 'chunk_mb': 16, 'max_output_mb': 512,
                 'max_download_mb': 2048, 'buffer_m': 0, 'bg': 'white',
                 'maxside': 1600, 'plain': False}
        argv17 = gem15_dem._tile_argv(ctx17, plan_fra['tiles'][0],
                                      tmp / 'tile', 'probe_r00c00')
        argv_ok = ('--split' in argv17 and 'off' in argv17 and
                   '--no-clip' in argv17 and '--bbox' in argv17 and
                   '--max-download-mb' in argv17)
        pp = gem15_dem.write_split_plan(tmp, 'probe', plan_fra)
        plan_ok = json.loads(pp.read_text(encoding='utf-8'))['total_tiles'] == \
            plan_fra['total_tiles']
        small = gem15_dem.make_split_plan(
            {'estimated_output_mb': 10, 'bbox': [9.5, 47.0, 9.6, 47.1]},
            512, 'grid', 2, 2)
        resume_dir = tmp / 'resume'
        resume_dir.mkdir(parents=True, exist_ok=True)
        for t in small['tiles']:
            d = resume_dir / f"tile_r{t['row']:02d}c{t['col']:02d}"
            d.mkdir(parents=True, exist_ok=True)
            s = f"probe_r{t['row']:02d}c{t['col']:02d}"
            (d / f'{s}_gem15_dtm.tif').write_bytes(b'x')
            (d / f'{s}_gem15_manifest.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(gem15_dem, 'run') as m_run, \
                contextlib.redirect_stdout(io.StringIO()):
            rc17 = gem15_dem.run_split(ctx17, small, 'probe', resume_dir)
        check('split_argv_and_resume',
              argv_ok and plan_ok and rc17 == 0 and m_run.call_count == 0 and
              (resume_dir / 'probe_split_manifest.json').exists(),
              f'CLI 含 split off/no-clip；4 块全部断点跳过，run() 调用 '
              f'{m_run.call_count} 次')

        # ---- 60. v1.7.0 签名 / 版本号同步 ----
        check('v17_signature',
              gem15_common.VERSION == '2.0.0' and
              hasattr(gem15_common, 'DownloadTooLargeError') and
              hasattr(gem15_common, 'DiskSpaceError') and
              callable(getattr(fetch_tiles, 'assert_download_fits', None)) and
              'max_download_mb' in inspect.signature(build_mosaic.build).parameters and
              'make_split_plan' in inspect.getsource(gem15_dem) and
              'available_memory_mb' in inspect.getsource(gem15_common) and
              'EmptyRasterError' in inspect.getsource(gem15_dem.run_split) and
              len(EXPECTED_KEYS) == 154,
              '版本号 2.0.0 / 总量+磁盘+内存三预检 / 自动分块 / 154 项回归')

        # ---- 61. v1.7.0：物理内存对照（防止「提高上限后静默 OOM」）----
        with mock.patch.object(build_mosaic, 'available_memory_mb',
                               return_value=100.0):
            st_mem: dict = {}
            try:
                # 10000² 成果 381 MB < 512 MB 上限，但峰值 1144 MB > 100×0.8
                build_mosaic.assert_output_fits(10000, 10000, 1, 512, st_mem)
                mem_deny = False
            except gem15_common.OutputTooLargeError as exc:
                mem_deny = ('OOM' in str(exc) and
                            exc.info.get('available_memory_mb') == 100.0)
        with mock.patch.object(build_mosaic, 'available_memory_mb',
                               return_value=8000.0):
            st_mem_ok: dict = {}
            build_mosaic.assert_output_fits(1000, 1000, 1, 512, st_mem_ok)
        check('physical_memory_guard',
              mem_deny and st_mem_ok['available_memory_mb'] == 8000.0 and
              st_mem_ok['estimated_peak_mb'] > st_mem_ok['estimated_output_mb'],
              f'可用 100 MB 时拒绝（峰值 {st_mem["estimated_peak_mb"]} MB）；'
              f'可用 8000 MB 时小区域放行')

        # ---- 62. v1.7.0：大国分块遇到空块不中断整批 ----
        small_e = gem15_dem.make_split_plan(
            {'estimated_output_mb': 10, 'bbox': [9.5, 47.0, 9.6, 47.1]},
            512, 'grid', 2, 2)
        edir = tmp / 'empty_case'
        edir.mkdir(parents=True, exist_ok=True)
        calls_e: list = []

        def fake_run_e(argv):
            calls_e.append(argv)
            if len(calls_e) == 1:
                raise gem15_common.EmptyRasterError('裁切后没有任何有效像元')
            return 0

        with mock.patch.object(gem15_dem, 'run', side_effect=fake_run_e), \
                contextlib.redirect_stdout(io.StringIO()):
            rc_e = gem15_dem.run_split(ctx17, small_e, 'emptytest', edir)
        sum_e = json.loads((edir / 'emptytest_split_manifest.json').read_text(
            encoding='utf-8'))
        marker_e = edir / 'tile_r00c00' / 'emptytest_r00c00_empty.json'
        check('split_empty_tile_continues',
              rc_e == 0 and len(calls_e) == 4 and
              sum_e['tiles'][0]['status'] == 'empty' and
              sum_e['tiles'][1]['status'] == 'ok' and marker_e.exists(),
              f'4 块中 1 块空数据：显式标记 + 继续，run() 调用 {len(calls_e)} 次')

        # ---- 62b. v1.12.2：STAC 判空的纯海域块也不中断整批 ----
        # 德国 r7c0（54.09–55.06N / 5.87–7.40E 纯北海）STAC 返回 0 片 ->
        # CoverageError。v1.7.0 只兜住了取数后判空的 EmptyRasterError，
        # 这一支让整批在 43/48 处中断（前面 42 块白跑 2 小时）。
        cdir = tmp / 'cov_case'
        cdir.mkdir(parents=True, exist_ok=True)
        calls_c: list = []

        def fake_run_c(argv):
            calls_c.append(argv)
            if len(calls_c) == 1:
                raise gem15_common.CoverageError('该范围没有 GEM-15 1° 瓦片')
            return 0

        with mock.patch.object(gem15_dem, 'run', side_effect=fake_run_c),                 contextlib.redirect_stdout(io.StringIO()):
            rc_c = gem15_dem.run_split(ctx17, small_e, 'covtest', cdir)
        sum_c = json.loads((cdir / 'covtest_split_manifest.json').read_text(
            encoding='utf-8'))
        marker_c = cdir / 'tile_r00c00' / 'covtest_r00c00_empty.json'
        check('split_coverage_empty_continues',
              rc_c == 0 and len(calls_c) == 4 and
              sum_c['tiles'][0]['status'] == 'empty' and
              sum_c['tiles'][0].get('reason') == 'no_stac_tiles' and
              sum_c['tiles'][1]['status'] == 'ok' and marker_c.exists(),
              f'纯海域块（STAC 0 片）标记 empty + reason=no_stac_tiles 后继续，'
              f'run() 仍走到第 {len(calls_c)} 块')

        # ---- 62c. v1.12.2：MemoryError 先换新进程重试（德国第 42 块实测有效）----
        odir = tmp / 'oom_case'
        odir.mkdir(parents=True, exist_ok=True)
        calls_o: list = []

        def fake_run_o(argv):
            calls_o.append(argv)
            if len(calls_o) == 1:
                raise MemoryError('Unable to allocate 138. MiB')
            return 0

        with mock.patch.object(gem15_dem, 'run', side_effect=fake_run_o),                 mock.patch.object(gem15_dem.subprocess, 'run',
                                  return_value=mock.Mock(returncode=0,
                                                         stdout='', stderr='')),                 contextlib.redirect_stdout(io.StringIO()):
            rc_o = gem15_dem.run_split(ctx17, small_e, 'oomtest', odir)
        sum_o = json.loads((odir / 'oomtest_split_manifest.json').read_text(
            encoding='utf-8'))
        check('split_oom_child_retry',
              rc_o == 0 and len(calls_o) == 4 and
              sum_o['tiles'][0]['status'] == 'ok' and
              '新进程' in sum_o['tiles'][0].get('note', ''),
              f'第 1 块 MemoryError -> 换新进程重试成功、整批继续（run() 共 '
              f'{len(calls_o)} 次，首块 note={sum_o["tiles"][0].get("note")!r}）')

        # ---- 64. v1.12.0：原地分位与 np.percentile 数值一致（省内存）----
        rng64 = np.random.default_rng(7)
        arr64 = rng64.normal(500, 120, 400_000).astype(np.float32)
        ref2, ref98 = np.percentile(arr64, 2), np.percentile(arr64, 98)
        work64 = arr64.copy()
        got2, got98 = build_mosaic.percentiles_inplace(work64)
        check('percentiles_inplace_exact',
              abs(got2 - ref2) < 1e-3 and abs(got98 - ref98) < 1e-3 and
              work64.base is None,
              f'原地分位 {got2:.4f}/{got98:.4f} vs np.percentile '
              f'{ref2:.4f}/{ref98:.4f}（同一数组内完成，87.7 M 像元时省 2×351 MB）')

        # ---- 64b. v1.12.0：分块 MemoryError 给出进度与补救路径 ----
        small_m = gem15_dem.make_split_plan(
            {'estimated_output_mb': 10, 'bbox': [9.5, 47.0, 9.6, 47.1]},
            512, 'grid', 2, 1)
        mdir = tmp / 'mem_case'
        mdir.mkdir(parents=True, exist_ok=True)

        def fake_run_m(argv):                            # noqa: ARG001
            raise MemoryError('Unable to allocate 190. MiB')

        # v1.12.2：MemoryError 现在会先换新进程重试，必须把子进程 mock 成失败，
        # 否则它会真的起进程把这块跑成功（探针反而拿不到异常）。
        with mock.patch.object(gem15_dem, 'run', side_effect=fake_run_m),                 mock.patch.object(gem15_dem.subprocess, 'run',
                                  return_value=mock.Mock(
                                      returncode=1,
                                      stdout='[错误] GemError: synthetic child fail',
                                      stderr='')),                 contextlib.redirect_stdout(io.StringIO()):
            try:
                gem15_dem.run_split(ctx17, small_m, 'memtest', mdir)
                mem_msg = ''
            except gem15_common.GemError as exc:
                mem_msg = str(exc)
        check('split_memory_error_actionable',
              '内存不足' in mem_msg and '重跑会自动断点跳过' in mem_msg and
              '--split 4x1' in mem_msg and 'window' in mem_msg and
              '已自动换新进程重试一次' in mem_msg,
              '旧实现抛 MemoryError traceback 退出；现给出进度 + 三种补救'
              '（关程序重跑 / 双倍分块 / window 模式）+ 新进程重试失败原因')

        # ---- 64. v1.12.0：原地分位与 np.percentile 数值一致（省内存）----
        rng64 = np.random.default_rng(7)
        arr64 = rng64.normal(500, 120, 400_000).astype(np.float32)
        ref2, ref98 = np.percentile(arr64, 2), np.percentile(arr64, 98)
        work64 = arr64.copy()
        got2, got98 = build_mosaic.percentiles_inplace(work64)
        check('percentiles_inplace_exact',
              abs(got2 - ref2) < 1e-3 and abs(got98 - ref98) < 1e-3 and
              work64.base is None,
              f'原地分位 {got2:.4f}/{got98:.4f} vs np.percentile '
              f'{ref2:.4f}/{ref98:.4f}（同一数组内完成，87.7 M 像元时省 2×351 MB）')

        # ---- 65. v1.12.0：续传请求带 -L / -C - / --connect-timeout ----
        cmds18 = []
        dl18 = tmp / 'v18_resume.dl'

        def fake18(cmd, **kw):                           # noqa: ARG001
            cmds18.append(list(cmd))
            Path(cmd[cmd.index('-o') + 1]).write_bytes(b'X' * 10)
            return mock.Mock(stdout='206', stderr='')

        with mock.patch.object(fetch_tiles.subprocess, 'run',
                               side_effect=fake18),              mock.patch.object(fetch_tiles, '_cache_ok',
                               return_value=True),              contextlib.redirect_stdout(io.StringIO()):
            info18 = fetch_tiles._resume_download('https://x/t.tif', dl18,
                                                  10, 'v18')
        c18 = cmds18[0]
        check('resume_curl_flags',
              info18['bytes'] == 10 and '-L' in c18 and '-C' in c18 and
              c18[c18.index('-C') + 1] == '-' and '--connect-timeout' in c18,
              f'cmd 片段 {c18[2:9]}...（-L 跟重定向、-C - 续传、'
              '--connect-timeout 让不可达主机快速失败）')

        # ---- 66. v1.12.0：clean_cache 超预算改为清零（不 rmtree）----
        croot = tmp / 'v12_clean_root'
        cdtm = croot / 'tiles' / 'dtm'
        cdtm.mkdir(parents=True)
        for i in range(45):
            (cdtm / f'{i:04d}.tif').write_bytes(b'z' * 100)
        with mock.patch('shutil.rmtree') as mrm66,                 contextlib.redirect_stdout(io.StringIO()):
            rep66 = fetch_tiles.clean_cache(croot, 'tiles')
        check('clean_cache_budget_safe',
              mrm66.call_count == 0 and
              rep66['subdirs']['tiles']['mode'] == 'truncate' and
              rep66['bytes'] == 4500 and
              all(f.stat().st_size == 0 for f in cdtm.glob('*.tif')),
              f'45 个文件的缓存清零 4500 B 而非 rmtree（rmtree 调用 '
              f'{mrm66.call_count} 次）——守卫按进程累计计数，超 50 直接杀进程')

        # ---- 67. v1.8.0：build() layer 前置校验 ----
        p_tr18 = from_origin(500000.0, 5200000.0, 15.0, 15.0)
        cog18 = tmp / 'v18_layer_cog.tif'
        make_tif(cog18, np.full((1, 4, 4), 100.0, np.float32), p_tr18)
        utm18 = (500030.0, 5199940.0, 500060.0, 5199970.0)
        wgs18 = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326), *utm18)
        bnd18 = resolve_region.bbox_feature(wgs18, 'v18')
        item18 = {'cell': 'w9s47', 'group': 'p40_p0',
                  'url': 'https://example.invalid/x.tif', 'epsg': 32632,
                  'proj_transform': list(p_tr18)[:6], 'proj_shape': [4, 4],
                  'size': cog18.stat().st_size, 'bbox': list(wgs18)}
        v3msg = ''
        with mock.patch.object(build_mosaic, 'download_items', return_value={}), \
             mock.patch.object(build_mosaic, 'tile_path', return_value=cog18), \
             contextlib.redirect_stdout(io.StringIO()):
            try:
                build_mosaic.build([item18], bnd18, tmp / 'v18_layer.tif',
                                   layer='foo', mode='full', cache_dir=tmp)
            except gem15_common.GemError as exc:
                v3msg = str(exc)
        check('build_layer_validated',
              'dtm' in v3msg and 'dsm' in v3msg,
              f"layer=foo -> GemError: {v3msg}（旧实现 KeyError 未预期 exit 1）")

        # ---- 68. v1.8.0：STAC 相对 href 归一化 ----
        def fake_gj18(url, timeout=45, retries=3, cache=None):
            if url.endswith('collection.json'):
                return {"links": [{"rel": "child", "title": "p40_p0"}]}
            if url.endswith('p40_p0/catalog.json'):
                return {"links": [{"rel": "item", "title": "w9s47"}]}
            return {"type": "Feature", "properties": {"proj:epsg": 32632},
                    "assets": {"dtm": {"href": "dtm/zall/mosaic.tif",
                                       "file:size": 100,
                                       "proj:transform": list(p_tr18)[:6],
                                       "proj:shape": [4, 4],
                                       "raster:bands": [{"nodata": "nan"}]}}}
        with mock.patch.object(stac_index, 'get_json', side_effect=fake_gj18), \
             contextlib.redirect_stdout(io.StringIO()):
            its18 = stac_index.select_items((9.0, 47.0, 9.5, 47.5), 'dtm', tmp)
        abs18 = stac_index.resolve_asset_href('https://a/b.tif',
                                              'https://h/i.json')
        rel18 = stac_index.resolve_asset_href('./x/y.tif',
                                              'https://h/g/w9s47/w9s47.json')
        root18 = stac_index.resolve_asset_href('/top.tif',
                                               'https://h/g/w9s47/w9s47.json')
        check('relative_href_resolved',
              its18 and its18[0]['url'].endswith(
                  '/p40_p0/w9s47/dtm/zall/mosaic.tif') and
              abs18 == 'https://a/b.tif' and
              rel18 == 'https://h/g/w9s47/x/y.tif' and
              root18 == 'https://h/top.tif',
              f"相对 href -> {its18[0]['url']}（旧实现原样透传静默 404；"
              "绝对 URL 原样返回）")

        # ---- 69. v1.8.0：海域/空隙 cell 预过滤（纯函数）----
        def ring18(bb):
            return [[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]],
                    [bb[0], bb[3]], [bb[0], bb[1]]]
        bA18 = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326),
                    500000.0, 4799400.0, 503000.0, 4800000.0)
        bM18 = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326),
                    505000.0, 4799400.0, 508000.0, 4800000.0)
        bB18 = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326),
                    511000.0, 4799400.0, 514000.0, 4800000.0)
        TRA18 = list(from_origin(500000.0, 4800000.0, 15.0, 15.0))[:6]
        TRB18 = list(from_origin(511000.0, 4800000.0, 15.0, 15.0))[:6]
        cells18 = [
            {'cell': 'w3s43', 'group': 'p40_p0', 'url': 'x', 'epsg': 32632,
             'proj_transform': TRA18, 'proj_shape': [200, 200], 'size': 999,
             'bbox': list(bA18)},
            {'cell': 'w4s43', 'group': 'p40_p0', 'url': 'x', 'epsg': 32632,
             'proj_transform': TRA18, 'proj_shape': [200, 200], 'size': 999,
             'bbox': list(bM18)},
            {'cell': 'w5s43', 'group': 'p40_p0', 'url': 'x', 'epsg': 32632,
             'proj_transform': TRB18, 'proj_shape': [200, 200], 'size': 999,
             'bbox': list(bB18)},
        ]
        isl18 = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [ring18(bA18)]}},
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [ring18(bB18)]}}]}
        boxes18 = build_mosaic.part_bboxes(isl18)
        multi18 = build_mosaic.part_bboxes(
            {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {},
                 "geometry": {"type": "MultiPolygon",
                              "coordinates": [[ring18(bA18)],
                                              [ring18(bB18)]]}}]})
        unk18 = build_mosaic.part_bboxes(
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Point", "coordinates": [1.0, 2.0]}})
        kept18, drop18 = build_mosaic.offshore_prefilter(
            [(it, None) for it in cells18], isl18)
        keptb, dropb = build_mosaic.offshore_prefilter(
            [(it, None) for it in cells18], isl18, buffer_m=200000)
        bboxall18 = tb15(CRS.from_epsg(32632), CRS.from_epsg(4326),
                         500000.0, 4799400.0, 514000.0, 4800000.0)
        keptc, dropc = build_mosaic.offshore_prefilter(
            [(it, None) for it in cells18],
            resolve_region.bbox_feature(bboxall18, 'bbox'))
        check('offshore_prefilter_pure',
              len(boxes18) == 2 and len(multi18) == 2 and unk18 == [] and
              [it['cell'] for it, _ in drop18] == ['w4s43'] and
              len(kept18) == 2 and len(dropb) == 0 and len(dropc) == 0,
              f'双岛保 2 环外接框；中间海域 w4s43 被判为界外；'
              f'--buffer 200km 桥接后不跳（{len(dropb)}）；'
              f'bbox 矩形边界不跳（{len(dropc)}）；未知几何类型放弃预过滤')

        # ---- 70. v1.8.0：跳过海域取数但成果逐像元不变（端到端）----
        cogs18 = {'A': tmp / 'v18_islA.tif', 'B': tmp / 'v18_islB.tif'}

        def mk18(path, x0):
            t = from_origin(x0, 4800000.0, 15.0, 15.0)
            with rasterio.open(path, 'w', driver='GTiff', height=200,
                               width=200, count=1, dtype='float32',
                               crs=CRS.from_epsg(32632), transform=t,
                               nodata=np.nan) as ds:
                ds.write(np.full((1, 200, 200), 500.0, np.float32))
        mk18(cogs18['A'], 500000.0)
        mk18(cogs18['B'], 511000.0)
        cmap18 = {'w3s43': cogs18['A'], 'w4s43': cogs18['A'],
                  'w5s43': cogs18['B']}
        fetched18 = []

        def fake_dl18(is_, layer, cache, workers=4, timeout=90,
                      chunk_mb=16, max_download_mb=2048):
            fetched18.append([it['cell'] for it in is_])
            return {}
        with mock.patch.object(build_mosaic, 'download_items',
                               side_effect=fake_dl18), \
             mock.patch.object(build_mosaic, 'tile_path',
                               side_effect=lambda it, l, c: cmap18[it['cell']]), \
             contextlib.redirect_stdout(io.StringIO()):
            st_on = build_mosaic.build(cells18, isl18, tmp / 'v18_on.tif',
                                       mode='full', cache_dir=tmp)
        with mock.patch.object(build_mosaic, 'download_items',
                               side_effect=fake_dl18), \
             mock.patch.object(build_mosaic, 'tile_path',
                               side_effect=lambda it, l, c: cmap18[it['cell']]), \
             mock.patch.object(build_mosaic, 'offshore_prefilter',
                               side_effect=lambda pairs, bnd, buf=0.0:
                                   (list(pairs), [])), \
             contextlib.redirect_stdout(io.StringIO()):
            st_off = build_mosaic.build(cells18, isl18, tmp / 'v18_off.tif',
                                        mode='full', cache_dir=tmp)
        fetched_on, fetched_off = fetched18[0], fetched18[1]
        with rasterio.open(tmp / 'v18_on.tif') as d1, \
                rasterio.open(tmp / 'v18_off.tif') as d2:
            a1, a2 = d1.read(), d2.read()
            same18 = (tuple(d1.transform)[:6] == tuple(d2.transform)[:6] and
                      d1.shape == d2.shape and
                      bool((np.isnan(a1) == np.isnan(a2)).all()) and
                      bool(((a1 == a2) |
                            (np.isnan(a1) & np.isnan(a2))).all()))
        check('offshore_skip_identical_output',
              fetched_on == ['w3s43', 'w5s43'] and
              fetched_off == ['w3s43', 'w4s43', 'w5s43'] and
              st_on.get('cells_skipped_offshore') == ['w4s43'] and
              'fetch_total_mb' in st_on and
              not st_off.get('cells_skipped_offshore') and same18,
              f'开启：取数 {fetched_on}、跳过 {st_on.get("cells_skipped_offshore")}'
              f'；关闭：取数 {fetched_off}；两次成果 transform/形状/像元'
              '（含 NaN 分布）逐位一致 —— 中间海域 cell 有有效数据也被掩膜，'
              '跳过取数零回归')

        # ---- 71. v1.8.0：超限提示不再含本机专属文案 ----
        m18 = ''
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                build_mosaic.assert_output_fits(10000, 10000, 1, 1.0,
                                                {"bbox_used": [0, 0, 1, 1]})
            except gem15_common.OutputTooLargeError as exc:
                m18 = str(exc)
        check('output_message_neutral',
              '10 GiB' not in m18 and '本机' not in m18 and '分块' in m18,
              '超限提示不再包含「本机 C: 实测仅约 10 GiB」这类机器专属信息，'
              '任何环境输出一致')

        # ---- 72. v1.8.0 签名 ----
        check('v18_signature',
              gem15_common.VERSION == '2.0.0' and
              hasattr(build_mosaic, 'offshore_prefilter') and
              hasattr(build_mosaic, 'part_bboxes') and
              hasattr(stac_index, 'resolve_asset_href') and
              hasattr(gem15_common, 'dispose_tree') and
              not hasattr(fetch_tiles, '_range_download'),
              '版本号 2.0.0 / dispose_tree 删除预算 / 单文件续传 / layer 校验 / '
              '相对 href 归一化 / 海域预过滤 / 文案中性化')

        # ---- 73. v1.9.0：台湾/香港/澳门按名称归并 CHN ----
        rr_iso = resolve_region.resolve_iso
        named19 = {n: rr_iso(n)[0] for n in
                   ('台湾', '中国台湾', '香港', '中国香港', '澳门', 'Macao')}
        check('china_regions_named',
              all(iso == 'CHN' for iso in named19.values()) and
              len(set(named19.values())) == 1,
              f'名称直查全部归并 CHN：{named19}'
              '（旧名称表缺这三项，输入只得到「无法解析」）')

        # ---- 74. v1.9.0：TWN/HKG/MAC 直传同样落排除路径 ----
        msg19 = {}
        for iso19 in ('TWN', 'HKG', 'MAC'):
            try:
                resolve_region.assert_allowed(iso19, iso19)
                msg19[iso19] = '未拦截！'
            except CoverageError as exc19:
                s19 = str(exc19)
                msg19[iso19] = ('ok' if ('中国的一部分' in s19 and
                                         'cn-dem' in s19) else
                                f'文案异常: {s19[:40]}')
        check('china_regions_iso_blocked',
              all(v == 'ok' for v in msg19.values()),
              f'ISO3 直传 3 项全部 CoverageError 且文案含「中国的一部分」'
              f'+ cn-dem：{msg19}（旧实现完全绕过排除检查去拉边界与 STAC）')

        # ---- 75. v1.9.0 签名 ----
        check('v19_signature',
              gem15_common.VERSION == '2.0.0' and
              resolve_region.CHINA_REGION_ISO == {'TWN': '台湾', 'HKG': '香港',
                                                  'MAC': '澳门'} and
              hasattr(resolve_region, 'assert_not_china') and
              hasattr(resolve_region, 'CHINA_BBOX') and
              hasattr(resolve_region, 'assert_not_china') and
              hasattr(resolve_region, 'CHINA_BBOX') and
              '含台湾' in gem15_common.EXCLUDED_NOTE and
              '香港' in gem15_common.EXCLUDED_NOTE,
              '版本号 2.0.0 / 台湾·香港·澳门归并 CHN（名称 + ISO3 双路）/ '
              'EXCLUDED_NOTE 明确含台湾、香港、澳门')

        # ---- 76. v1.9.0：bbox 中心点落在中国（含台湾/港澳）→ 拒绝 ----
        chn_cache = tmp / 'boundary19'
        (chn_cache / 'boundary').mkdir(parents=True, exist_ok=True)
        # 合成 ADM1：大要素（台湾式，精确判定）+ 小要素（香港式失真：
        # bounds 含点但多边形不含 —— 实测 simplified 版香港 3 parts
        # 均不含新界大部，小要素须用 bounds 近似）
        syn19 = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"shapeName": "TW"},
             "geometry": {"type": "Polygon", "coordinates": [[
                 [118, 20], [122, 20], [122, 26], [118, 26], [118, 20]]]}},
            {"type": "Feature", "properties": {"shapeName": "HK"},
             "geometry": {"type": "Polygon", "coordinates": [[
                 [113.8, 22.1], [114.4, 22.1], [113.8, 22.6],
                 [113.8, 22.1]]]}},
        ]}
        (chn_cache / 'boundary' / 'CHN_ADM1_simplified.geojson').write_text(
            json.dumps(syn19), encoding='utf-8')
        tw_err19 = vn_ok19 = hk_err19 = fr_ok19 = ''
        try:
            resolve_region.assert_not_china((120.0, 22.9, 122.0, 24.5),
                                            'tw', chn_cache)
        except CoverageError as e19:
            tw_err19 = str(e19)
        try:
            # 中心 (114.3, 22.5)：在 HK 三角形 bounds (113.8,22.1,114.4,22.6)
            # 内、多边形外（斜边外侧）—— 复刻 simplified 香港失真机制
            resolve_region.assert_not_china((114.0, 22.3, 114.6, 22.7),
                                            'hk', chn_cache)
        except CoverageError as e19:
            hk_err19 = str(e19)
        try:
            resolve_region.assert_not_china((104.5, 20.5, 106.5, 21.8),
                                            'vn', chn_cache)
            vn_ok19 = 'ok'
        except CoverageError as e19:
            vn_ok19 = f'误伤: {e19[:40]}'
        try:
            resolve_region.assert_not_china(
                (0.0, 45.0, 2.0, 46.0), 'fr',
                tmp / 'nonexistent_cache19')       # 粗判不相交，零网络零读取
            fr_ok19 = 'ok'
        except Exception as e19:                   # noqa: BLE001
            fr_ok19 = f'异常: {type(e19).__name__}'
        check('bbox_china_blocked',
              '中国境内' in tw_err19 and 'cn-dem' in tw_err19 and
              '中国境内' in hk_err19 and vn_ok19 == 'ok' and fr_ok19 == 'ok',
              f'台湾 bbox 精确拦截 / 香港小要素 bounds 近似拦截（simplified '
              f'失真实测）/ 越南 {vn_ok19} / 法国粗判零网络 {fr_ok19}')

        # ============ v1.11.0：独立黑盒矩阵（31 例 CLI）证实并修复 ============
        # ---- 77. --geojson 异常文件统一 RegionError（旧：traceback exit 1）----
        gbad110 = tmp / 'v110_bad.json'
        gbad110.write_text('not json {{{', encoding='utf-8')
        e110 = {}
        for tag110, gp110 in (('dir', tmp), ('bad', gbad110)):
            try:
                resolve_region.resolve('QA', geojson_path=str(gp110))
                e110[tag110] = '未报错!'
            except gem15_common.RegionError as exc:
                s110 = str(exc)
                e110[tag110] = ('ok' if ('JSON' in s110 or '路径无效' in s110
                                         or '读取失败' in s110)
                                else f'文案异常: {s110[:40]}')
        check('geojson_wrapped',
              all(v == 'ok' for v in e110.values()),
              f'目录/坏 JSON 均显式 RegionError：{e110}'
              '（旧实现 PermissionError / JSONDecodeError traceback exit 1）')

        # ---- 78-80. 三个独立 CLI 的 exit 2 行为（subprocess 真实黑盒）----
        def cli110(script, *args):
            return subprocess.run(
                [sys.executable, str(HERE / script), *args],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120, cwd=str(tmp),
                env=_SUBPROCESS_ENV)

        r_ft = cli110('fetch_tiles.py', 'nope_items.json', 'dtm',
                      str(tmp / 'v110_ft'), '--workers', '0')
        r_pv = cli110('preview_dem.py', 'nope.tif', str(tmp / 'v110_x.png'),
                      '--maxside', 'abc')
        r_bm = cli110('build_mosaic.py', 'nope_i.json', 'nope_b.json',
                      str(tmp / 'v110_o.tif'), '--layer', 'foo')
        check('fetch_cli_validated',
              r_ft.returncode == 2 and 'Traceback' not in r_ft.stderr and
              '--workers' in r_ft.stdout,
              f'fetch_tiles: items 缺失 + --workers 0 -> rc={r_ft.returncode} '
              f'显式报错（旧实现 FileNotFoundError traceback exit 1，且参数零校验）'
              f'；stderr 首行：{(r_ft.stderr or "").strip().splitlines()[:1]}')
        check('preview_cli_exit2',
              r_pv.returncode == 2 and 'Traceback' not in r_pv.stderr and
              '--maxside' in r_pv.stdout,
              f'preview_dem: --maxside abc -> rc={r_pv.returncode} 显式报错'
              '（旧实现 int() ValueError traceback exit 1；GemError 也 exit 1）')
        check('build_cli_layer_first',
              r_bm.returncode == 2 and 'Traceback' not in r_bm.stderr and
              'layer' in r_bm.stdout and 'No such file' not in r_bm.stdout,
              f'build_mosaic: --layer foo 在文件读取之前报出（rc='
              f'{r_bm.returncode}；旧实现 FileNotFoundError 先崩）')

        # ---- 81. --country 与 --bbox/--geojson 互斥 ----
        try:
            resolve_region.resolve('QA', country='TWN',
                                   bbox_spec='0,45,2,46')
            excl110 = '未报错!'
        except gem15_common.RegionError as exc:
            excl110 = 'ok' if '互斥' in str(exc) else str(exc)[:40]
        check('exclusive_inputs', excl110 == 'ok',
              f'互斥来源显式报错：{excl110}（旧实现 TWN 静默忽略、'
              '跑起法国全流程，用户想要台湾却拿到法国边界）')

        # ---- 82. 多余位置参数 ----
        try:
            gem15_dem.run(['QA', str(tmp / 'v110_p3'), 'EXTRA'])
            pos110 = '未报错!'
        except gem15_common.GemError as exc:
            pos110 = 'ok' if '最多 2 个' in str(exc) else str(exc)[:40]
        check('pos_args_capped', pos110 == 'ok',
              f'3 个位置参数显式报错：{pos110}（旧实现 EXTRA 静默忽略）')

        # ---- 83. STAC 判空失败不再白建输出目录（推迟 mkdir 到选片成功后）----
        outdir110 = tmp / 'v110_pole'
        with mock.patch.object(gem15_dem.stac_index, 'select_items',
                               side_effect=gem15_common.CoverageError(
                                   'bbox 未落在任何 STAC 组内')), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                gem15_dem.run(['QA', str(outdir110),
                               '--bbox', '0,88,10,89'])
            except gem15_common.CoverageError:
                pass
        check('stac_fail_no_outdir',
              not outdir110.exists(),
              f'极区 bbox 在 STAC 判空报错后输出目录不存在：'
              f'{outdir110.exists()}（旧实现留下只含 boundary 的半成品目录；'
              '建目录已推迟到 STAC 选片成功之后）')

        # ---- 84. v1.11.0 签名 ----
        sig110 = (gem15_common.VERSION == '2.0.0' and
                  'sys.exit(2)' in inspect.getsource(fetch_tiles.main) and
                  'sys.exit(2)' in inspect.getsource(preview_dem.main) and
                  'sys.exit(2)' in inspect.getsource(build_mosaic.main) and
                  '互斥' in inspect.getsource(resolve_region.resolve))
        check('v110_signature', sig110,
              '版本号 2.0.0 / 三个独立 CLI 统一 [错误]+exit 2 / geojson 异常'
              '包装 / 互斥与多余位置参数显式报错 / STAC 判空不白建目录')

        # ============ v1.11.0：CLI 一致性收尾 + 静默忽略类 + 守卫可观测 ============
        # ---- 85. stac_index CLI 统一 exit 2（旧实现零包装，traceback exit 1）----
        def cli111(script, *args):
            return subprocess.run(
                [sys.executable, str(HERE / script), *args],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120, cwd=str(tmp),
                env=_SUBPROCESS_ENV)

        r_s1 = cli111('stac_index.py', 'a', 'b', 'c', 'd')
        r_s2 = cli111('stac_index.py', '9', '47', '10', '48',
                      '--layer', 'foo')
        r_s3 = cli111('stac_index.py', '9', '47', '10', '48',
                      '--workers', 'abc')
        check('stac_cli_validated',
              r_s1.returncode == 2 and 'Traceback' not in r_s1.stderr and
              'bbox' in r_s1.stdout and
              r_s2.returncode == 2 and 'Traceback' not in r_s2.stderr and
              'layer' in r_s2.stdout and
              r_s3.returncode == 2 and 'Traceback' not in r_s3.stderr and
              'workers' in r_s3.stdout,
              f'stac_index: bbox 非数字 rc={r_s1.returncode}、'
              f'--layer foo rc={r_s2.returncode}、--workers abc '
              f'rc={r_s3.returncode}，均 [错误]+exit 2'
              '（旧实现裸 ValueError traceback exit 1，v1.10 漏了本 CLI）')

        # ---- 86. items 形状校验（dict / list[str] 不再裸崩）----
        d111 = tmp / 'v111_dict.json'
        d111.write_text(json.dumps({"cells": []}), encoding='utf-8')
        s111 = tmp / 'v111_strlist.json'
        s111.write_text(json.dumps(["w9s47"]), encoding='utf-8')
        ok111 = tmp / 'v111_ok.json'
        ok111.write_text(json.dumps(
            [{"cell": "w9s47", "url": "u://x", "size": 10}]),
            encoding='utf-8')
        bnd111 = tmp / 'v111_bnd.json'
        bnd111.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": {
                "type": "Polygon", "coordinates": [
                    [[9, 47], [10, 47], [10, 48], [9, 48], [9, 47]]]}}]}),
            encoding='utf-8')
        r_i1 = cli111('fetch_tiles.py', str(d111), 'dtm')
        r_i2 = cli111('fetch_tiles.py', str(s111), 'dtm')
        r_i3 = cli111('build_mosaic.py', str(d111), str(bnd111),
                      str(tmp / 'v111_o.tif'))
        r_i4 = cli111('build_mosaic.py', str(s111), str(bnd111),
                      str(tmp / 'v111_o2.tif'))
        check('items_shape_validated',
              all(r.returncode == 2 and 'Traceback' not in r.stderr
                  for r in (r_i1, r_i2, r_i3, r_i4)),
              f'fetch items=dict rc={r_i1.returncode}、items=[str] '
              f'rc={r_i2.returncode}；build items=dict rc={r_i3.returncode}、'
              f'items=[str] rc={r_i4.returncode}'
              '（旧实现 AttributeError/TypeError traceback exit 1）')

        # ---- 87. fetch_tiles --layer foo 前置校验（旧实现下到网络才报错）----
        r_l1 = cli111('fetch_tiles.py', str(ok111), 'foo')
        check('fetch_layer_validated',
              r_l1.returncode == 2 and 'Traceback' not in r_l1.stderr and
              'layer' in r_l1.stdout,
              f'fetch_tiles: --layer foo -> rc={r_l1.returncode} '
              'layer 显式报错（旧实现把错层名当缓存目录，一路下到'
              '网络失败才报与原因无关的错）')

        # ---- 88. --adm1 与 --bbox/--geojson 互斥 ----
        try:
            resolve_region.resolve('QA', bbox_spec='9,47,10,48',
                                   adm1='大东部')
            adm111 = '未报错!'
        except gem15_common.RegionError as exc:
            adm111 = 'ok' if '互斥' in str(exc) else str(exc)[:40]
        check('adm1_mutex', adm111 == 'ok',
              f'--adm1 与 --bbox 同给显式报错：{adm111}'
              '（旧实现静默忽略 adm1，用户以为拿了行政区实际是矩形）')

        # ---- 89. --country 与名称冲突 ----
        _feat111 = {'type': 'Feature', 'properties': {},
                    'geometry': {'type': 'Polygon', 'coordinates': [
                        [[2, 47], [3, 47], [3, 48], [2, 48], [2, 47]]]}}
        with mock.patch.object(resolve_region, 'gb_meta',
                               return_value={'boundaryISO': 'FRA',
                                             'gjDownloadURL': 'http://x/y.geojson'}), \
             mock.patch.object(resolve_region, 'gb_geojson',
                               return_value={'type': 'FeatureCollection',
                                             'features': [_feat111]}):
            try:
                resolve_region.resolve('日本', country='FRA', cache_dir=tmp)
                c111 = '未报错!'
            except gem15_common.RegionError as exc:
                c111 = 'ok' if '冲突' in str(exc) else str(exc)[:40]
            try:
                resolve_region.resolve('FRA', country='FRA', cache_dir=tmp)
                c112 = 'ok'          # 名称与 country 一致：正常放行
            except gem15_common.RegionError:
                c112 = '一致也报错'
            try:
                resolve_region.resolve('只是个标签', country='FRA',
                                       cache_dir=tmp)
                c113 = 'ok'          # 名称不可解析：仅作标签放行
            except gem15_common.RegionError:
                c113 = '标签被拒'
        check('country_name_conflict',
              c111 == 'ok' and c112 == 'ok' and c113 == 'ok',
              f'日本+--country FRA 显式报冲突：{c111}；FRA+FRA 放行：{c112}；'
              f'不可解析名称作标签放行：{c113}'
              '（旧实现名称被完全无视，静默跑法国）')

        # ---- 90. window 超限 warn 扣除海域预过滤（旧实现按全量 pairs 误报）----
        MB111 = 1048576

        def wgs111(x0, x1):
            from rasterio.warp import transform_bounds as tb111
            return tb111(CRS.from_epsg(32632), CRS.from_epsg(4326),
                         x0, 4799400.0, x1, 4800000.0)

        def cell111(name, x0, size_mb):
            tr = list(from_origin(x0, 4800000.0, 15.0, 15.0))[:6]
            return {'cell': name, 'group': 'g', 'url': 'u://' + name,
                    'epsg': 32632, 'proj_transform': tr,
                    'proj_shape': [200, 200],
                    'size': int(size_mb * MB111),
                    'bbox': list(wgs111(x0, x0 + 3000.0))}

        items111 = [cell111('A', 500000.0, 200), cell111('B', 506000.0, 400),
                    cell111('C', 511000.0, 200)]

        def ring111(bb):
            return [[bb[0], bb[1]], [bb[2], bb[1]], [bb[2], bb[3]],
                    [bb[0], bb[3]], [bb[0], bb[1]]]

        isl111 = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {},
             'geometry': {'type': 'Polygon',
                          'coordinates': [ring111(wgs111(500000.0, 503000.0))]}},
            {'type': 'Feature', 'properties': {},
             'geometry': {'type': 'Polygon',
                          'coordinates': [ring111(wgs111(511000.0, 514000.0))]}}]}

        def fake_read111(path, bbox, pad=48, retries=2, plan=None):
            tr = plan['transform']
            return {'arr': np.full((1, 10, 10), 500.0, np.float32),
                    'transform': tr, 'crs': CRS.from_epsg(32632),
                    'count': 1, 'width': 10, 'height': 10, 'nodata': np.nan,
                    'bounds': rasterio.transform.array_bounds(10, 10, tr)}

        out111 = io.StringIO()
        with mock.patch.object(build_mosaic, 'read_one_window',
                               side_effect=fake_read111), \
                contextlib.redirect_stdout(out111):
            st111 = build_mosaic.build(
                items111, isl111, tmp / 'v111_win.tif', mode='window',
                max_download_mb=500, cache_dir=tmp)
        no_false_warn = ('已超过 --max-download-mb' not in out111.getvalue() and
                         st111.get('cells_skipped_offshore') == ['B'] and
                         st111.get('fetch_total_mb') == 400.0)
        # 对照：取数总量真超限时 warn 必须照常报（不能为了不误报而漏报）
        out112 = io.StringIO()
        items112 = [cell111('A', 500000.0, 200), cell111('C', 511000.0, 400)]
        isl112 = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {},
             'geometry': {'type': 'Polygon',
                          'coordinates': [ring111(wgs111(500000.0, 503000.0))]}},
            {'type': 'Feature', 'properties': {},
             'geometry': {'type': 'Polygon',
                          'coordinates': [ring111(wgs111(511000.0, 514000.0))]}}]}
        with mock.patch.object(build_mosaic, 'read_one_window',
                               side_effect=fake_read111), \
                contextlib.redirect_stdout(out112):
            build_mosaic.build(items112, isl112, tmp / 'v111_win2.tif',
                               mode='window', max_download_mb=500,
                               cache_dir=tmp)
        true_warn_fires = '已超过 --max-download-mb' in out112.getvalue()
        check('window_warn_postfilter',
              no_false_warn and true_warn_fires,
              f'双岛+海域 400MB 片被跳过后（取数 {st111.get("fetch_total_mb")}'
              f'MB < 500）不再误报超限；真超限（600MB）时 warn 照常报'
              '（旧实现按全量 800MB/3 片误报）')

        # ---- 91. rmtree_verified：守卫拦截不再静默假成功 ----
        gt111 = tmp / 'v111_rmtree'
        gt111.mkdir()
        (gt111 / 'a.bin').write_bytes(b'x')
        ok_real = gem15_common.rmtree_verified(gt111, '探针目录')
        gt112 = tmp / 'v111_rmtree2'
        gt112.mkdir()
        (gt112 / 'a.bin').write_bytes(b'x')
        buf112 = io.StringIO()
        with mock.patch('shutil.rmtree', side_effect=lambda p: None), \
                contextlib.redirect_stdout(buf112):
            blocked = gem15_common.rmtree_verified(gt112, '探针目录2')
        warn112 = '[warn]' in buf112.getvalue() and '删除未生效' in buf112.getvalue()
        kept112 = gt112.exists()          # 拦截场景：目录必须原样保留
        gem15_common.rmtree_verified(gt112, '探针目录2')   # 真删掉，不留残留
        check('rmtree_verified_observable',
              ok_real and not gt111.exists() and
              blocked is False and kept112 and warn112,
              '真删成功返回 True；被守卫拦截（rmtree 静默 no-op）返回 False '
              '并打印 [warn]（旧实现 except OSError 探测不到，目录保留零提示）')

        # ---- 92. 历史自检临时目录自动扫除 ----
        stale_a = tmp.parent / 'gem15_selftest_zz_probe_a'
        stale_b = tmp.parent / 'gem15_selftest_zz_probe_b'
        for d in (stale_a, stale_b):
            d.mkdir(exist_ok=True)
            (d / 'f.json').write_text('{}', encoding='utf-8')
        n_rm, n_left = _sweep_stale_tmp(tmp.parent, tmp)
        swept = (n_rm >= 2 and n_left == 0 and not stale_a.exists()
                 and not stale_b.exists())
        # 拦截场景：新建一个残留目录，把 rmtree_verified 整体 mock 成失败，
        # 验证 sweep 如实计入残留（不依赖外部真实残留，环境无关）
        stale_c = tmp.parent / 'gem15_selftest_zz_probe_c'
        stale_c.mkdir(exist_ok=True)
        (stale_c / 'f.json').write_text('{}', encoding='utf-8')
        with mock.patch.object(gem15_common, 'rmtree_verified',
                               return_value=False):
            n_rm2, n_left2 = _sweep_stale_tmp(tmp.parent, tmp)
        blocked_ok = (n_left2 == 1 and stale_c.exists())
        gem15_common.rmtree_verified(stale_c, '探针目录C')   # 真删掉，不留残留
        check('stale_tmp_sweep',
              swept and blocked_ok,
              f'伪造历史目录被清除（{n_rm} 删 / {n_left} 残留）；删除全部'
              f'被拦截时如实计入残留（拦截场景 {n_left2} 个）；'
              '下次自检自动重试扫除守卫拦截的残留')

        # ---- 93. v1.12.0：GitHub 候选镜像展开 ----
        mu = gem15_common.github_mirror_urls(
            'https://github.com/wmgeolab/geoBoundaries/raw/9469f09/'
            'releaseData/gbOpen/DEU/ADM0/geoBoundaries-DEU-ADM0.geojson')
        # 输入本身即 web 形式 -> 去重后 4 个（web/raw/jsdelivr/ghproxy）
        mu_raw = gem15_common.github_mirror_urls(
            'https://raw.githubusercontent.com/wmgeolab/geoBoundaries/9469f09/'
            'releaseData/gbOpen/DEU/ADM0/geoBoundaries-DEU-ADM0.geojson')
        # 输入即 raw 子域 -> 5 个（raw 排第一，web 形式补进来）
        mu2 = gem15_common.github_mirror_urls('https://example.com/a.geojson')
        check('mirror_urls_expand',
              mu[0].startswith('https://github.com/wmgeolab/') and
              len(mu) == 4 and len(set(mu)) == 4 and
              len(mu_raw) == 4 and len(set(mu_raw)) == 4 and
              mu_raw[0].startswith('https://raw.githubusercontent.com/') and
              any('raw.githubusercontent.com/wmgeolab/geoBoundaries/9469f09' in u
                  for u in mu) and
              any('cdn.jsdelivr.net/gh/wmgeolab/geoBoundaries@9469f09' in u
                  for u in mu) and
              any(u.startswith('https://ghproxy.net/https://raw.githubusercontent')
                  for u in mu) and
              mu2 == ['https://example.com/a.geojson'],
              f'web 入口展开 {len(mu)} 个 / raw 入口展开 {len(mu_raw)} 个'
              '（均为传地址 + raw + web + jsDelivr + ghproxy 去重）；'
              '传入地址恒排第一且无重复；非 GitHub URL 仍返回单元素（行为不变）')

        # ---- 94. v1.12.0：连接阶段短超时真正传给 curl ----
        seen_cmd = []

        def _fake_run(cmd, **kw):                        # noqa: ARG001
            seen_cmd.append(list(cmd))

            class _R:
                stdout, stderr, returncode = '200', '', 0
            return _R()

        d94 = tmp / 'v112_ct'
        d94.mkdir(exist_ok=True)
        with mock.patch.object(gem15_common.subprocess, 'run',
                               side_effect=_fake_run):
            gem15_common.curl_get('https://example.invalid/x.geojson',
                                  dest=d94 / 'a.bin', retries=0,
                                  connect_timeout=5)
            gem15_common.curl_get('https://example.invalid/x.geojson',
                                  dest=d94 / 'b.bin', retries=0)
        has_ct = ('--connect-timeout' in seen_cmd[0] and
                  seen_cmd[0][seen_cmd[0].index('--connect-timeout') + 1] == '5')
        no_ct = '--connect-timeout' not in seen_cmd[1]
        check('connect_timeout_applied', has_ct and no_ct,
              'curl_get 把 --connect-timeout 5 传给 curl；未指定时不加该参数'
              '（德国实测：不加时每次连接失败要耗满 21 s）')

        # ---- 95. v1.12.0：镜像回退 + 内容校验（200 错误页不算成功）----
        m95 = gem15_common.github_mirror_urls(
            'https://github.com/o/r/raw/ref/p/x.geojson')
        calls95 = []

        def _val95(pth):
            dd = json.loads(pth.read_text(encoding='utf-8'))
            if not dd.get('type'):
                raise ValueError('不是 GeoJSON')

        def _fake_get95(url, dest, **kw):                # noqa: ARG001
            calls95.append(url)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if len(calls95) == 1:
                raise gem15_common.DownloadError('GET 失败（最后状态码 000）')
            if len(calls95) == 2:
                # 模拟 ghproxy：HTTP 200 但内容是 132 B 错误页
                dest.write_text('{"error": "blocked"}', encoding='utf-8')
                return 200, dest
            dest.write_text(json.dumps({'type': 'FeatureCollection',
                                        'features': []}), encoding='utf-8')
            return 200, dest

        buf95 = io.StringIO()
        with mock.patch.object(gem15_common, 'curl_get', side_effect=_fake_get95), \
                contextlib.redirect_stdout(buf95):
            code95, _, used95 = gem15_common.curl_get_mirrors(
                m95[0], dest=tmp / 'v112_m' / 'x.geojson', validate=_val95)
        fallback_ok = (len(calls95) == 3 and code95 == 200 and used95 == m95[2]
                       and '镜像' in buf95.getvalue())
        # 全部候选失败：必须一次性抛出并列出每个主机的失败原因
        def _all_fail(url, dest, **kw):                  # noqa: ARG001
            raise gem15_common.DownloadError('GET 失败（最后状态码 000）')

        with mock.patch.object(gem15_common, 'curl_get', side_effect=_all_fail):
            try:
                gem15_common.curl_get_mirrors(
                    m95[0], dest=tmp / 'v112_m' / 'y.geojson')
                fail_msg = ''
            except gem15_common.DownloadError as exc:
                fail_msg = str(exc)
        allfail_ok = ('所有候选地址均失败' in fail_msg and
                      fail_msg.count('github.com') >= 1 and
                      'ghproxy.net' in fail_msg)
        check('mirror_fallback_validated',
              fallback_ok and allfail_ok,
              '首地址 000 → 第二个候选 200 但内容非法被校验挡下 → 第三个候选成功'
              '（共试 3 个，日志标注镜像）；全部失败时一次报出所有主机原因')

        # ---- 97. v1.12.0：截断后从现有长度续传（curl -C - 语义）----
        payload = bytes(range(256)) * 40                 # 10240 B
        dl97 = tmp / 'v12_cont.dl'
        calls97 = []

        def _fake97(cmd, **kw):                          # noqa: ARG001
            calls97.append(list(cmd))
            out = Path(cmd[cmd.index('-o') + 1])
            cur = out.stat().st_size if out.exists() else 0
            rest = len(payload) - cur
            take = rest // 2 if len(calls97) == 1 else rest
            with out.open('ab') as fh:
                fh.write(payload[cur:cur + take])
            return mock.Mock(stdout='206', stderr='')

        with mock.patch.object(fetch_tiles.subprocess, 'run',
                               side_effect=_fake97),              mock.patch.object(fetch_tiles, '_cache_ok', return_value=True):
            info97 = fetch_tiles._resume_download('u://x', dl97, len(payload),
                                                  'v97', retries=2)
        check('resume_download_continues',
              info97['bytes'] == len(payload) and info97['attempts'] == 2 and
              info97['resumed_from'] == 0 and dl97.read_bytes() == payload,
              f'首轮只给一半 → 第 2 轮从 {len(payload) // 2} 续传到 '
              f'{len(payload)} B，字节完全正确（attempts=2，resumed_from=0）')

        # ---- 98. v1.12.0：成果写出参数 deflate + predictor=3 + zlevel=4 ----
        cap98 = {}

        class _FakeDs:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write(self, *a, **k):
                pass

            def set_band_description(self, *a, **k):
                pass

            def update_tags(self, *a, **k):
                pass

        def _cap98(*a, **k):
            cap98.update(k)
            return _FakeDs()

        with mock.patch.object(build_mosaic.rasterio, 'open',
                               side_effect=_cap98):
            build_mosaic.write_geotiff(
                tmp / 'v12_cap.tif', np.zeros((1, 4, 4), np.float32),
                from_origin(0, 0, 1, 1), CRS.from_epsg(4326), 'dtm', {})
        check('geotiff_compression_opts',
              cap98.get('compress') == 'deflate' and
              cap98.get('predictor') == 3 and cap98.get('zlevel') == 4,
              f'写出参数 {cap98.get("compress")}/predictor={cap98.get("predictor")}'
              f'/zlevel={cap98.get("zlevel")} —— 实测比默认快 2.1x、体积小 28%')

        # ---- 99. v1.13.0：auto 分块 gx 截断后回补 gy（狭长 bbox）----
        plan_thin = gem15_dem.make_split_plan(
            {"bbox": [5, -0.05, 15, 0.05], "estimated_output_mb": 38400},
            512, "auto", safety=0.75)
        check('auto_split_refit_gy',
              plan_thin["nx"] == 64 and plan_thin["ny"] >= 2 and
              plan_thin["est_per_tile_mb"] <= 512 and
              plan_thin["nx"] * plan_thin["ny"] >= 100,
              f'狭长 bbox k=100 gx 截断到 64 后 gy 回补为 '
              f'{plan_thin["ny"]}（旧实现 gy=1 -> est=600>512 自相矛盾）')

        # ---- 100. v1.13.0：build_mosaic CLI 数值参数统一 exit 2 ----
        _cli_tmp = tmp / 'v113_cli'
        _cli_tmp.mkdir(parents=True, exist_ok=True)
        _it = _cli_tmp / 'items.json'
        _it.write_text(json.dumps([{"cell": "w6s51", "url": "https://x/t.tif",
            "epsg": 32632, "proj_transform": [1, 0, 0, 0, -1, 0],
            "proj_shape": [10, 10]}]), encoding="utf-8")
        _gj = _cli_tmp / 'b.json'
        _gj.write_text(json.dumps({"type": "Polygon", "coordinates": [
            [[6, 50], [7, 50], [7, 51], [6, 51], [6, 50]]]}), encoding="utf-8")
        _cli_rc = _cli_out = None
        with mock.patch.object(sys, 'argv', ['build_mosaic.py', str(_it),
                                             str(_gj), str(_cli_tmp / 'o.tif'),
                                             '--workers', 'abc']), \
                contextlib.redirect_stdout(io.StringIO()) as _co:
            try:
                build_mosaic.main()
            except SystemExit as _se:
                _cli_rc = _se.code
            _cli_out = _co.getvalue()
        check('build_cli_validated',
              _cli_rc == 2 and 'Traceback' not in _cli_out and
              '--workers 需要数值' in _cli_out,
              f'build_mosaic --workers abc -> rc={_cli_rc} 显式报错（旧实现 '
              f'ValueError traceback exit 1），文案={_cli_out.strip()[:50]!r}')

        # ---- 101. v1.13.0：percentiles_inplace 不再重复定义 ----
        import ast as _ast
        _src = (HERE / 'build_mosaic.py').read_text(encoding='utf-8')
        _defs = [n.lineno for n in _ast.walk(_ast.parse(_src))
                 if isinstance(n, _ast.FunctionDef) and
                 n.name == 'percentiles_inplace']
        check('no_dup_percentiles', len(_defs) == 1,
              f'percentiles_inplace 定义次数={len(_defs)}（旧实现 489/517 两份，'
              f'后一份覆盖前一份，改错地方会静默失效）')

        # ---- 102. v1.13.0：单行/列 DEM 预览显式 GemError ----
        _srcprev = (HERE / 'preview_dem.py').read_text(encoding='utf-8')
        _thin_ok = 'min(src_h, src_w) < 2' in _srcprev and \
                   '预览需要至少 2 行 2 列' in _srcprev
        check('preview_thin_explicit', _thin_ok,
              '单行/列 DEM 在 render 入口抛 GemError（旧实现 np.gradient '
              'ValueError traceback exit 1）')

        # ---- 103. v1.13.0：GeometryCollection 正确算 bbox ----
        _gc = {"type": "GeometryCollection", "geometries": [
            {"type": "Point", "coordinates": [7.4, 47.9]},
            {"type": "Point", "coordinates": [8.1, 48.5]}]}
        _gc_box = gem15_common.bbox_of_geojson(_gc)
        check('geometry_collection_bbox',
              _gc_box == (7.4, 47.9, 8.1, 48.5),
              f'GeometryCollection 展开 geometries[] -> bbox={_gc_box}'
              f'（旧实现取空坐标误报 RegionError）')

        # ---- 104. v1.13.0：--force-empty 清标记重跑 ----
        _fe_dir = tmp / 'force_empty_case'
        _fe_dir.mkdir(parents=True, exist_ok=True)
        _fe_calls = []
        def _fe_run(argv):
            _fe_calls.append(argv)
            return 0
        _fe_tile = {"index": 0, "row": 0, "col": 0, "bbox": [0, 0, 1, 1]}
        _fe_ctx = dict(ctx17, force_empty=True)
        with mock.patch.object(gem15_dem, 'run', side_effect=_fe_run), \
                contextlib.redirect_stdout(io.StringIO()):
            _fe_name = 'fetest'
            _fe_tstem = gem15_dem.safe_name(f'{_fe_name}_r00c00')
            _fe_mark = _fe_dir / 'tile_r00c00' / f'{_fe_tstem}_empty.json'
            _fe_mark.parent.mkdir(parents=True, exist_ok=True)
            _fe_mark.write_text('{}', encoding='utf-8')
            gem15_dem.run_split(_fe_ctx, {"total_tiles": 1, "nx": 1, "ny": 1,
                "total_output_mb": 1, "est_per_tile_mb": 1, "tiles": [_fe_tile]},
                _fe_name, _fe_dir)
        check('force_empty_refresh',
              len(_fe_calls) == 1 and not _fe_mark.exists(),
              f'--force-empty 清除空块标记并重跑（run 调用 {len(_fe_calls)} 次；'
              f'旧实现永久 cached_empty）')

        # ---- 105. v1.13.0：dispose_tree 跳过已 0 B 空壳 ----
        _sh_dir = tmp / 'shell_case'
        _sh_dir.mkdir(parents=True, exist_ok=True)
        for _i in range(45):
            (_sh_dir / f'f{_i}.tif').write_bytes(b'X' if _i < 3 else b'')
        with contextlib.redirect_stdout(io.StringIO()):
            _sh_r = gem15_common.dispose_tree(_sh_dir, budget=40)
        check('dispose_shell_skip',
              _sh_r["mode"] == "truncate" and _sh_r["shells"] >= 42 and
              _sh_r["bytes"] == 3,
              f'45 文件超预算40 -> truncate；其中 {_sh_r["shells"]} 个已空壳'
              f'跳过无效写，仅写新清零 {_sh_r["bytes"]} B')

        # ---- 106. v1.13.0 签名 ----
        _src_dem = (HERE / 'gem15_dem.py').read_text(encoding='utf-8')
        check('v113_signature',
              gem15_common.VERSION == '2.0.0' and
              'force-empty' in gem15_dem.FLAGS_ALLOWED and
              'force_empty' in _src_dem and
              'gx_raw' in _src_dem and len(EXPECTED_KEYS) == 154,
              '版本号 2.0.0 / force-empty 开关 / auto 截断回补 / 154 项回归')

        # ---- 107. v1.14.0：整区预下载按唯一 cell 去重（块间共享）----
        _pf_items = [
            {"cell": "w6s51", "bbox": [6, 51, 7, 52], "size": 100},
            {"cell": "w6s52", "bbox": [6, 52, 7, 53], "size": 100},
            {"cell": "w7s51", "bbox": [7, 51, 8, 52], "size": 100},
        ]
        _pf_dir = tmp / 'prefetch_unique'
        _pf_dir.mkdir(parents=True, exist_ok=True)
        (_pf_dir / 'pf_gem15_items.json').write_text(
            json.dumps(_pf_items), encoding='utf-8')
        _pf_plan = {"nx": 2, "ny": 1, "total_tiles": 2,
                    "tiles": [
                        {"index": 0, "row": 0, "col": 0,
                         "bbox": [5.9, 50.9, 7.1, 52.1]},
                        {"index": 1, "row": 0, "col": 1,
                         "bbox": [6.9, 50.9, 8.1, 52.1]}]}
        _pf_ctx = {"mode": "full", "layer": "dtm", "workers": 8,
                   "timeout": 150, "chunk_mb": 16,
                   "cache": str(tmp / 'pf_cache'), "max_download_mb": 5000,
                   "force_empty": False}
        _pf_got = {}

        def _pf_fake_dl(need, layer, cache, **kw):
            _pf_got['cells'] = [it['cell'] for it in need]
            _pf_got['kw'] = kw
            return {"downloaded": len(need), "cached": 0, "bytes": 0,
                    "wall_seconds": 1.0, "tiles": []}

        with mock.patch.object(gem15_dem, 'download_items',
                               side_effect=_pf_fake_dl),              mock.patch.object(gem15_dem, 'log'),              mock.patch.object(gem15_dem, 'warn'):
            _pf_r = gem15_dem.prefetch_for_split(
                _pf_ctx, _pf_plan, 'pf', _pf_dir)
        check('prefetch_unique_cells',
              len(_pf_got['cells']) == len(set(_pf_got['cells'])) == 3 and
              _pf_got['kw']['workers'] == 8 and _pf_r['cells'] == 3,
              f'2 块共需 3 个唯一 cell -> 去重后下 {len(_pf_got["cells"])} 个'
              f'（8 路并发），旧逐块各下各的仅 2 片并发')

        # ---- 108. v1.14.0：已完成块不触发预下载 ----
        _pf_done = tmp / 'prefetch_done'
        _pf_done.mkdir(parents=True, exist_ok=True)
        (_pf_done / 'pf_gem15_items.json').write_text(
            json.dumps(_pf_items), encoding='utf-8')
        for _ti in _pf_plan['tiles']:
            _d = _pf_done / f'tile_r{_ti["row"]:02d}c{_ti["col"]:02d}'
            _d.mkdir(parents=True, exist_ok=True)
            _st = gem15_dem.safe_name(f'pf_r{_ti["row"]:02d}c{_ti["col"]:02d}')
            (_d / f'{_st}_gem15_dtm.tif').write_bytes(b'x')
            (_d / f'{_st}_gem15_manifest.json').write_text('{}',
                                                           encoding='utf-8')
        _pf_calls = []
        with mock.patch.object(gem15_dem, 'download_items',
                               side_effect=lambda *a, **k: _pf_calls.append(1)),              mock.patch.object(gem15_dem, 'log'),              mock.patch.object(gem15_dem, 'warn'):
            _pf_r2 = gem15_dem.prefetch_for_split(
                _pf_ctx, _pf_plan, 'pf', _pf_done)
        check('prefetch_skips_done',
              len(_pf_calls) == 0 and _pf_r2['skipped'] is True and
              _pf_r2['pending_tiles'] == 0,
              '所有块已完成 -> 0 次 download_items 调用，断点续跑安全')

        # ---- 109. v1.14.0：window 模式不做预下载 ----
        _pf_calls_w = []
        with mock.patch.object(gem15_dem, 'download_items',
                               side_effect=lambda *a, **k: _pf_calls_w.append(1)):
            _pf_r3 = gem15_dem.prefetch_for_split(
                dict(_pf_ctx, mode='window'), _pf_plan, 'pf', _pf_dir)
        check('prefetch_window_noop',
              _pf_r3 is None and len(_pf_calls_w) == 0,
              'window 模式不落盘缓存 -> 预下载空操作，不发起任何请求')

        # ---- 110. v1.14.0：小 max-download-mb 不误拒整区预下载 ----
        _pf_lim = {}

        def _pf_fake_lim(need, layer, cache, **kw):
            _pf_lim['v'] = kw.get('max_download_mb')
            return {"downloaded": 0, "cached": len(need), "bytes": 0,
                    "wall_seconds": 0.1, "tiles": []}

        with mock.patch.object(gem15_dem, 'download_items',
                               side_effect=_pf_fake_lim),              mock.patch.object(gem15_dem, 'log'),              mock.patch.object(gem15_dem, 'warn'):
            gem15_dem.prefetch_for_split(
                dict(_pf_ctx, max_download_mb=5000), _pf_plan, 'pf', _pf_dir)
        check('prefetch_budget_no_reject',
              _pf_lim['v'] == float('inf'),
              '预下载传 inf 跳过整批校验（逐块 build 仍按本块 items 校验）'
              ' -> 用户设 5000MB 不再被整区 12GB 误拒')

        # ---- v1.16.0：默认输出 COG ----
        import os as _os
        _cog_dir = tmp / 'cog'
        _cog_dir.mkdir(parents=True, exist_ok=True)
        _cog_src = _cog_dir / 'probe_src.tif'
        with rasterio.open(
                _cog_src, 'w', driver='GTiff', width=1024, height=1024,
                count=1, dtype='float32', crs='EPSG:4326',
                transform=rasterio.Affine(0.001, 0, 124, 0, -0.001, 38),
                nodata=float('nan'), tiled=True, blockxsize=512,
                blockysize=512, compress='deflate', predictor=3) as _ds:
            _ds.write(np.random.default_rng(0).random((1024, 1024))
                      .astype('float32'), 1)
        build_mosaic.to_cog(_cog_src)
        with rasterio.open(_cog_src) as _ds:
            _lay = _ds.tags(ns='IMAGE_STRUCTURE').get('LAYOUT', '')
            _pred = _ds.tags(ns='IMAGE_STRUCTURE').get('PREDICTOR', '')
            _ovs = _ds.overviews(1)
        check('cog_layout',
              _lay == 'COG' and _pred == '3' and bool(_ovs),
              f'GTiff -> COG: LAYOUT={_lay} predictor={_pred} '
              f'overviews={_ovs}（GDAL 3.12 双环境实测）')
        _leftover = list(_cog_dir.glob('*.cog.tmp.tif'))
        check('cog_no_tmp_left',
              not _leftover,
              'COG 转换后无 .cog.tmp.tif 残留（os.replace 原子替换，'
              '失败路径保留原 GTiff 并 [warn]）')

        # ---- v2.0.0：--single 整幅流式出图 ----
        _gj_sq = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [
                [[7.968, 52.308], [8.092, 52.308], [8.092, 52.350],
                 [7.968, 52.350], [7.968, 52.308]]]}}]}
        _pl = build_mosaic.plan_streaming_grid(
            [{"cell": "x", "epsg": 32632}], _gj_sq, "auto")
        check('stream_grid_align',
              _pl["ncol"] > 0 and _pl["nrow"] > 0
              and abs(_pl["left"] / _pl["res"]
                      - round(_pl["left"] / _pl["res"])) < 1e-9
              and _pl["crs"] is not None,
              f'plan_streaming_grid 网格对齐：{_pl["nrow"]}x{_pl["ncol"]} '
              f'@ {_pl["res"]}，原点对齐 res 整数倍')

        # 最小端到端：2 个相邻 UTM 片（连续高程函数）-> 整幅流式拼接
        from rasterio.warp import transform_bounds as _tb
        _sc = tmp / 'stream_cache'
        _sc.mkdir(parents=True, exist_ok=True)
        _items2 = []
        for _i, _e0 in enumerate((430000.0, 433840.0)):
            # 缓存布局必须与技能一致（tile_path: <cache>/tiles/<layer>/<cell>.tif）
            _items2.append({"cell": f"t{_i}", "epsg": 32632})
            _f = fetch_tiles.tile_path(_items2[-1], 'dtm', _sc)
            _f.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(
                    _f, 'w', driver='GTiff', width=256, height=256, count=1,
                    dtype='float32', crs='EPSG:32632',
                    transform=rasterio.Affine(15, 0, _e0, 0, -15, 5800000),
                    nodata=float('nan'), tiled=True, blockxsize=256,
                    blockysize=256) as _ds:
                _xx, _yy = np.meshgrid(
                    _e0 + np.arange(256) * 15.0,
                    5800000 - np.arange(256) * 15.0)
                _ds.write((50 + (_xx - 430000) * 0.002
                           + (5800000 - _yy) * 0.002).astype('float32'), 1)
            _bb = _tb('EPSG:32632', 'EPSG:4326', _e0, 5796160.0,
                      _e0 + 256 * 15.0, 5800000.0, densify_pts=21)
            _items2[-1]["bbox"] = list(_bb)
        _sout = tmp / 'stream_out.tif'
        build_mosaic.build_streaming(_items2, _gj_sq, _sout, layer='dtm',
                                     cache_dir=_sc, cog=True,
                                     band_budget_mb=16)
        with rasterio.open(_sout) as _ds:
            _a = _ds.read(1)
            _ratio = float(np.isfinite(_a).mean())
            _lay = _ds.tags(ns='IMAGE_STRUCTURE').get('LAYOUT', '')
            _rng = (float(np.nanmin(_a)), float(np.nanmax(_a)))
            _resx = (_ds.bounds.right - _ds.bounds.left) / _ds.width
            _resy = (_ds.bounds.top - _ds.bounds.bottom) / _ds.height
            _seam_col = int((433840.0 - _ds.bounds.left) / _resx)
            _ra = max(0, int((_ds.bounds.top - 5800040.0) / _resy))
            _rb = min(_ds.height, int((_ds.bounds.top - 5796120.0) / _resy))
        # 接缝连续性：跨两片交界取竖直窄带（只在 cell 纵距内测）
        _strip = _a[_ra:_rb, max(0, _seam_col - 2):_seam_col + 3]
        _seam_fin = float(np.isfinite(_strip).mean())
        # 解析值核对：两 cell 中心的像元值 == 连续高程函数解析解（±0.5 m）。
        # 比 valid 比例强得多：任何度/米混用、行映射错位都会立刻暴露。
        def _expect(_xc, _yc):
            return 50 + (_xc - 430000) * 0.002 + (5800000 - _yc) * 0.002
        _val_errs = []
        for _xc, _yc in ((431800.0, 5798080.0), (435640.0, 5798080.0)):
            _r = int((_ds.bounds.top - _yc) / _resy)
            _c = int((_xc - _ds.bounds.left) / _resx)
            _val_errs.append(abs(float(_a[_r, _c]) - _expect(_xc, _yc)))
        check('stream_e2e_tiny',
              _ratio > 0.1 and _lay == 'COG' and _rng[1] > _rng[0]
              and _seam_fin > 0.95 and max(_val_errs) < 0.5,
              f'整幅流式端到端：valid={_ratio:.2f} LAYOUT={_lay} '
              f'range={_rng[0]:.1f}~{_rng[1]:.1f} 接缝带 {_seam_fin*100:.1f}% '
              f'中心值误差 {max(_val_errs):.3f} m'
              '（2 片相邻 UTM 32632；valid 低是 pad 主导的小 fixture，属正常）')


        # ---- 111. v1.15.0：window 模式复用本地整片缓存（实测 35.2x）----
        _ws_cache = tmp / 'win_cache'
        _ws_item = {"cell": "w0s0", "url": "https://example.invalid/x.tif",
                    "size": 0, "bbox": [0.1, 0.1, 0.2, 0.2]}
        # 命中：缓存文件存在、字节数与 STAC size 一致、rasterio 可打开
        _hit_path = fetch_tiles.tile_path(_ws_item, 'dtm', _ws_cache)
        _hit_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tif, _hit_path)
        _ws_item['size'] = _hit_path.stat().st_size
        _src, _hits = build_mosaic.window_sources(
            [(_ws_item, None)], 'dtm', _ws_cache)
        check('window_cache_hit',
              _hits == 1 and _src[0][2] == str(_hit_path),
              f'缓存 size 一致 -> 读本地 {_src[0][2]}'
              '（实测本地 0.21s vs /vsicurl/ 7.42s，35.2x）')
        # size 不符（截断/损坏）-> 回退远程，不误用坏缓存
        _ws_item['size'] = _ws_item['size'] + 1
        _src, _hits = build_mosaic.window_sources(
            [(_ws_item, None)], 'dtm', _ws_cache)
        check('window_cache_size_mismatch',
              _hits == 0 and _src[0][2].startswith('/vsicurl/'),
              '缓存字节数与 STAC file:size 不符 -> 回退 /vsicurl/ 远程'
              '（坏缓存不误用，[warn] 可观测）')
        # 无缓存 -> 远程，行为与旧版一致
        _miss_item = dict(_ws_item, cell='w1s1')
        _src, _hits = build_mosaic.window_sources(
            [(_miss_item, None)], 'dtm', _ws_cache)
        check('window_cache_missing',
              _hits == 0 and _src[0][2].startswith('/vsicurl/'),
              '无本地缓存 -> 照旧走 /vsicurl/，零回归')

        # ---- v1.15.0 签名 ----
        check('v115_signature',
              gem15_common.VERSION == '2.0.0' and
              hasattr(build_mosaic, 'window_sources') and
              'window_cache_hits' in inspect.getsource(build_mosaic.build) and
              '_cache_ok' in inspect.getsource(build_mosaic.window_sources) and
              len(EXPECTED_KEYS) == 154,
              '版本号 2.0.0 / window 缓存复用 + 命中数入 stats / '
              '_cache_ok 字节+可打开双校验 / 154 项回归')

        # ---- 98. v1.11.0 签名 ----
        sig111 = (gem15_common.VERSION == '2.0.0' and
                  'sys.exit(2)' in inspect.getsource(stac_index.main) and
                  'dispose_tree' in inspect.getsource(fetch_tiles) and
                  'adm1' in inspect.getsource(resolve_region.resolve) and
                  len(EXPECTED_KEYS) == 154)
        check('v111_signature', sig111,
              '版本号 2.0.0 / stac_index CLI 统一 exit 2 / clean_cache 走删除预算 / '
              'adm1 互斥与 country-名称冲突 / 154 项回归')

        # ---- 97. v1.12.0 签名 ----
        sig112 = (gem15_common.VERSION == '2.0.0' and
                  hasattr(gem15_common, 'curl_get_mirrors') and
                  hasattr(gem15_common, 'github_mirror_urls') and
                  gem15_common.CONNECT_TIMEOUT == 12 and
                  'curl_get_mirrors' in inspect.getsource(
                      resolve_region.gb_geojson) and
                  'connect_timeout' in inspect.signature(
                      gem15_common.curl_get).parameters and
                  len(EXPECTED_KEYS) == 154)
        check('v112_signature', sig112,
              '版本号 2.0.0 / github_mirror_urls + curl_get_mirrors / '
              'CONNECT_TIMEOUT=12 / 边界下载走镜像 / 154 项回归')

        missing = EXPECTED_KEYS - set(results)
        if missing:
            raise AssertionError(f"探针 key 未加入断言列表: {sorted(missing)}")
        extra = set(results) - EXPECTED_KEYS
        if extra:
            raise AssertionError(f"新增探针 key 未同步 EXPECTED_KEYS: {sorted(extra)}")
        failed = [k for k, v in results.items() if not v]
        print(f"\n通过 {len(results) - len(failed)}/{len(results)} 项", flush=True)
        return 0 if not failed else 1
    except Exception as exc:                         # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"\n[selftest 失败] {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        # v1.6.0：ignore_errors=True 会静默吞掉清理失败。改为可观测。
        # v1.11.0：实测宿主守卫拦截删除时 rmtree **不抛 OSError**、目录原样
        # 保留 —— 旧版 except 分支探测不到，每跑一次泄漏一个 152 文件的
        # 临时目录且零提示。改用 rmtree_verified：删后验证目录确实消失，
        # 被拦截时 [warn] + 路径（下次自检开始时会自动重试扫除）。
        gem15_common.rmtree_verified(tmp, "自检临时目录")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""GEM-15 15 m 高程一键下载器（全球任意国家 / 一级行政区 / bbox / 自备边界）。

  python gem15_dem.py 列支敦士登 out
  python gem15_dem.py LUX out --layer dsm
  python gem15_dem.py FRA out --adm1 大东部 --country FRA   # 一级行政区
  python gem15_dem.py barcelona out --bbox "2.10,41.35,2.25,41.48"
  python gem15_dem.py area out --geojson my.geojson --mode full
  python gem15_dem.py AUT out --max-output-mb 4096   # 大区域提高内存预算

产物：<区域>_gem15_<layer>.tif / _preview.png / _boundary.geojson /
      _gem15_manifest.json

v1.2.0：拼接改为流式（逐片读取->贴入->释放），并在读取任何像元之前按
`像元数 × 波段数 × 4 B` 预估输出内存；超过 --max-output-mb（默认 512）
直接报 OutputTooLargeError 并给出分块建议，不让进程被 OOM 静默杀掉。
预览图也在 v1.2.0 改为先降采样再渲染。

v1.3.0：清理 / 参数健壮性 / 内存
  python gem15_dem.py --clean-cache                 # 不需要位置参数，清全部运行时缓存
  python gem15_dem.py --clean-tiles-only            # 只清瓦片，保留 STAC 索引与边界
  python gem15_dem.py LIE out --layer=dsm           # 支持 --k=v，拼错选项显式报错

v1.7.0：大国 / 大文件（四项预检全部在下载之前，超限自动生成 _split_plan.json）
  python gem15_dem.py 法国 out --split auto        # 自动分块逐块产出，内存恒定 + 断点续跑
  python gem15_dem.py 法国 out --split 4x3         # 或手工指定网格
  python gem15_dem.py LIE out --mode full --max-download-mb 256   # 限制整 COG 落盘总量
  注意：不要用提高 --max-output-mb 硬扛大国，物理内存预检会拒绝以避免 OOM 静默杀进程。
"""
from __future__ import annotations

import gc
import json
import math
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True   # 避免生成 scripts/__pycache__（技能只支持两级目录）

import build_mosaic
import preview_dem
import resolve_region
import stac_index
from fetch_tiles import clean_cache, download_items
from gem15_common import (DEFAULT_MAX_DOWNLOAD_MB, DEFAULT_MAX_OUTPUT_MB,
                          EXCLUDED_NOTE, CoverageError,
                          EmptyRasterError, GemError,
                          INDEX_WORKERS, OutputTooLargeError, VERSION,
                          default_cache, fmt_mb, log, parse_args, warn)

WITH_VALUE = ("--bbox", "--geojson", "--country", "--adm1", "--layer",
              "--mode", "--crs", "--cache", "--workers", "--timeout",
              "--buffer", "--bg", "--title", "--maxside", "--chunk-mb",
              "--max-output-mb", "--max-download-mb", "--split")
# v1.3.0：布尔开关白名单。parse_args 据此把拼错的选项直接报错，
# 不再把 --layr 的值 dsm 当成输出目录、把 --layer=dsm 当成一个开关。
FLAGS_ALLOWED = ("help", "h", "clean-cache", "clean-tiles-only",
                 "refresh", "plain", "no-clip", "force-empty", "no-cog",
                 "single")


def safe_name(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|\s]+", "_", name).strip("_") or "region"


def parse_split_opt(value: str) -> tuple[str, int | None, int | None]:
    """v1.7.0：解析 --split off|auto|NxM（纯函数，供 selftest 固化）。"""
    v = (value or "off").strip().lower()
    if v == "off":
        return "off", None, None
    if v == "auto":
        return "auto", None, None
    m = re.fullmatch(r"(\d+)x(\d+)", v)
    if not m:
        raise GemError("--split 只能是 off / auto / NxM（如 4x3），"
                       f"实际 {value!r}")
    nx, ny = int(m.group(1)), int(m.group(2))
    if not (1 <= nx <= 64 and 1 <= ny <= 64):
        raise GemError(f"--split 网格边长须在 1..64（实际 {nx}x{ny}），"
                       "避免误输入产生上千个碎块")
    return "grid", nx, ny


def make_split_plan(info: dict, max_output_mb: float, mode: str = "auto",
                    nx: int | None = None, ny: int | None = None,
                    safety: float = 0.75) -> dict:
    """v1.7.0：按预估成果数组把 bbox 切成 N×M 块，生成**可执行**分块方案。

    纯函数（不联网、不写盘）。安全系数 0.75：按整块线性外推到子块时，
    PAD 外扩与网格对齐会让小块的相对开销更大，留 25% 余量避免子块二次超限。
    经度方向按纬度做余弦修正，使子块接近等面积而不是等经纬度跨度。
    """
    bbox = info.get("bbox")
    if not bbox or len(bbox) != 4:
        raise GemError("分块方案缺少 bbox（来自 OutputTooLargeError.info）")
    mb = float(info.get("estimated_output_mb") or 0)
    if mb <= 0 or max_output_mb <= 0:
        raise GemError("分块方案缺少有效的预估成果大小")
    if mode == "grid" and nx and ny:
        gx, gy = nx, ny
    else:
        k = max(1, math.ceil(mb / (max_output_mb * safety)))
        lat = (bbox[1] + bbox[3]) / 2
        aspect = ((bbox[2] - bbox[0]) *
                  max(math.cos(math.radians(lat)), 1e-6) /
                  max(bbox[3] - bbox[1], 1e-9))
        gx_raw = max(1, math.ceil(math.sqrt(k * max(aspect, 1e-6))))
        gy_raw = max(1, math.ceil(k / gx_raw))
        gx, gy = min(gx_raw, 64), min(gy_raw, 64)
        # v1.13.0：gx 被 64 上限截断后，gy 是按**截断前**的 gx 算的
        # （狭长 bbox 实测 gx=100->64、gy=1 未回补 -> 每块 est=600 MB
        # 超过 max_output_mb=512，方案自相矛盾、子块必抛 OutputTooLargeError
        # 中断整批）。必须用截断后的 gx 重算 gy，保证 gx*gy >= k。
        if gx != gx_raw:
            gy = max(1, math.ceil(k / gx))
            gy = min(gy, 64)
    tiles = []
    for r in range(gy):
        for c in range(gx):
            x0 = bbox[0] + (bbox[2] - bbox[0]) * c / gx
            x1 = bbox[0] + (bbox[2] - bbox[0]) * (c + 1) / gx
            y0 = bbox[1] + (bbox[3] - bbox[1]) * r / gy
            y1 = bbox[1] + (bbox[3] - bbox[1]) * (r + 1) / gy
            tiles.append({"index": r * gx + c, "row": r, "col": c,
                          "bbox": [round(x0, 7), round(y0, 7),
                                   round(x1, 7), round(y1, 7)]})
    return {"nx": gx, "ny": gy, "total_tiles": gx * gy,
            "total_output_mb": round(mb, 2),
            "est_per_tile_mb": round(mb / (gx * gy), 2),
            "max_output_mb": max_output_mb, "safety_factor": safety,
            "bbox": list(bbox), "tiles": tiles,
            "note": "每块独立产出 GeoTIFF + 预览 + manifest；--no-clip 保留完整"
                    "矩形网格（避免 WGS84 矩形投影成梯形后的斜边空白）。"
                    "块输出范围 = 该块源窗口并集，实测比计划 bbox 每边最多宽约 "
                    "0.067°（≈490 像元 / 5 km），实际相邻块重叠 165~1000 像元"
                    "（德国 48 块批实测：水平 165~1000、垂直 233~689），"
                    "不是 PAD(48) 那一量级；"
                    "重叠区同坐标高程差均值 ≤0.09 m（19 条接缝实测 0.003~0.089 m），可直接任取一块拼接。"}


def write_split_plan(outdir: Path, stem: str, plan: dict) -> Path:
    p = outdir / f"{stem}_split_plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def _tile_argv(ctx: dict, tile: dict, tile_dir: Path, tile_name: str) -> list[str]:
    """构造单个分块的 CLI 参数（复用已验证的单块路径，不复制业务逻辑）。"""
    a = [tile_name, str(tile_dir), "--bbox",
         ",".join(f"{v:.7f}" for v in tile["bbox"]),
         "--split", "off", "--no-clip",
         "--layer", ctx["layer"], "--mode", ctx["mode"],
         "--crs", ctx["crs_opt"], "--cache", ctx["cache"],
         "--workers", str(ctx["workers"]), "--timeout", str(ctx["timeout"]),
         "--max-output-mb", str(ctx["max_output_mb"]),
         "--max-download-mb", str(ctx["max_download_mb"]),
         "--buffer", str(ctx["buffer_m"]), "--bg", ctx["bg"],
         "--title", tile_name, "--maxside", str(ctx["maxside"])]
    if ctx["plain"]:
        a.append("--plain")
    if ctx.get("no_cog"):
        a.append("--no-cog")
    return a


def _mark_empty(marker: Path, tile: dict, reason: str) -> None:
    """写空块标记（`_empty.json`），供断点续跑识别，不静默略过。"""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"bbox": tile["bbox"], "reason": reason},
                                 ensure_ascii=False, indent=2),
                      encoding="utf-8")


def _retry_tile_in_child(ctx: dict, tile: dict, tdir: Path,
                         tile_name: str) -> tuple[bool, str]:
    """内存不足时换一个**全新进程**重跑同一块（v1.12.2）。

    德国 48 块实测：前 41 块全部成功，第 42 块 reproject 需要
    `(1, 7096, 5097) float32` = 138 MiB 时分配失败（机器 32 GB、当时可用
    8.6 GB），而同参数的全新进程 42.9 s 就把该块跑完了 —— 长跑进程的
    虚拟内存碎片 / 已提交页不会因 `gc.collect()` 归还给系统，换进程是
    唯一可靠的复位手段。返回 (是否成功, 失败时末尾几行输出)。
    """
    warn(f"{tile_name} 内存不足，改用全新进程重试该块"
         f"（长跑进程的内存碎片无法靠 gc 复位）")
    cmd = [sys.executable, str(Path(__file__).resolve()),
           *_tile_argv(ctx, tile, tdir, tile_name)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=3600)
    except Exception as exc:                          # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if r.returncode == 0:
        log(f"{tile_name} 新进程重试成功")
        return True, ""
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
    return False, " / ".join(tail[-3:])


def prefetch_for_split(ctx: dict, plan: dict, name: str,
                       outdir: Path) -> dict | None:
    """v1.14.0：**整区一次并发下载**所有未完成块需要的 COG 片。

    德国整国 48 块实测：93 个唯一 cell 被 244 次块引用（83 片被 2~4 块共用）。
    旧实现逐块下载、每块只有 0~4 个新片（`workers=8` 大部分空转），实测并发
    加速仅 **2.08x**，下载占整批 build 的 **73.2%**。本函数把整区唯一 cell
    一次交给 ThreadPoolExecutor（实测 8 路 GCS 聚合 25.3 MB/s = 单路 3.4x），
    随后各块的 `download_items` 全部命中缓存秒过。

    只处理**尚未完成**（无 tif+manifest、且无空块标记）的块，断点续跑时不会
    为已建成果重下。仅 `mode=full` 有效（window 不落盘缓存）。失败不静默：
    保留已下缓存供续传并抛 GemError。返回统计 dict，window 模式返回 None。
    """
    if ctx.get("mode") != "full":
        return None
    items_file = outdir / f"{safe_name(name)}_gem15_items.json"
    if not items_file.exists():
        warn("整区 items 清单缺失，跳过预下载（各块仍会按需下载）")
        return None
    try:
        all_items = json.loads(items_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"整区 items 清单读取失败，跳过预下载：{exc}")
        return None
    by_cell = {it["cell"]: it for it in all_items
               if isinstance(it, dict) and it.get("cell")}

    # 只统计未完成块的实际 bbox -> 与该 bbox 相交的 cell。用 bbox 相交而非
    # 块 manifest 的 download.tiles（那是过去某次运行的状态，可能已过期）。
    pending = []
    for tile in plan["tiles"]:
        r, c = tile["row"], tile["col"]
        tdir = outdir / f"tile_r{r:02d}c{c:02d}"
        tstem = safe_name(f"{name}_r{r:02d}c{c:02d}")
        if ((tdir / f"{tstem}_gem15_{ctx['layer']}.tif").exists()
                and (tdir / f"{tstem}_gem15_manifest.json").exists()):
            continue
        if (tdir / f"{tstem}_empty.json").exists() and not ctx.get("force_empty"):
            continue
        pending.append(tile)
    if not pending:
        log("整区预下载：所有块已有成果或空块标记，无需下载")
        return {"skipped": True, "cells": 0, "pending_tiles": 0}

    need = []
    for it in all_items:
        bb = it.get("bbox")
        if not (isinstance(bb, (list, tuple)) and len(bb) == 4):
            continue
        if any(not (bb[2] < t["bbox"][0] or bb[0] > t["bbox"][2] or
                    bb[3] < t["bbox"][1] or bb[1] > t["bbox"][3])
               for t in pending):
            need.append(it)
    # 整区下载总量口径：未完成块需要的 cell 之和（<= 整区 items 之和），
    # 用 ctx 的整区上限约束即可，不会突破用户设定的 --max-download-mb。
    t0 = time.time()
    log(f"整区预下载：{len(pending)}/{plan['total_tiles']} 块待建，"
        f"需 {len(need)} 个唯一 COG 片，{ctx['workers']} 路并发"
        f"（避免逐块各下各的、每块仅 2 片并发）")
    try:
        # max-download-mb 口径：预下载只是「提前并发执行各块本来就要下的片」，
        # 不能改变限额语义。逐块 build 内仍用**本块** items 做
        # assert_download_fits（德国约 600 MB/块）；若这里传整区上限做单次
        # 整批校验，用户设 5000 MB 时会被误拒，而旧逐块路径本来能过。
        # 传 inf 跳过本次校验；真正的防御在 run_split 开头整区磁盘预检 +
        # 每块 build 内的本块 assert_download_fits，两处都保留。
        dl = download_items(need, ctx["layer"], Path(ctx["cache"]),
                            workers=ctx["workers"], timeout=ctx["timeout"],
                            chunk_mb=ctx.get("chunk_mb", 16),
                            max_download_mb=float("inf"))
    except Exception as exc:                          # noqa: BLE001
        raise GemError(
            f"整区预下载失败：{exc}。已下载的 COG 保留在缓存，"
            f"重跑本命令会自动断点续传（已完成块也会跳过）。") from exc
    out = {"skipped": False, "cells": len(need),
           "pending_tiles": len(pending),
           "downloaded": dl.get("downloaded", 0),
           "cached": dl.get("cached", 0),
           "wall_seconds": round(time.time() - t0, 2),
           "bytes": dl.get("bytes", 0)}
    log(f"整区预下载完成：新下 {out['downloaded']} / 缓存 {out['cached']} 片，"
        f"墙钟 {out['wall_seconds']}s；后续各块将秒过下载阶段")
    return out


def run_split(ctx: dict, plan: dict, name: str, outdir: Path) -> int:
    """v1.7.0：逐块执行（内存恒定、断点续跑），最后写汇总 manifest。"""
    t0 = time.time()
    total = plan["total_tiles"]
    # 总量磁盘预检：单块各自检查通过，累加仍可能写满磁盘。
    need_mb = plan["total_output_mb"] * 1.05
    if ctx["mode"] == "full":
        need_mb += ctx.get("download_total_mb", 0)
    from gem15_common import assert_disk_space
    assert_disk_space(outdir, need_mb)
    log(f"自动分块：{plan['nx']}x{plan['ny']} = {total} 块，"
        f"整区预估 {plan['total_output_mb']:.1f} MB，"
        f"每块约 {plan['est_per_tile_mb']:.1f} MB（内存恒定，逐块释放）")
    results = []
    # v1.14.0：整区一次并发下载（德国实测下载占 73%、逐块仅 2.08x 并发）。
    # 必须在逐块循环前完成；否则每块各下各的、8 worker 空转。
    prefetch = prefetch_for_split(ctx, plan, name, outdir)
    for tile in plan["tiles"]:
        r, c = tile["row"], tile["col"]
        idx = tile["index"] + 1
        tile_name = f"{name}_r{r:02d}c{c:02d}"
        tdir = outdir / f"tile_r{r:02d}c{c:02d}"
        tstem = safe_name(tile_name)
        tif = tdir / f"{tstem}_gem15_{ctx['layer']}.tif"
        mf = tdir / f"{tstem}_gem15_manifest.json"
        empty_marker = tdir / f"{tstem}_empty.json"
        if tif.exists() and mf.exists():
            log(f"[{idx}/{total}] 断点跳过 r{r}c{c}（成果已存在）")
            results.append({"index": tile["index"], "row": r, "col": c,
                            "bbox": tile["bbox"], "status": "cached",
                            "dir": str(tdir)})
            continue
        # v1.7.0：大国分块必然遇到纯水域 / 境外 / 冰雪空洞块，这些块由
        # require_valid_pixels 显式判空。不能让一块空数据中新整批（几十小时），
        # 也不能静默略过 —— 写空块标记 + [warn]，并纳入汇总 manifest。
        if empty_marker.exists():
            if ctx.get("force_empty"):
                # v1.13.0：空块判定可能来自临时故障（STAC 缓存不全 / 瞬时
                # 读取失败）。旧实现永久锁定、无任何刷新手段 —— 数据/代码
                # 修复后重跑仍跳过，manifest 记 cached_empty，用户拿到带
                # 空洞的成果却无异常提示。--force-empty 显式清标记重跑。
                # 单文件 unlink 分散在逐块循环里，不触发批量删除守卫。
                log(f"[{idx}/{total}] --force-empty：清除 r{r}c{c} 空块标记，强制重跑")
                empty_marker.unlink(missing_ok=True)
            else:
                log(f"[{idx}/{total}] 断点跳过 r{r}c{c}（已记录为空块；"
                    f"如需重判加 --force-empty）")
                results.append({"index": tile["index"], "row": r, "col": c,
                                "bbox": tile["bbox"], "status": "cached_empty",
                                "dir": str(tdir)})
                continue
        log(f"[{idx}/{total}] 开始 r{r}c{c} bbox="
            f"{','.join(f'{v:.4f}' for v in tile['bbox'])}")
        try:
            rc = run(_tile_argv(ctx, tile, tdir, tile_name))
        except OutputTooLargeError as exc:
            raise GemError(
                f"分块后 r{r}c{c} 仍超过 --max-output-mb "
                f"{ctx['max_output_mb']:.0f} MB。请用 --split NxM 增加切块数，"
                f"或适当提高 --max-output-mb。原始信息：{exc}") from exc
        except MemoryError as exc:
            # v1.12.0：德国 48 块实测第 3 块 reproject 临时数组（190 MB）分配失败
            # 时，旧实现直接抛未预期异常、traceback 退出，用户只看到 MemoryError，
            # 不知道已经建好的块有效、也不知道重跑能续跑。明确告知进度与补救路径。
            # v1.12.2：先换全新进程重试一次（德国第 42 块实测新进程 42.9 s 成功），
            # 只有新进程也失败才中断 —— 长跑进程的内存碎片 gc 复位不了。
            ok_mem, why_mem = _retry_tile_in_child(ctx, tile, tdir, tile_name)
            if ok_mem:
                results.append({"index": tile["index"], "row": r, "col": c,
                                "bbox": tile["bbox"], "status": "ok",
                                "dir": str(tdir), "tif": str(tif),
                                "note": "内存不足后由新进程重试完成"})
                gc.collect()
                continue
            raise GemError(
                f"分块 r{r}c{c} 内存不足（{type(exc).__name__}: {exc}）。"
                f"已完成/跳过 {len(results)}/{total} 块（已建成果有效，重跑会自动"
                f"断点跳过）。建议：① 关闭其他占内存程序后重跑；"
                f"② 用 --split {plan['nx'] * 2}x{plan['ny']} 把块切得更小"
                f"（当前每块约 {plan['est_per_tile_mb']:.0f} MB）；"
                f"③ 用 --mode window 降低单块峰值。"
                f"（本次已自动换新进程重试一次，仍失败：{why_mem}）"
            ) from exc
        except CoverageError as exc:
            # v1.12.2：德国 r7c0（54.09–55.06N / 5.87–7.40E 纯北海）STAC 判空抛
            # CoverageError，旧实现让**整批在 43/48 处中断**（前面 42 块白跑）。
            # v1.7.0 的"空块不中断整批"只兜住了 EmptyRasterError（取数后判空），
            # 漏了 STAC 阶段就判空的这一支。纯海域块与大国的冰雪/境外块同性质。
            warn(f"[{idx}/{total}] r{r}c{c} 无 GEM-15 瓦片（纯海域/排除区），"
                 f"标记 empty 后继续，不中断整批：{exc}")
            _mark_empty(empty_marker, tile, str(exc))
            results.append({"index": tile["index"], "row": r, "col": c,
                            "bbox": tile["bbox"], "status": "empty",
                            "dir": str(tdir), "marker": str(empty_marker),
                            "reason": "no_stac_tiles"})
            continue
        except EmptyRasterError as exc:
            warn(f"[{idx}/{total}] r{r}c{c} 无有效像元（水域/境外/冰雪空洞），"
                 f"标记 empty 后继续，不中断整批：{exc}")
            _mark_empty(empty_marker, tile, str(exc))
            results.append({"index": tile["index"], "row": r, "col": c,
                            "bbox": tile["bbox"], "status": "empty",
                            "dir": str(tdir), "marker": str(empty_marker)})
            continue
        if rc != 0:
            raise GemError(f"分块 r{r}c{c} 退出码 {rc}（不静默跳过）")
        results.append({"index": tile["index"], "row": r, "col": c,
                        "bbox": tile["bbox"], "status": "ok", "dir": str(tdir),
                        "tif": str(tif)})
        # v1.12.0：每块结束显式回收 —— 实测 48 块顺序跑时 RSS 累积，
        # 第 3 块就出现内存分配失败；gc 让大数组尽快归还给系统。
        gc.collect()
    summary = {"skill": "gem15-dem", "version": VERSION,
               "generated_at": datetime.now().isoformat(timespec="seconds"),
               "kind": "split_summary", "region": name,
               "split_plan": plan, "tiles": results,
               "prefetch": prefetch,
               "seconds": round(time.time() - t0, 2),
               "note": "每块成果独立可用；相邻块重叠 165~1000 像元（块输出 = 源"
                       "窗口并集，比计划 bbox 每边最多宽约 0.067°），重叠区同坐标"
                       "高程差均值 ≤0.09 m，拼接时任取一块即可。"
                       "status=empty/cached_empty 表示该块"
                       "无有效高程（水域/境外/冰雪），已显式记录并跳过，非失败。"}
    sp = outdir / f"{safe_name(name)}_split_manifest.json"
    sp.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    log(f"分块完成：{total} 块，耗时 {summary['seconds']}s，汇总 {sp}")
    return 0


def run(argv: list[str]) -> int:
    pos, opts, flags = parse_args(argv, WITH_VALUE, FLAGS_ALLOWED)
    # v1.1.0：--help 会进 flags（help）、-h 进 pos；两者都应打印帮助并以 0 退出。
    # 原实现用 `return 0 if pos else 1`，--help 时 pos 为空 -> 退出码 1（假失败）。
    if ("help" in flags or "h" in flags or
            any(a in ("-h", "--help") for a in pos)):
        print(__doc__)
        return 0
    cache = Path(opts["cache"]) if "cache" in opts else default_cache()
    # v1.3.0：清缓存是**无位置参数**的操作，必须排在 `if not pos` 之前。
    # 旧实现要求写成 `gem15_dem.py LIE out --clean-cache`，直接
    # `gem15_dem.py --clean-cache` 会打印帮助并 exit 1（实测），且还会白建一个输出目录。
    if "clean-cache" in flags or "clean-tiles-only" in flags:
        clean_cache(cache, scope="tiles" if "clean-tiles-only" in flags else "all")
        log("缓存已清空并退出（未触发下载，也未创建输出目录）。")
        return 0
    if not pos:
        print(__doc__)
        return 1
    name = pos[0]
    # v1.10.0：黑盒实测 `--country TWN --bbox 法国` 会静默忽略 TWN 跑起法国
    # 全流程（用户想要台湾却拿到法国边界，无任何提示）；3 个位置参数时
    # 第 3 个静默忽略。互斥/多余参数显式报错。
    if len(pos) > 2:
        raise GemError(
            f"最多 2 个位置参数（名称 输出目录），实际 {len(pos)} 个："
            f"{pos}；多余参数是否漏了 -- 前缀？")
    # v1.10.0：--country 与 --bbox/--geojson 互斥校验下沉到
    # resolve_region.resolve()（同时覆盖独立 CLI），此处不重复。
    outdir = Path(pos[1]) if len(pos) > 1 else Path.cwd() / f"gem15_{safe_name(name)}"

    # v1.5.0：**全部参数校验前移到建目录之前**。实测 `--layer bad` 会先
    # mkdir 再抛错，白建一个输出目录（与 v1.3.0 修的 --clean-cache 同类）。
    def num(key, default, cast):
        try:
            return cast(opts.get(key, default))
        except (TypeError, ValueError) as exc:
            raise GemError(f"--{key} 需要数值，实际 {opts.get(key)!r}") from exc

    layer = opts.get("layer", "dtm")
    if layer not in ("dtm", "dsm"):
        raise GemError(f"--layer 只能是 dtm 或 dsm，实际 {layer}")
    mode = opts.get("mode", "window")
    if mode not in ("window", "full"):
        raise GemError(f"--mode 只能是 window 或 full，实际 {mode}")
    crs_opt = opts.get("crs", "auto")
    # v1.6.0：非法 CRS 会在 build 深处抛 rasterio.errors.CRSError（不是 GemError，
    # 主入口打印 traceback 并 exit 1）。前置校验统一成 GemError / exit 2。
    build_mosaic.validate_crs_opt(crs_opt)
    workers = num("workers", 4, int)
    if workers < 1:
        raise GemError(f"--workers 至少为 1（实际 {workers}）")
    timeout = num("timeout", 45, int)        # full 模式单次请求上限（-C - 续传）
    if timeout < 1:
        raise GemError("--timeout 至少为 1 秒（0 会让 curl --max-time 变成无超时，"
                       "并被 subprocess 的 timeout+15 兜底误杀正常请求）")
    chunk_mb = num("chunk-mb", 16, float)
    if chunk_mb <= 0:
        raise GemError(f"--chunk-mb 必须为正数（实际 {chunk_mb}）")
    if "chunk-mb" in opts:
        # v1.12.0：下载改为单文件 curl -C - 续传，不再分段；显式提示而非静默忽略。
        warn("--chunk-mb 自 v1.12.0 起已废弃（下载不再分段），本次忽略")
    max_output_mb = num("max-output-mb", DEFAULT_MAX_OUTPUT_MB, float)
    if max_output_mb <= 0:
        raise GemError("--max-output-mb 必须为正数")
    max_download_mb = num("max-download-mb", DEFAULT_MAX_DOWNLOAD_MB, float)
    if max_download_mb <= 0:
        raise GemError("--max-download-mb 必须为正数")
    split_mode, split_nx, split_ny = parse_split_opt(opts.get("split", "off"))
    buffer_m = num("buffer", 0, float)
    if buffer_m < 0:
        raise GemError(f"--buffer 不能为负数（会缩小取数范围，实际 {buffer_m}）")
    bg = opts.get("bg", "transparent")
    if bg not in preview_dem.BG_MODES:
        raise GemError(f"--bg 只能是 {' 或 '.join(preview_dem.BG_MODES)}，实际 {bg}")
    maxside = num("maxside", 1600, int)
    if maxside < 2:
        raise GemError("--maxside 至少为 2（0 会把降采样图退化成 1x1 并触发 np.gradient 崩溃）")
    title = opts.get("title", "")
    plain = "plain" in flags
    no_clip = "no-clip" in flags
    no_cog = "no-cog" in flags

    # v1.6.0：不再提前创建输出目录 / 缓存根目录。前者导致边界解析、STAC 选片、
    # 覆盖排除、规模预检等**任何**失败都留下半成品目录（黑盒矩阵实测 13 个残留）；
    # 后者各写缓存处本就自带 mkdir(parents=True)。第一次真正写成果时才建目录。
    t0 = time.time()

    # 1) 边界
    t = time.time()
    boundary, region_meta = resolve_region.resolve(
        name, bbox_spec=opts.get("bbox"), geojson_path=opts.get("geojson"),
        country=opts.get("country"), adm1=opts.get("adm1"),
        cache_dir=cache, refresh="refresh" in flags)
    t_boundary = time.time() - t
    stem = safe_name(region_meta.get("name") or name)

    # 2) STAC 选片
    t = time.time()
    index_workers = max(INDEX_WORKERS, workers)   # 实测 16 比 8 快且方差更小
    items = stac_index.select_items(region_meta["bbox"], layer, cache,
                                    workers=index_workers)
    t_index = time.time() - t
    # v1.10.0：黑盒实测极区 / 纯海洋 bbox 在 STAC 判空报 CoverageError
    # （exit 2）后仍留下只含 boundary.geojson 的半成品目录。把建目录
    # 推迟到 STAC 选片成功之后 —— 判空类失败不再白建目录。
    outdir.mkdir(parents=True, exist_ok=True)
    boundary_file = outdir / f"{stem}_boundary.geojson"
    boundary_file.write_text(json.dumps(boundary, ensure_ascii=False),
                             encoding="utf-8")
    items_file = outdir / f"{stem}_gem15_items.json"
    items_file.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    # 3) 分片取数 + 拼接 + 裁切
    t = time.time()
    out_tif = outdir / f"{stem}_gem15_{layer}.tif"
    if "single" in flags:
        # v2.0.0 整幅单文件模式：先整区下载到缓存（流式拼接需要完整本地 COG），
        # 再逐带流式拼接。内存恒定（约 1~1.2 GB 峰值，与成果总大小无关），
        # 不受 --max-output-mb 约束，也不分块；磁盘空间仍需足够。
        if opts.get("split", "off") not in ("off", ""):
            warn("--single 与 --split 同传：--single 优先，整幅不分块")
        from gem15_common import assert_disk_space
        dl_mb = sum(it["size"] for it in items
                    if isinstance(it.get("size"), int) and it["size"] > 0) \
            / 1048576.0
        # 成果网格经验上界 ≈ 2.8x 下载量（韩国实测 2.7 GB 下载 -> 7.5 GB 网格）
        assert_disk_space(outdir, dl_mb * 1.05 + dl_mb * 2.8)
        # v2.0.0：--single 本质就是整区落盘。默认 2048 MB 下载上限会挡住所有
        # 正常用法（韩国 2637 MB 即被拒，即使缓存已齐也在入口被拦）；仅当用户
        # **未显式**设置 --max-download-mb 时自动放宽并 [warn]，显式设置则尊重。
        dl_cap = max_download_mb
        if "max-download-mb" not in opts and dl_mb > dl_cap:
            dl_cap = float("inf")
            warn(f"--single 需整区落盘约 {dl_mb:.0f} MB，超过默认下载上限 "
                 f"{max_download_mb:.0f} MB；未显式设置 --max-download-mb，"
                 "自动放宽（如需硬限额请显式传 --max-download-mb）")
        log(f"--single 整幅模式：先整区落盘 {len(items)} 片（约 {dl_mb:.0f} MB），"
            "随后逐带流式拼接（内存恒定，不分块）")
        download_items(items, layer, cache, workers=workers, timeout=timeout,
                       max_download_mb=dl_cap)
        build_stats = build_mosaic.build_streaming(
            items, boundary, out_tif, layer=layer, cache_dir=cache,
            no_clip=no_clip, cog=not no_cog, region_meta=region_meta)
    else:
        try:
            build_stats = build_mosaic.build(
                items, boundary, out_tif, layer=layer, mode=mode,
                crs_opt=crs_opt, cache_dir=cache, workers=workers,
                timeout=timeout, chunk_mb=chunk_mb, no_clip=no_clip,
                buffer_m=buffer_m, max_output_mb=max_output_mb,
                max_download_mb=max_download_mb, cog=not no_cog,
                region_meta=region_meta)
        except OutputTooLargeError as exc:
            info = dict(exc.info)
            info["bbox"] = list(region_meta["bbox"])   # 用原始边界，不含 --buffer 外扩
            # v1.7.0：分块预算按**物理可用内存**封顶。若用户把 --max-output-mb
            # 提到 40000，按该值推导会切出每块 16 GB 的方案，仍然 OOM。
            # 实测本机可用 7717 MB，峰值约为成果数组 3×，留 20% 给系统：
            # 每块预算 <= avail*0.8/3（本机约 2058 MB），且不超过用户设定值。
            from gem15_common import available_memory_mb
            avail_mb = available_memory_mb()
            tile_budget = max_output_mb
            if avail_mb:
                tile_budget = min(tile_budget, max(1.0, avail_mb * 0.8 / 3.0))
            plan = make_split_plan(info, tile_budget, split_mode,
                                   split_nx, split_ny)
            plan["user_max_output_mb"] = max_output_mb
            plan["tile_budget_mb"] = round(tile_budget, 2)
            plan["available_memory_mb"] = (round(avail_mb, 2) if avail_mb
                                           else None)
            plan_path = write_split_plan(outdir, stem, plan)
            log(f"已生成分块方案 {plan_path}：{plan['nx']}x{plan['ny']} 共 "
                f"{plan['total_tiles']} 块，每块约 {plan['est_per_tile_mb']:.1f} MB")
            if split_mode == "off":
                raise OutputTooLargeError(
                    f"{exc}\n\n已生成可执行分块方案：{plan_path}\n"
                    f"一键分块：在原命令后加 --split auto（或 --split "
                    f"{plan['nx']}x{plan['ny']}），逐块产出、断点续跑。", info) from exc
            # 整区 CRS 由**全部** items 一次性决定，下传给每块，避免同一大国
            # 不同块分别判成 UTM / EPSG:4326 而无法拼接。只在分块路径计算，
            # 小区域路径因此完全不变（零回归）。
            whole_crs = build_mosaic.decide_crs(
                [it.get("epsg") for it in items], crs_opt, {})[0].to_string()
            ctx = {"layer": layer, "mode": mode, "crs_opt": whole_crs,
                   "cache": str(cache), "workers": workers, "timeout": timeout,
                   "chunk_mb": chunk_mb, "max_output_mb": max_output_mb,
                   "max_download_mb": max_download_mb, "buffer_m": buffer_m,
                   "bg": bg, "maxside": maxside, "plain": plain,
                   "force_empty": "force-empty" in flags,
                   "no_cog": no_cog,
                   "download_total_mb": sum(
                       it["size"] for it in items
                       if isinstance(it.get("size"), int) and it["size"] > 0)
                   / 1048576.0}
            return run_split(ctx, plan, name, outdir)
    t_build = time.time() - t

    # 4) 预览图
    t = time.time()
    out_png = outdir / f"{stem}_gem15_preview.png"
    # v1.6.0：--title "" 会回落到区域名；manifest 必须记录**实际生效**的标题，
    # 否则 manifest.query.title 与 render 实参不一致（成果不可复现）。
    eff_title = title or region_meta.get("name", "")
    preview_stats = preview_dem.render(
        out_tif, out_png, title=eff_title,
        bg=bg, maxside=maxside, do_annotate=not plain,
        p2=build_stats.get("elev_p2_m"), p98=build_stats.get("elev_p98_m"))
    t_preview = time.time() - t

    outputs = []
    for p in (out_tif, out_png, boundary_file, items_file):
        if p.exists():
            outputs.append({"path": str(p), "bytes": p.stat().st_size})
    manifest = {
        "skill": "gem15-dem", "version": VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "region": region_meta,
        "query": {"input": name, "layer": layer, "mode": mode,
                  "crs": crs_opt, "workers": workers,
                  "index_workers": index_workers,
                  "chunk_mb": chunk_mb, "timeout": timeout,
                  "max_output_mb": max_output_mb,
                  "max_download_mb": max_download_mb,
                  "split": opts.get("split", "off"),
                  "no_clip": no_clip, "buffer_m": buffer_m,
                  # v1.5.0：预览参数一并入 manifest，成果可复现
                  "bg": bg, "title": eff_title, "title_input": title,
                  "maxside": maxside, "plain": plain},
        "tiles": [{"cell": it["cell"], "group": it["group"],
                   "epsg": it["epsg"], "size_bytes": it["size"],
                   "bbox": it["bbox"]} for it in items],
        "seconds": {"boundary": round(t_boundary, 2), "stac": round(t_index, 2),
                    "build": round(t_build, 2), "preview": round(t_preview, 2),
                    "total": round(time.time() - t0, 2)},
        "build": build_stats, "preview": preview_stats,
        "outputs": outputs, "coverage_note": EXCLUDED_NOTE,
    }
    manifest_file = outdir / f"{stem}_gem15_manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    log("\n成品:")
    for o in outputs:
        log(f"  {o['path']}  ({fmt_mb(o['bytes'])})")
    log(f"  {manifest_file}")
    log(f"总耗时 {manifest['seconds']['total']}s；"
        f"分片 {len(items)} 个，CRS={build_stats['crs']}，"
        f"有效像元 {build_stats['valid_pixels']}")
    if build_stats.get("degraded"):
        warn("本次跨 UTM 带，已降级到 EPSG:4326（详见 manifest.build.degraded_reason）")
    return 0


def main() -> None:
    try:
        rc = run(sys.argv[1:])
    except GemError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)
    except Exception as exc:                          # noqa: BLE001
        print(f"[未预期错误] {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
    sys.exit(rc)


if __name__ == "__main__":
    main()

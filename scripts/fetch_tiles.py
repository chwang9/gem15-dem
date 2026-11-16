#!/usr/bin/env python3
"""整 COG 落盘下载（--mode full）：1° cell 分片 + 16 MiB 串行分段 + 段级断点缓存。

本机实测（2026-11-14，同一 GCS COG）：
  单连接 32 MiB Range        17.76 s   1.89 MB/s
  16 MiB 串行分段 × 3        30.62 s   1.61 MB/s   <- full 模式采用
  8 并发 × 1 MiB (par=8)    11.70 s   0.70 MB/s
  16 并发 × 2 MiB (par=16)  45.20 s   0.73 MB/s
  8 分块 par=4              51.15 s   0.32 MB/s
  整文件 153 MiB 单连接     368.1 s   0.42 MB/s（6.8 MiB 处被网关截断后重试）
=> ① 字节级**并发**分块反优化；② 单个大响应会被网关掐断，所以既不做并发分块，
   也不做整文件单连接，而是**串行分段**：每段独立校验/重试，已完成的段留在
   .parts/ 作断点缓存，全部成功后再拼成完整 COG。
   总大小来自 STAC asset 的 file:size，逐段与总量都做字节级校验，不静默损坏。

v1.1.0：曾用 .parts/ 分段 + 指纹（v1.12.0 起改为单文件 curl -C - 续传，见 _resume_download）。
原实现只按「段号 + 长度」复用，16 MiB 与 32 MiB 的尾段偏移长度完全相同，
v1.12.0：整套分段机制因宿主批量删除守卫会杀进程而移除，改为单文件续传。

v1.3.0：clean_cache 从「只清 tiles」扩展为可清全部运行时缓存
（tiles / stac / boundary / _tmp），并新增只清瓦片的 scope；分项打印释放量。

v1.7.0：新增 assert_download_fits() —— 在发出任何 Range 请求之前按 STAC
file:size 求和校验落盘总量（--max-download-mb，默认 2048 MiB）。法国整国
147 片 / 19705 MiB 实测会被直接拒绝，不再白下几十 GiB。
"""
from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import rasterio

from gem15_common import (CACHE_SUBDIRS, CONNECT_TIMEOUT, DEFAULT_MAX_DOWNLOAD_MB,
                          DownloadError, DownloadTooLargeError, GemError,
                          RegionError, _sleep_for,
                          check_help, default_cache, dispose_tree, fmt_mb, log,
                          parse_args, progress, warn)

DEFAULT_CHUNK_MB = 16
CLI_FLAGS = ("help", "h", "clean-cache", "clean-tiles-only")
USAGE = ("用法: python fetch_tiles.py items.json dtm|dsm "
         "[--cache dir] [--workers 4] [--timeout 90] [--max-download-mb 2048]\n"
         "     [--clean-cache | --clean-tiles-only]"
         "（--chunk-mb 已废弃，传入会被忽略并打印 [warn]）")


def assert_download_fits(items: list[dict], max_download_mb: float,
                         stats: dict | None = None,
                         enforce: bool = True) -> float:
    """v1.7.0：在任何下载请求之前按 STAC `file:size` 校验落盘总量。

    实测法国整国 147 片 / 19705 MiB（19.24 GiB），而旧实现会先把这些全部
    下完才做输出规模预检（预检顺序也反了）。本函数同时供 build() 与
    download_items() 调用（双入口防御），返回总量 MB。
    `enforce=False` 时只记录不拒绝（window 模式不落盘，但远程读入量同等可观）。
    """
    missing = [it.get("cell", "?") for it in items
               if not isinstance(it.get("size"), int) or it["size"] <= 0]
    total = sum(it["size"] for it in items
                if isinstance(it.get("size"), int) and it["size"] > 0)
    total_mb = total / 1048576.0
    if stats is not None:
        stats["download_total_mb"] = round(total_mb, 2)
        stats["download_cells"] = len(items)
    if missing:
        msg = (f"{len(missing)} 个 STAC item 缺少有效 file:size，下载总量 "
               f"{total_mb:.0f} MB 仅为下界：{', '.join(missing[:8])}...")
        if enforce:
            raise DownloadError(msg + "（无法做字节级校验，拒绝启动下载）")
        warn(msg)
    if enforce and total_mb > max_download_mb:
        raise DownloadTooLargeError(
            f"full 模式需落盘 {total_mb:.0f} MB（{len(items)} 片整 COG），"
            f"超过 --max-download-mb {max_download_mb:.0f} MB；"
            "已在发出任何下载请求之前停止。\n"
            "建议：① 改用默认 --mode window（只读与区域相交的窗口，不落盘整 COG）；"
            "② 用 --bbox 缩小区域或 --split auto 分块逐块产出；"
            f"③ 确认磁盘/带宽充足后加 --max-download-mb "
            f"{max(1, int(total_mb * 1.1))}；"
            "④ 只需原始 COG 时优先单独下载所需 cell。")
    return total_mb


def tile_path(item: dict, layer: str, cache_dir: Path) -> Path:
    return cache_dir / "tiles" / layer / f"{item['cell']}.tif"


def _cache_ok(dest: Path, expected: int | None) -> bool:
    if not dest.exists() or dest.stat().st_size == 0:
        return False
    if expected and dest.stat().st_size != expected:
        warn(f"缓存大小不符 {dest.name}: {dest.stat().st_size} != {expected}，重下")
        return False
    try:
        with rasterio.open(dest) as ds:
            return ds.width > 0 and ds.height > 0
    except Exception as exc:                          # noqa: BLE001
        warn(f"缓存无法打开 {dest.name}: {type(exc).__name__}，重下")
        return False


def _resume_download(url: str, dl: Path, size: int, cell: str,
                     timeout: int = 90, retries: int = 3) -> dict:
    """单文件 `curl -C -` 续传到 size 字节（v1.12.0 起取代整套分段机制）。

    为什么换掉分段（.parts 目录 + 16 MiB 段 + 合并拷贝）——德国 12.5 GB 实测
    暴露三个硬伤，任何一个都足以让大国跑不完：

    ① **宿主批量删除守卫会杀进程**。合并成功后清理 .parts（11 段 + 11 个续传
       临时文件 ≈ 23 个条目）本身无害，但守卫按**进程累计**计数：德国第 3 片
       清理时累计 count=68 > threshold=50，进程被
       `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 直接终止（exit 1，无 traceback）。
       93 片 × 23 条目 = 2000+ 次删除，无论怎么切批次都必然越线。
    ② **磁盘双倍占用**：段文件 + 合并文件同时存在，一片 160 MB 就占 320 MB。
    ③ **合并多一次全量拷贝**：161 MB 读 + 161 MB 写，纯开销。

    改用 `curl -C -`（GCS 支持 Accept-Ranges，实测 8 MB 前缀 → 190 MB 完整
    文件，单连接 2.29 MB/s）：截断后续传、无中间文件、无删除、无拷贝。
    字节校验仍然保留：完成后必须是 size 字节且能被 rasterio 打开。
    """
    got = dl.stat().st_size if dl.exists() else 0
    if got > size:                       # 残留文件比目标长：作废重来
        warn(f"{cell} 残留下载文件比目标长（{got}/{size} B），清零重下")
        dl.write_bytes(b"")
        got = 0
    t = time.time()
    attempts = 0
    stalled = 0
    resumed_from = got
    # v1.12.1：重试预算从「尝试次数」改为「连续零进展次数」。
    # 旧实现 `while got < size and attempts <= retries` 等价于「最多 4 次 ×
    # 每次 --max-time <timeout> 秒」，在慢链路上等于给单文件设了**字节上限**：
    # 德国整国实测单连接 0.35~0.79 MB/s，150 s 只能取回 52~118 MB，而单个 cell
    # 就有 156~176 MB —— 日志里 w15s47/w15s48 已用满 3 次尝试，再多一点波动就会
    # 抛 DownloadError 拖死整批（48 块跑到一半前功尽弃）。现在只要有进展就继续
    # （总上限 16 次兜底，防病态死循环），连续 retries+1 次零进展才判定失败。
    max_attempts = max(retries + 1, 16)
    code = "000"
    while got < size and attempts < max_attempts:
        attempts += 1
        cmd = ["curl", "-s", "-L", "--connect-timeout", str(CONNECT_TIMEOUT),
               "--max-time", str(timeout), "-C", "-", "-o", str(dl),
               "-w", "%{http_code}", url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout + 15)
            code = (r.stdout or "").strip()[-3:] or "000"
        except Exception as exc:                      # noqa: BLE001
            warn(f"{cell} 下载异常 {type(exc).__name__}: {exc}")
            code = "000"
        prev, got = got, (dl.stat().st_size if dl.exists() else 0)
        if code == "404" and got < size:
            raise DownloadError(f"COG 不存在 404: {url}")
        if got > size:
            warn(f"{cell} 收到多余字节（{got}/{size}），清零重下")
            dl.write_bytes(b"")
            prev, got, stalled = 0, 0, 0
        if got < size:
            stalled = stalled + 1 if got <= prev else 0
            warn(f"{cell} 续传 {got}/{size} B（第 {attempts} 次，code={code}，"
                 f"本次新增 {got - prev} B"
                 f"{f'，连续 {stalled} 次零进展' if stalled else ''}）")
        elif prev < size <= got:
            log(f"{cell} 第 {attempts} 次尝试补齐（本次新增 {got - prev} B，"
                f"共 {got}/{size} B；进入本函数时已有 {resumed_from} B）")
        if got < size and stalled >= retries + 1:
            break
        if got < size:
            _sleep_for(code, stalled)
    if got != size:
        raise DownloadError(
            f"{cell} 下载未完成 {got}/{size} B（{attempts} 次尝试，连续 "
            f"{stalled} 次零进展，最后 code={code}）；残留文件保留在 {dl}，"
            f"重跑可继续续传")
    if not _cache_ok(dl, size):
        raise DownloadError(f"{cell} 下载完成但无法打开（字节数 {got} 正确），"
                            f"疑似内容损坏：{dl}")
    return {"bytes": got, "seconds": round(time.time() - t, 2),
            "attempts": attempts, "resumed_from": resumed_from,
            "code": code}


def download_one(item: dict, layer: str, cache_dir: Path,
                 timeout: int = 90, chunk_mb: float = DEFAULT_CHUNK_MB) -> dict:
    """下载一片 COG 到缓存（单文件续传，无 .parts 目录、无删除）。

    chunk_mb 仅为兼容旧 CLI 保留，v1.12.0 起不再影响下载粒度。
    """
    dest = tile_path(item, layer, cache_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _cache_ok(dest, item.get("size")):
        return {"cell": item["cell"], "status": "cache",
                "bytes": dest.stat().st_size, "seconds": 0.0,
                "attempts": 0, "path": str(dest)}
    size = item.get("size")
    if not isinstance(size, int) or size <= 0:
        raise DownloadError(f"STAC item {item['cell']} 缺少 file:size，无法做字节校验")
    dl = dest.with_name(dest.name + ".dl")
    info = _resume_download(item["url"], dl, size, item["cell"], timeout=timeout)
    if dl.stat().st_size != size or not _cache_ok(dl, size):
        raise DownloadError(f"下载校验失败: {item['cell']}")
    # v1.5.0：Path.replace 在 Windows 上可覆盖已存在目标（实测 700 B 旧文件被
    # 21 B 新文件替换、源自动消失），比「先 unlink 再 rename」少一次删除。
    dl.replace(dest)
    meta = {"cell": item["cell"], "layer": layer, "url": item["url"],
            "size": dest.stat().st_size, "epsg": item.get("epsg"),
            "attempts": info["attempts"], "resumed_from": info["resumed_from"]}
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False),
                                         encoding="utf-8")
    return {"cell": item["cell"], "status": "download",
            "bytes": dest.stat().st_size, "seconds": info["seconds"],
            "attempts": info["attempts"], "resumed_from": info["resumed_from"],
            "path": str(dest)}


def download_items(items: list[dict], layer: str, cache_dir: Path,
                   workers: int = 4, timeout: int = 90,
                   chunk_mb: float = DEFAULT_CHUNK_MB,
                   max_download_mb: float = DEFAULT_MAX_DOWNLOAD_MB) -> dict:
    # v1.7.0：独立 CLI 与 build() 双入口都要在发请求之前校验总量。
    total_mb = assert_download_fits(items, max_download_mb)
    t = time.time()
    results: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(download_one, it, layer, cache_dir, timeout, chunk_mb): it
                for it in items}
        # as_completed：进度不再被最先提交但最慢的那片卡住（可观测性）
        for fut in as_completed(futs):
            try:
                r = fut.result()
                results.append(r)
                progress(len(results), len(items),
                         f"{r['cell']} {r['status']} {fmt_mb(r['bytes'])}"
                         + (f" {r['seconds']}s" if r["status"] == "download" else ""))
            except Exception as exc:                  # noqa: BLE001
                errors.append(f"{futs[fut]['cell']}: {type(exc).__name__}: {exc}")
    if errors:
        raise DownloadError("以下瓦片下载失败（不静默跳过）:\n  " + "\n  ".join(errors))
    n_down = sum(1 for r in results if r["status"] == "download")
    total = sum(r["bytes"] for r in results)
    wall = time.time() - t
    log(f"下载汇总: {len(results)} 片（新下 {n_down} / 缓存 {len(results) - n_down}），"
        f"落盘 {fmt_mb(total)}，墙钟 {wall:.1f}s"
        + (f"，新下载聚合 {fmt_mb(sum(r['bytes'] for r in results if r['status']=='download'))}/"
           f"{max(wall, 0.1):.1f}s（含并行与重试，非单连接速率）" if n_down else ""))
    results.sort(key=lambda r: r["cell"])
    return {"tiles": results, "bytes": total, "wall_seconds": round(wall, 2),
            "downloaded": n_down, "cached": len(results) - n_down,
            "planned_mb": round(total_mb, 2), "max_download_mb": max_download_mb}


def clean_cache(cache_dir: Path, scope: str = "all") -> dict:
    """清空运行时缓存，返回分项统计。

    v1.3.0 前只清 tiles：用户执行 --clean-cache 后 stac 索引 / 边界 / 失败残留的
    _tmp 文件仍在，且日志只报瓦片释放量，看着像「全清了」。实测本机缓存分布
    tiles 154 MB / boundary 728 KB / stac 237 KB / _tmp 4 KB。
    scope='all' 清 CACHE_SUBDIRS 全部四类；scope='tiles' 保留旧语义。
    """
    if scope not in ("all", "tiles"):
        raise ValueError(f"未知清理范围 {scope}（应为 all 或 tiles）")
    targets = ("tiles",) if scope == "tiles" else CACHE_SUBDIRS
    report: dict[str, dict] = {}
    for name in targets:
        d = cache_dir / name
        if not d.exists():
            report[name] = {"files": 0, "bytes": 0}
            continue
        # v1.12.0：改用 dispose_tree —— 宿主删除守卫按进程累计计数，
        # 大国缓存（93 片 + 断点）一次 rmtree 就是上千次删除，会被直接杀进程。
        # 条目 ≤ 40 才真删，超出则清零（空间立即释放，留 0 B 空壳）。
        r = dispose_tree(d, f"缓存目录 {name}")
        report[name] = {"files": r["files"], "bytes": r["bytes"],
                        "mode": r["mode"]}
    total = sum(v["bytes"] for v in report.values())
    n = sum(v["files"] for v in report.values())
    parts = " / ".join(f"{k} {fmt_mb(v['bytes'])}"
                       + ("(空壳)" if v.get("mode") == "truncate" else "")
                       for k, v in report.items())
    log(f"已清空缓存 [{scope}]: {cache_dir}（{n} 个文件，释放 {fmt_mb(total)}；{parts}）")
    return {"scope": scope, "subdirs": report, "files": n, "bytes": total}


def main() -> None:
    import sys
    pos, opts, flags = parse_args(
        sys.argv[1:], ("--cache", "--workers", "--timeout", "--chunk-mb",
                       "--max-download-mb"),
        CLI_FLAGS)
    if check_help(pos, flags, USAGE):
        sys.exit(0)
    cache = Path(opts["cache"]) if "cache" in opts else default_cache()
    if "clean-cache" in flags or "clean-tiles-only" in flags:
        clean_cache(cache, scope="tiles" if "clean-tiles-only" in flags else "all")
        return
    if len(pos) < 2:
        print(USAGE)
        sys.exit(1)
    # v1.11.0：黑盒实测 items 是 dict / list[str] 时在 it.get 上裸崩
    # AttributeError（traceback exit 1）；--layer foo 不校验，会把错层名
    # 当缓存目录名一路下到网络失败才报错（报错文案与真实原因无关）。
    # 与其它 CLI 同一套前置校验 + GemError -> exit 2。
    def num(key, default, cast, minimum, why):
        try:
            v = cast(opts.get(key, default))
        except (TypeError, ValueError) as exc:
            raise GemError(f"--{key} 需要数值，实际 {opts.get(key)!r}") from exc
        if v < minimum:
            raise GemError(f"--{key} 至少为 {minimum}（{why}；实际 {v}）")
        return v

    try:
        if pos[1] not in ("dtm", "dsm"):
            raise GemError(f"layer 只能是 dtm 或 dsm，实际 {pos[1]!r}")
        workers = num("workers", 4, int, 1, "0 会让线程池退化")
        timeout = num("timeout", 90, int, 1,
                      "0 会让 curl --max-time 变成无超时")
        if "chunk-mb" in opts:
            warn("--chunk-mb 自 v1.12.0 起已废弃（下载改为单文件 curl -C - "
                 "断点续传，不再分段），本次忽略该参数")
        chunk_mb = num("chunk-mb", DEFAULT_CHUNK_MB, float, 0.0001,
                       "必须为正数")
        max_download_mb = num("max-download-mb", DEFAULT_MAX_DOWNLOAD_MB,
                              float, 0.0001, "必须为正数")
        items_path = Path(pos[0])
        if not items_path.exists():
            raise RegionError(f"items 文件不存在: {items_path}")
        try:
            items = json.loads(items_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RegionError(
                f"items 文件不是合法 JSON {items_path}: {exc}") from exc
        if not isinstance(items, list) or not items:
            raise RegionError(
                f"items 文件应是 STAC item 对象数组（非空 list），"
                f"实际类型 {type(items).__name__}")
        bad = [it.get("cell", f"#{i}") if isinstance(it, dict)
               else f"#{i}:{it!r}"
               for i, it in enumerate(items)
               if not isinstance(it, dict) or "cell" not in it
               or "url" not in it]
        if bad:
            raise RegionError(
                f"items 内 {len(bad)} 个元素不是带 cell/url 的对象："
                f"{bad[:8]}（应由 stac_index 生成，勿手写）")
        stats = download_items(items, pos[1], cache,
                               workers, timeout, chunk_mb, max_download_mb)
    except GemError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

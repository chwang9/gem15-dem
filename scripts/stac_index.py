#!/usr/bin/env python3
"""GEM-15 STAC 索引：bbox -> 相交的 1° COG 瓦片清单。

完全由官方 STAC 驱动（collection.json -> 10° 组 catalog.json -> 1° item.json -> assets），
不靠记忆里的实体 id 拼 URL。命名规则只用于**从 bbox 推算候选组 / 从 cell id 解析范围**，
且已用落盘的真实 STAC 样本在 selftest.py 中固化校验。

STAC 结构实测（2026-11-14）：
  gem-15/stac/catalog.json
  gem-15/stac/gem-15/collection.json          297 个 child 组 catalog
  gem-15/stac/gem-15/{p|m}{lat10}_{p|m}{lon10}/catalog.json
  .../w{lon}s{lat}/w{lon}s{lat}.json          item，bbox 为 1°×1°，assets: dsm / dtm
  负数用 m 前缀（wm35sm7 = 经度 -35°、纬度 -7°；w9s47 = 9°E、47°N）

v1.1.0：候选组不再由硬编码 LAT_LIMITS 生成，而是解析 collection 的真实 child
组名、用 10° 外接框与 bbox 做正面积相交。实测原下界 -60 会漏掉南纬 -61° 的
m70_m50 组（真实瓦片 wm45sm61）；297 个真实组名全部可解析。
collection 的 item_assets 只有波段元数据，不含 href / size / epsg，
因此逐 item 请求无法省略（已实测并放弃该优化方向）。

v1.2.0：item asset 同时含 proj:transform / proj:shape / proj:epsg / proj:bbox /
raster:bands（实测与真实 COG 逐项一致，见 CHANGELOG），select_items 现把这些
元数据一并带出，供 build_mosaic 纯离线算出窗口与统一网格 -> 流式拼接，
不再把全部分片数组常驻内存。
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

from gem15_common import (COLLECTION_URL, INDEX_WORKERS, STAC_BASE,
                          CoverageError, DownloadError, GemError,
                          bbox_intersects, check_help, get_json, log,
                          parse_args, progress, warn)

CELL_RE = re.compile(r"^w(m?\d+)s(m?\d+)$")
GROUP_RE = re.compile(r"^([pm])(\d+)_([pm])(\d+)$")
STEP = 10
# v1.1.0：原下界 -60 会漏掉南纬 -61° 的 m70_m50 组（实测 collection 确有该组）。
# 收藏夹式的硬编码边界不再作为唯一依据：select_items 改用 collection child 组反推。
LAT_LIMITS = (-70, 80)      # collection extent: -61 .. 84
LON_LIMITS = (-180, 170)

# 独立 CLI 的带值选项（v1.1.0 修复：原先 --layer / --cache 被当成位置参数/开关）
CLI_WITH_VALUE = ("--layer", "--cache", "--workers")
CLI_FLAGS = ("help", "h")
USAGE = ("用法: python stac_index.py minx miny maxx maxy "
         "[--layer dtm] [--cache dir] [--workers 16]")


def _link_id(link: dict, pattern, kind: str) -> str | None:
    """从 STAC link 解析 10° 组名 / 1° cell 名，不假设 href 是相对路径。

    v1.3.0 前取 href 的第一个路径段，实测：
      './p40_p0/catalog.json'                              -> 'p40_p0'  ✅
      'https://.../gem-15/p40_p0/catalog.json'             -> 'https:'   ❌
      '../gem-15/p40_p0/catalog.json'                      -> 'gem-15'   ❌
    后两者会在 groups_for_bbox 中被当作非法组 warn 跳过，最终误报 CoverageError。
    现优先用 STAC 自带的 title，再在 href 各段中找第一个符合命名正则的段；
    都不匹配就打印 [warn] 跳过，不静默猜 id。
    """
    title = str(link.get("title") or "")
    if pattern.fullmatch(title):
        return title
    href = (link.get("href") or "").split("?", 1)[0].split("#", 1)[0]
    for seg in href.replace("\\", "/").split("/"):
        if seg and seg not in (".", "..") and pattern.fullmatch(seg):
            return seg
    warn(f"无法从 STAC link 解析 {kind}，跳过（不静默猜 id）: {link}")
    return None


def band_token(value: float) -> str:
    """把纬度/经度折到 10° 带名：7 -> p0，-7 -> m10，-35 -> m40，103 -> p100。"""
    n = math.floor(value / STEP) * STEP
    return ("p" if n >= 0 else "m") + str(abs(int(n)))


def group_for(lat: float, lon: float) -> str:
    return f"{band_token(lat)}_{band_token(lon)}"


def _num(tok: str) -> int:
    return -int(tok[1:]) if tok.startswith("m") else int(tok)


def parse_cell(cell: str) -> tuple[float, float]:
    """'wm35sm7' -> (-35.0, -7.0)；'w9s47' -> (9.0, 47.0)。"""
    m = CELL_RE.match(cell)
    if not m:
        raise ValueError(f"无法解析 cell id: {cell}")
    return float(_num(m.group(1))), float(_num(m.group(2)))


def cell_bbox(cell: str) -> tuple[float, float, float, float]:
    lon, lat = parse_cell(cell)
    return lon, lat, lon + 1.0, lat + 1.0


def parse_group(group: str) -> tuple[float, float]:
    """'p40_p0' -> (40, 0)；'m70_m50' -> (-70, -50)。返回 (lat10, lon10)。"""
    m = GROUP_RE.match(group)
    if not m:
        raise ValueError(f"无法解析 STAC 组名: {group}")
    la = int(m.group(2)) * (-1 if m.group(1) == "m" else 1)
    lo = int(m.group(4)) * (-1 if m.group(3) == "m" else 1)
    return float(la), float(lo)


def groups_for_bbox(bbox, available: set[str] | None = None) -> list[str]:
    """覆盖 bbox 的 10° 组名。

    v1.1.0：传入 collection 的 child 集合时，**直接按真实组名的 10° 外接框反推**，
    不再靠硬编码纬度上下限生成候选。实测修复了南纬 -61°（m70_m50 组）被
    LAT_LIMITS=(-60,..) 裁掉导致的 CoverageError；297 个真实组名全部可解析。
    available 为空/None 时保留原生成逻辑（供离线用例使用）。
    """
    if available is not None:
        out = []
        for g in sorted(available):
            try:
                la, lo = parse_group(g)
            except ValueError as exc:
                warn(f"{exc}，跳过该组（不静默吞掉）")
                continue
            if bbox_intersects(bbox, (lo, la, lo + STEP, la + STEP)):
                out.append(g)
        return out
    minx, miny, maxx, maxy = bbox
    lat0 = max(LAT_LIMITS[0], math.floor(miny / STEP) * STEP)
    lat1 = min(LAT_LIMITS[1], math.floor(maxy / STEP) * STEP)
    lon0 = max(LON_LIMITS[0], math.floor(minx / STEP) * STEP)
    lon1 = min(LON_LIMITS[1], math.floor(maxx / STEP) * STEP)
    out = []
    for la in range(int(lat0), int(lat1) + 1, STEP):
        for lo in range(int(lon0), int(lon1) + 1, STEP):
            out.append(group_for(la, lo))
    return out


def load_collection(cache_dir: Path) -> dict:
    return get_json(COLLECTION_URL, cache=cache_dir / "stac" / "collection.json")


def child_groups(collection: dict) -> set[str]:
    out = set()
    for link in collection.get("links", []):
        if link.get("rel") != "child":
            continue
        g = _link_id(link, GROUP_RE, "10° 组名")
        if g:
            out.add(g)
    return out


def group_url(group: str) -> str:
    return f"{STAC_BASE}/gem-15/{group}/catalog.json"


def load_group(group: str, cache_dir: Path) -> list[str]:
    d = get_json(group_url(group), cache=cache_dir / "stac" / f"group_{group}.json")
    cells = []
    for link in d.get("links", []):
        if link.get("rel") != "item":
            continue
        c = _link_id(link, CELL_RE, "1° cell 名")
        if c:
            cells.append(c)
    if not cells and d.get("links"):
        warn(f"组 {group} 的 catalog 未解析出任何 cell（判空，不静默返回空清单）")
    return cells


def item_url(group: str, cell: str) -> str:
    return f"{STAC_BASE}/gem-15/{group}/{cell}/{cell}.json"


def load_item(group: str, cell: str, cache_dir: Path) -> dict:
    return get_json(item_url(group, cell),
                    cache=cache_dir / "stac" / f"item_{cell}.json")


def resolve_asset_href(href: str, item_url_: str) -> str:
    """v1.8.0：STAC asset href 归一化为绝对 URL（纯函数，供 selftest 固化）。

    实测 GEM-15 当前返回绝对 GCS URL（fixtures 12 个样本全部 https:// 开头），
    但 STAC 规范允许相对 href（RFC 3986），生产方一旦改版，旧实现会把
    'dtm/zall/mosaic.tif' 原样透传给 /vsicurl/ 与 curl，静默 404。
    现按 item URL 解析相对/绝对路径（'./x'、'x'、'/x' 均正确），
    已是绝对 URL 时原样返回。
    """
    if not href or "://" in href:
        return href
    return urljoin(item_url_, href)


def select_items(bbox, layer: str, cache_dir: Path,
                 workers: int = INDEX_WORKERS) -> list[dict]:
    """返回与 bbox 有正面积相交的 COG 清单，含真实 href / size / epsg。"""
    # v1.11.0：独立 CLI 实测 --layer foo 抛裸 ValueError（traceback exit 1），
    # 与 v1.10「三个独立 CLI 统一 [错误]+exit 2」不一致（当时漏了本文件）。
    if layer not in ("dtm", "dsm"):
        raise GemError(f"--layer 只能是 dtm 或 dsm，实际 {layer!r}")
    collection = load_collection(cache_dir)
    available = child_groups(collection)
    if not available:
        raise DownloadError("STAC collection 中没有 child 组，索引可能已变更")

    # 由真实 child 组反推候选，避免硬编码纬度边界漏片（v1.1.0）
    groups = groups_for_bbox(bbox, available)
    log(f"STAC: bbox={tuple(round(v, 4) for v in bbox)} 候选 10° 组 {len(groups)} 个 {groups}")
    if not groups:
        raise CoverageError(
            "bbox 未落在任何 GEM-15 STAC 组内（纬度需在 60°S–84°N；"
            "中国、俄罗斯、蒙古、格陵兰、南极洲被数据源排除）。")

    # 组 catalog 与 item JSON 都很小（5–12 KB），并发拉取；失败不静默
    cell_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(load_group, g, cache_dir): g for g in groups}
        for fut in as_completed(futs):
            g = futs[fut]
            for cell in fut.result():
                cell_map[cell] = g

    hits = [(cell, g) for cell, g in cell_map.items()
            if bbox_intersects(bbox, cell_bbox(cell))]
    hits.sort(key=lambda x: x[0])
    log(f"STAC: 组内共 {len(cell_map)} 个 1° cell，与 bbox 相交 {len(hits)} 个")
    if not hits:
        raise CoverageError(
            "该范围没有 GEM-15 1° 瓦片。可能是海洋、纬度超界，或位于"
            "数据源排除区（中国、俄罗斯、蒙古、格陵兰、南极洲）。")

    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(load_item, g, c, cache_dir): (c, g)
                for c, g in hits}
        for fut in as_completed(futs):
            cell, group = futs[fut]
            d = fut.result()
            asset = d.get("assets", {}).get(layer)
            if not asset or not asset.get("href"):
                raise DownloadError(f"STAC item {cell} 缺少 {layer} 资产")
            raw_href = asset["href"]
            url = resolve_asset_href(raw_href, item_url(group, cell))
            if url != raw_href:
                warn(f"STAC item {cell} 的 {layer} href 为相对路径，已按 item URL "
                     f"解析：{raw_href} -> {url}")
            it = {
                "cell": cell,
                "group": group,
                "url": url,
                "epsg": d.get("properties", {}).get("proj:epsg"),
                "bbox": d.get("bbox"),
                "size": asset.get("file:size"),
                "proj_bbox": asset.get("proj:bbox"),
                # v1.2.0：离线算窗口 / 统一网格所需的真实投影元数据
                "proj_transform": asset.get("proj:transform"),
                "proj_shape": asset.get("proj:shape"),
                "nodata": (asset.get("raster:bands") or [{}])[0].get("nodata"),
                "bands": [b.get("name") for b in asset.get("raster:bands", [])],
            }
            missing = [k for k in ("epsg", "proj_transform", "proj_shape")
                       if not it.get(k)]
            if missing:
                raise DownloadError(
                    f"STAC item {cell} 缺少投影元数据 {missing}，无法离线规划窗口")
            items.append(it)
            progress(len(items), len(hits),
                     f"cell={cell} EPSG:{it['epsg']} "
                     f"size={it['size'] / 1048576:.1f} MB" if it["size"] else
                     f"cell={cell} EPSG:{it['epsg']} size=?")
            # as_completed 后顺序不定，用 cell 排序保证 items.json 可复现
    items.sort(key=lambda x: x["cell"])
    if not items:
        raise CoverageError("STAC 命中 0 个可用瓦片（禁止静默产出空数据）")
    return items


def main() -> None:
    import sys
    from gem15_common import default_cache
    pos, opts, flags = parse_args(sys.argv[1:], CLI_WITH_VALUE, CLI_FLAGS)
    if check_help(pos, flags, USAGE):
        sys.exit(0)
    # v1.11.0：黑盒实测本 CLI 是四个独立 CLI 里唯一零错误包装的 ——
    # bbox 非数字 / --layer foo / --workers abc 全部裸 traceback exit 1
    # （v1.10 只统一了 fetch_tiles / preview_dem / build_mosaic 三个）。
    # 现与其它 CLI 同一套规则：显式校验 + GemError -> [错误] + exit 2。
    try:
        if len(pos) < 4:
            print(USAGE)
            sys.exit(1)
        try:
            bbox = tuple(float(v) for v in pos[:4])
        except ValueError as exc:
            raise GemError(
                f"bbox 需要 4 个数字（minx miny maxx maxy），"
                f"实际 {pos[:4]}") from exc
        layer = opts.get("layer", "dtm")
        if layer not in ("dtm", "dsm"):
            raise GemError(f"--layer 只能是 dtm 或 dsm，实际 {layer!r}")
        try:
            workers = int(opts.get("workers", INDEX_WORKERS))
        except ValueError as exc:
            raise GemError(
                f"--workers 需要整数，实际 {opts.get('workers')!r}") from exc
        if workers < 1:
            raise GemError(f"--workers 至少为 1（实际 {workers}）")
        items = select_items(bbox, layer,
                             Path(opts["cache"]) if "cache" in opts
                             else default_cache(), workers)
    except GemError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)
    for it in items:
        print(it)


if __name__ == "__main__":
    main()

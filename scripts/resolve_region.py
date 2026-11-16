#!/usr/bin/env python3
"""把「全球任意国家 / 一级行政区 / bbox / 自备 GeoJSON」解析为 WGS84 边界。

边界源：geoBoundaries gbOpen（CC0 / 各国开放许可），API 元数据 + GeoJSON 下载。
本机实测：www.geoboundaries.org 与 github.com/.../raw/... 可达（~1-3 s）；
raw.githubusercontent.com / cdn.jsdelivr.net 不可达，因此不依赖这些源。

- ADM0 国家级：用完整版 GeoJSON（一般 0.1–1 MB）。
- ADM1 一级行政区：用 simplified 版（法国 ADM1 完整版 72.7 MB，简化版 1.67 MB，
  用于 15 m 像元掩膜足够；降级是显式选路，不是静默兜底）。
- 同名 / 多处匹配会列出候选并要求精确名称，不静默选错。
"""
from __future__ import annotations

import json
from pathlib import Path

from gem15_common import (CONNECT_TIMEOUT, EXCLUDED_ISO3, EXCLUDED_NOTE, GB_API,
                          CoverageError, DownloadError, GemError, RegionError,
                          bbox_intersects, bbox_of_geojson,
                          check_help, curl_get, curl_get_mirrors, default_cache,
                          get_json, log,
                          parse_args, warn)

HERE = Path(__file__).resolve().parent
COUNTRIES = HERE / "countries_zh.json"
# 中国的台湾、香港、澳门地区常被 ISO3 直传（TWN/HKG/MAC），一律归并 CHN
CHINA_REGION_ISO = {"TWN": "台湾", "HKG": "香港", "MAC": "澳门"}
# 中国全域粗判框（含台湾、海南、港澳、钓鱼岛）；仅作精判门槛，宽于实际领土
CHINA_BBOX = (73.0, 3.5, 136.0, 54.5)
CLI_FLAGS = ("help", "h", "refresh")
USAGE = ("用法: python resolve_region.py <名称> [out.json] "
         "[--bbox minx,miny,maxx,maxy] [--geojson f.json] "
         "[--country ISO3] [--adm1 区名] [--refresh]")


def load_countries() -> tuple[dict, dict]:
    data = json.loads(COUNTRIES.read_text(encoding="utf-8"))
    lookup: dict[str, str] = {}
    names: dict[str, dict] = {}
    for iso, zh, en, aliases in data["countries"]:
        names[iso] = {"zh": zh, "en": en}
        for key in (iso.lower(), zh, en.lower(), *(a.lower() for a in aliases.split("|") if a)):
            lookup.setdefault(key.strip().lower(), iso)
    return lookup, names


def resolve_iso(name: str) -> tuple[str, dict]:
    lookup, names = load_countries()
    key = name.strip()
    iso = lookup.get(key.lower()) or lookup.get(key.lower().replace(" ", ""))
    if iso:
        return iso, names[iso]
    # 允许直接传未收录的 ISO3（geoBoundaries 覆盖比内置中文表更全）
    if len(key) == 3 and key.isalpha():
        return key.upper(), {"zh": key.upper(), "en": key.upper()}
    hint = [n["zh"] for n in names.values() if key and key in n["zh"]][:8]
    raise RegionError(
        f"无法把「{name}」解析为国家/地区。请传中文名、英文名或 ISO3"
        f"（如 LIE / 列支敦士登 / Liechtenstein）；也可用 --bbox 或 --geojson。"
        + (f" 相近候选：{', '.join(hint)}" if hint else ""))


def assert_allowed(iso: str, display: str) -> None:
    # v1.9.0：台湾、香港、澳门是中国的一部分，与 CHN 同属 GEM-15 声明排除的
    # 中国区域。实测两个漏洞：①名称表缺「台湾/香港/澳门」条目，输入只得到
    # 「无法解析」而非指向 cn-dem；②3 字母直传（TWN/HKG/MAC）会完全绕过
    # 排除检查去拉边界与 STAC。现在 ISO3 直传也归并到中国排除路径，文案明确。
    if iso in CHINA_REGION_ISO:
        raise CoverageError(
            f"「{display}」({iso}) 是中国的一部分，位于 GEM-15 覆盖排除区"
            f"（中国{CHINA_REGION_ISO[iso]}）。\n{EXCLUDED_NOTE}")
    if iso in EXCLUDED_ISO3:
        raise CoverageError(
            f"「{display}」({iso}) 位于 GEM-15 覆盖排除区。\n{EXCLUDED_NOTE}")


def _china_boundary(cache_dir: Path) -> dict | None:
    """拉取 geoBoundaries CHN **ADM1** 简化边界作为「中国（含台湾、港澳）」判定源。

    实测（本轮台湾测试）：①gbOpen 的 CHN ADM0 **不含**香港/澳门（香港
    bbox 中心点真实漏判）；②gbOpen 根本没有 HKG/MAC 条目（API 404）；
    ③CHN ADM1 = 中国官方 34 个省级行政区，实测含 `Taiwan Province`、
    `Hong Kong Special Administrative Region`、`Macau Special Administrative
    Region` —— 单一数据源即与「中国（含台湾、港澳）」语义完全一致。
    任何失败 warn（可观测）后返回 None（放行）。
    """
    cache = cache_dir / "boundary" / "CHN_ADM1_simplified.geojson"
    try:
        gj = None
        if cache.exists() and cache.stat().st_size > 0:
            try:
                gj = json.loads(cache.read_text(encoding="utf-8"))
            except Exception as exc:                  # noqa: BLE001
                warn(f"中国 ADM1 边界缓存损坏，重新下载 {cache}: "
                     f"{type(exc).__name__}")
                cache.unlink()
        if gj is None:
            meta = gb_meta("CHN", "ADM1", cache_dir)
            url = (meta.get("simplifiedGeometryGeoJSON")
                   or meta.get("gjDownloadURL"))
            if not url:
                warn("geoBoundaries CHN ADM1 元数据缺 GeoJSON 链接，跳过精判")
                return None
            code, _ = curl_get(url, dest=cache, timeout=120, retries=3,
                               connect_timeout=CONNECT_TIMEOUT)
            if code != 200:
                warn(f"中国 ADM1 边界下载失败 HTTP {code}，跳过精判")
                cache.unlink(missing_ok=True)
                return None
            gj = json.loads(cache.read_text(encoding="utf-8"))
        feats = [f for f in gj.get("features", []) if f.get("geometry")]
        if not feats:
            warn("中国 ADM1 边界 0 个要素，跳过精判")
            return None
        return {"type": "FeatureCollection", "features": feats}
    except Exception as exc:                          # noqa: BLE001
        warn(f"中国边界获取失败，跳过中国范围精判（放行，可观测）："
             f"{type(exc).__name__}: {exc}")
        return None


def assert_not_china(box: tuple, name: str, cache_dir: Path) -> None:
    """v1.9.0：bbox / 自备 GeoJSON 的中心点落在中国（含台湾、香港、澳门）→ 拒绝。

    实测（本轮台湾测试）：GEM-15 STAC 对台湾区域实际有 cell 条目
    （14 片 / 826 MB），「bbox 落在排除区由 STAC 判空兜住」的契约失效。
    以区域 bbox 中心点为代表点，对 CHN+HKG+MAC 三边界并集精判，
    与名称路径「按政区归属」语义一致：中心在中国境内即 CoverageError
    指向 cn-dem；邻国 bbox 即使矩形擦到中国边界也不误伤。
    粗判（CHINA_BBOX）不相交零网络直接放行。
    """
    if not bbox_intersects(box, CHINA_BBOX):
        return
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    gj = _china_boundary(cache_dir)
    if gj is None:
        return
    try:
        from shapely.geometry import Point, shape
        from shapely.ops import unary_union
    except ImportError:                               # pragma: no cover
        warn("shapely 未安装，跳过中国范围精判（放行，可观测）")
        return
    geoms = [shape(f["geometry"]) for f in gj.get("features", [])
             if f.get("geometry")]
    if not geoms:
        warn("中国边界 0 个要素，跳过精判（放行，可观测）")
        return
    union = unary_union(geoms)
    hit = Point(cx, cy).within(union)
    if not hit:
        # 实测（本轮台湾测试）：simplified 版对小面积要素失真严重 ——
        # 香港 SAR（1104 km²）3 个 part 均不含新界大部，点在要素 bounds
        # 内却在多边形外（澳门同源更小）。小要素（<0.5 平方度）改用
        # bounds 近似；放宽方向是「多拒、指向 cn-dem」，符合契约；
        # 大要素（台湾 3.2 平方度等）仍用精确多边形。
        hit = any(g.bounds[0] <= cx <= g.bounds[2] and
                  g.bounds[1] <= cy <= g.bounds[3]
                  for g in geoms if g.area < 0.5)
    if hit:
        raise CoverageError(
            f"「{name}」的 bbox 中心 ({cx:.4f}, {cy:.4f}) 落在中国境内"
            f"（含台湾、香港、澳门），位于 GEM-15 覆盖排除区。\n{EXCLUDED_NOTE}")
    log(f"bbox 中心 ({cx:.4f}, {cy:.4f}) 不在中国境内，继续")


def gb_meta(iso: str, adm: str, cache_dir: Path, refresh: bool = False) -> dict:
    url = f"{GB_API}/{iso}/{adm}/"
    cache = cache_dir / "boundary" / f"meta_{iso}_{adm}.json"
    if refresh and cache.exists():
        cache.unlink()
    return get_json(url, cache=cache)


def gb_geojson(meta: dict, adm: str, cache_dir: Path,
               refresh: bool = False) -> dict:
    key = "gjDownloadURL" if adm == "ADM0" else "simplifiedGeometryGeoJSON"
    alt = "simplifiedGeometryGeoJSON" if adm == "ADM0" else "gjDownloadURL"
    used = key
    url = meta.get(key)
    if not url:
        url = meta.get(alt)
        used = alt
        # v1.5.0：旧实现 `meta.get(key) or meta.get(alt)` 静默回退，且日志按
        # key（而非实际选中的字段）判断 —— ADM0 主链接缺失时实测输出为空，
        # 用户完全不知道用了简化版边界。现在显式 [warn] + 打印真实字段名。
        warn(f"{adm} 主链接 {key} 缺失，回退到 {used}（边界精度可能降低）")
    if not url:
        raise RegionError("geoBoundaries 元数据缺少 GeoJSON 下载链接")
    if used != "gjDownloadURL" or adm != "ADM0":
        log(f"边界: {adm} 使用 {used}（避免下载超大完整版）")
    iso = meta.get("boundaryISO", "X")
    cache = cache_dir / "boundary" / f"{iso}_{adm}.geojson"
    if refresh and cache.exists():
        cache.unlink()
    if cache.exists() and cache.stat().st_size > 0:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as exc:                       # noqa: BLE001
            warn(f"边界缓存损坏，重新下载 {cache}: {type(exc).__name__}")
            cache.unlink()
    # v1.3.0：geoBoundaries 的 GeoJSON 全部托管在 GitHub（gjDownloadURL /
    # simplifiedGeometryGeoJSON / tjDownloadURL 同域）。本机实测该域可返回 000
    # 超时而 API 元数据仍 200，旧实现只抛一句「GET 失败 000」，用户无从下手。
    # 现在显式给出四条可执行建议，并清掉可能残留的损坏缓存。
    hint = ("\ngeoBoundaries 的 GeoJSON 托管在 GitHub，本机实测该域**间歇性**不可达。"
            "已依次尝试 github.com / raw.githubusercontent.com / jsDelivr / ghproxy "
            "四个候选并校验内容。可选："
            "\n  ① 稍后重试；② 用 --bbox \"minx,miny,maxx,maxy\" 下矩形区域；"
            "\n  ③ 用 --geojson 自备边界文件；"
            "\n  ④ 保留已有 boundary 缓存（清理时用 --clean-tiles-only，别用 --clean-cache）。")

    def _valid_geojson(pth: Path) -> None:
        """镜像内容校验：必须是可解析的 GeoJSON（挡掉 ghproxy 的 200 错误页）。"""
        data = json.loads(pth.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data.get("type"):
            raise ValueError("响应不是 GeoJSON 对象")

    try:
        curl_get_mirrors(url, dest=cache, timeout=120, retries=1,
                         validate=_valid_geojson, connect_timeout=CONNECT_TIMEOUT)
    except DownloadError as exc:
        cache.unlink(missing_ok=True)
        raise RegionError(f"边界 GeoJSON 下载失败：{exc}{hint}") from exc
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except Exception as exc:                          # noqa: BLE001
        cache.unlink(missing_ok=True)
        raise RegionError(f"边界 GeoJSON 解析失败 {url}: {exc}{hint}") from exc


def bbox_feature(bbox, name: str) -> dict:
    minx, miny, maxx, maxy = bbox
    ring = [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"gem15:name": name},
         "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def parse_bbox(spec: str):
    try:
        nums = [float(x) for x in spec.replace(";", ",").split(",")]
    except ValueError as exc:
        raise RegionError(f"--bbox 需要 4 个数字，实际：{spec}") from exc
    if len(nums) != 4:
        raise RegionError("--bbox 需要 minx,miny,maxx,maxy 四个数")
    minx, miny, maxx, maxy = nums
    if not (-180 <= minx < maxx <= 180 and -90 <= miny < maxy <= 90):
        raise RegionError("--bbox 坐标非法（需 WGS84，且 min < max）")
    return minx, miny, maxx, maxy


def filter_adm1(gj: dict, want: str) -> dict:
    feats = gj.get("features", [])
    exact, loose = [], []
    key = want.strip().lower()
    for f in feats:
        nm = str(f.get("properties", {}).get("shapeName", ""))
        if nm.lower() == key:
            exact.append(f)
        elif key and key in nm.lower():
            loose.append((f, nm))
    if len(exact) == 1:
        return {"type": "FeatureCollection", "features": exact}
    candidates = [f for f, _ in loose] or exact
    listed = [str(f.get("properties", {}).get("shapeName", "")) for f in candidates]
    if len(candidates) == 1:
        return {"type": "FeatureCollection", "features": candidates}
    raise RegionError(
        f"一级行政区「{want}」命中 {len(candidates)} 个候选，请给出更精确的名称："
        + ", ".join(listed[:20]))


def resolve(name: str, *, bbox_spec: str | None = None,
            geojson_path: str | None = None, country: str | None = None,
            adm1: str | None = None, cache_dir: Path | None = None,
            refresh: bool = False) -> tuple[dict, dict]:
    """返回 (GeoJSON FeatureCollection, meta)。"""
    cache = cache_dir or default_cache()
    name = name or "region"
    # v1.10.0：黑盒实测 `--country TWN --bbox 法国` 会静默忽略 TWN 跑起
    # 法国流程（用户想要台湾却拿到法国边界，无任何提示）。互斥来源显式报错。
    if country and (bbox_spec or geojson_path):
        raise RegionError(
            "--country 与 --bbox/--geojson 互斥（同时给出会静默忽略其中一个，"
            "拒绝猜测意图）；请二选一")
    # v1.11.0：黑盒实测同类静默忽略还有两处：
    # ①`--adm1 X --bbox Y` —— adm1 被静默忽略，用户以为拿了 X 实际是 Y 矩形；
    # ②`gem15_dem.py 日本 out --country FRA` —— 名称完全被无视，静默跑法国。
    if adm1 and (bbox_spec or geojson_path):
        raise RegionError(
            "--adm1 只用于国家/一级行政区路径，与 --bbox/--geojson 互斥"
            "（同时给出会静默忽略 --adm1，拒绝猜测意图）；请二选一")
    if country:
        iso_c, _ = resolve_iso(country)
        try:
            iso_n, _ = resolve_iso(name)
        except RegionError:
            iso_n = None          # 名称不可解析：仅作标签，允许（不阻断）
        if iso_n and iso_n != iso_c:
            raise RegionError(
                f"名称「{name}」解析为 {iso_n}，与 --country {iso_c} 冲突"
                f"（旧实现会静默忽略名称去跑 {iso_c}，用户想要的可能正是"
                f" {iso_n}）；请去掉其一或保持一致")
        if not iso_n and name != country:
            log(f"名称「{name}」不参与国家解析（--country {iso_c} 优先，"
                f"仅作输出目录名回退）")

    if geojson_path:
        p = Path(geojson_path)
        if not p.exists():
            raise RegionError(f"自备边界不存在: {p}")
        # v1.10.0：黑盒实测 `--geojson <目录>` 抛 PermissionError、坏 JSON 抛
        # JSONDecodeError，均未捕获 -> traceback exit 1。统一包成 RegionError
        # （GemError 子类，主入口 exit 2 + 中文提示），不静默崩溃。
        try:
            gj = json.loads(p.read_text(encoding="utf-8"))
        except NotADirectoryError as exc:
            raise RegionError(f"自备边界路径无效: {p}") from exc
        except OSError as exc:
            raise RegionError(f"自备边界读取失败 {p}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RegionError(f"自备边界不是合法 JSON {p}: {exc}") from exc
        if not gj.get("type"):
            raise RegionError("自备 GeoJSON 缺少 type 字段")
        box = bbox_of_geojson(gj)
        assert_not_china(box, name, cache)
        meta = {"name": name, "source": f"user geojson: {p}", "adm": "user",
                "iso3": None, "bbox": box, "features": len(gj.get("features", [gj]))}
        log(f"边界: 自备 GeoJSON {p}  bbox={tuple(round(v, 4) for v in box)}")
        return gj, meta

    if bbox_spec:
        box = parse_bbox(bbox_spec)
        assert_not_china(box, name, cache)
        meta = {"name": name, "source": "user bbox", "adm": "bbox",
                "iso3": None, "bbox": box, "features": 1}
        log(f"边界: 用户 bbox {tuple(box)}（WGS84 矩形）")
        return bbox_feature(box, name), meta

    iso, names = resolve_iso(country or name)
    adm = "ADM1" if adm1 else "ADM0"
    assert_allowed(iso, names["zh"])
    meta_json = gb_meta(iso, adm, cache, refresh=refresh)
    gj = gb_geojson(meta_json, adm, cache, refresh=refresh)
    if adm == "ADM1":
        gj = filter_adm1(gj, adm1)
    box = bbox_of_geojson(gj)
    n = len(gj.get("features", []))
    if n == 0:
        raise RegionError("geoBoundaries 返回 0 个要素（判空，不静默继续）")
    meta = {"name": adm1 or names["zh"], "name_en": names["en"],
            "source": f"geoBoundaries gbOpen {iso} {adm}",
            "license": meta_json.get("boundaryLicense", ""),
            "adm": adm, "iso3": iso, "bbox": box, "features": n}
    log(f"边界: {meta['name']} ({iso} {adm}, {n} 个要素) "
        f"bbox={tuple(round(v, 4) for v in box)}")
    return gj, meta


def main() -> None:
    import sys
    pos, opts, flags = parse_args(sys.argv[1:],
                                  ("--bbox", "--geojson", "--country",
                                   "--adm1", "--cache"), CLI_FLAGS)
    if check_help(pos, flags, USAGE):
        sys.exit(0)
    if not pos:
        print(USAGE)
        sys.exit(1)
    cache = Path(opts["cache"]) if "cache" in opts else default_cache()
    try:
        gj, meta = resolve(pos[0], bbox_spec=opts.get("bbox"),
                           geojson_path=opts.get("geojson"),
                           country=opts.get("country"), adm1=opts.get("adm1"),
                           cache_dir=cache, refresh="refresh" in flags)
    except GemError as exc:
        # v1.11.0：旧实现 except Exception 会把代码缺陷（TypeError 等）也
        # 包装成 exit 2 的「可预期错误」，丢失堆栈。与 gem15_dem 主入口对齐：
        # 只有 GemError 系（可预期）exit 2，未预期异常向上抛由 Python 退出 1。
        print(f"[错误] {type(exc).__name__}: {exc}")
        sys.exit(2)
    out = Path(pos[1]) if len(pos) > 1 else Path(f"{pos[0]}_boundary.geojson")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
    log(f"边界写出: {out}  {meta}")


if __name__ == "__main__":
    main()

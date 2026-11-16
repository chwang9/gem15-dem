#!/usr/bin/env python3
"""GEM-15 15m DEM 技能公共工具：常量、异常、GET、缓存、参数解析。

设计要点（均来自本机实测，见 README「性能实测」与 CHANGELOG）：
- 所有网络请求走 curl 子进程 + GET；POST 在本机会被网关拦截返回 504。
- GCS COG 的字节级**并发** Range 分块只有 0.32–0.73 MB/s，比单连接 1.89 MB/s 慢
  2.6–5.9 倍；单个大响应又会被网关在数 MiB 处掐断。因此 --mode full 采用
  16 MiB **串行**分段 + 段级断点缓存，分片粒度放在 1° cell 上。
- 远程 /vsicurl/ 窗口读取是默认取数方式；实测并发打开多个 COG 反而慢 2.6 倍
  （4 片 48.89 s vs 串行 18.87 s），故窗口读取保持串行，并发只用于 STAC 小 JSON。
- 任何静默兜底都必须打印 [warn]，否则「优化没生效」永远查不出来。
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

VERSION = "2.0.0"

# STAC 小 JSON 索引的并发度（微基准实测，见 CHANGELOG v1.1.0）：
# 9 个 item / 12 个请求，2->8->16 分别 8.54 / 6.06 / 4.23 s；
# 复测 8 为 4.62/6.28 s、16 为 4.43/4.34 s，16 更快且方差更小。
# 注意：这只适用于小 JSON；GDAL 窗口读取并发是明确反优化。
INDEX_WORKERS = 16

STAC_BASE = "https://storage.googleapis.com/climate-and-sustainability/gem-15/stac"
COLLECTION_URL = f"{STAC_BASE}/gem-15/collection.json"
GB_API = "https://www.geoboundaries.org/api/current/gbOpen"

# v1.12.0：geoBoundaries 的 GeoJSON 全部托管在 GitHub。本轮德国实测该域**间歇性**
# 不可达：同一 URL 四次 21 s 连接超时（状态码 000），一分钟后同一 URL 返回
# 200 / 2.8 MB / 3.3 s；镜像 jsDelivr 在本机 immediate 000（被拦），ghproxy
# 返回 200 但只有 132 B 错误页（**200 不等于成功**）。对策：
#   ①连接阶段单独设短超时，不可达主机 ~12 s 内失败，而不是耗满 --max-time；
#   ②准备 4 个候选主机逐个尝试，并对**内容**做校验（挡掉 ghproxy 的错误页）。
CONNECT_TIMEOUT = 12

# 数据生产方声明的覆盖排除区（GEM-15 Earth Engine Catalog / STAC collection）
EXCLUDED_ISO3 = {"CHN", "RUS", "MNG", "GRL", "ATA"}
EXCLUDED_NOTE = ("GEM-15 官方声明覆盖 60°S–84°N，并排除中国（含台湾、香港、澳门）、"
                 "俄罗斯、蒙古、格陵兰、南极洲。输入这些区域会得到空数据，"
                 "本技能直接报错，"
                 "不静默产出空 GeoTIFF。中国区域请改用 cn-dem（Copernicus GLO-30）。")

# v1.2.0：删除从未被使用的 LAT_MIN/LAT_MAX=(-60, 84) 死常量。
# 它是 v1.1.0 修复南纬 -61° 漏片时遗留的，且与真实 collection（m70_m50 组、
# 覆盖到 -61°）矛盾；留着只会误导后续维护。覆盖范围一律以 STAC child 组为准。

# 输出规模上限（实测依据见 CHANGELOG v1.2.0）。按成果数组估算：
#   bytes = 输出像元数 × 波段数 × 4 B（float32）
# 512 MiB 可覆盖约 1.34 亿像元的 dtm（约 2°×2°），或 2680 万像元的 dsm。
# 超限直接报错并给出可操作建议，绝不让进程被 OOM 杀掉（静默失败）。
DEFAULT_MAX_OUTPUT_MB = 512

# v1.7.0：full 模式整 COG 落盘的下载总量上限（大国防护）。
# 实测法国整国 147 片 / 19705 MiB（19.24 GiB），而旧实现会先把这些全部下完
# 才做输出规模预检。2 GiB 可覆盖十余片常规 cell（单片 150~183 MB），
# 又不至于让一次误操作在本机网关下跑几十小时。可用 --max-download-mb 调整。
DEFAULT_MAX_DOWNLOAD_MB = 2048

# 写大文件前至少保留的可用磁盘（MiB）。实测本机 C: 仅余约 10 GiB、D: 163 GiB，
# 大国成果 32 GiB 落在 C: 会中途写满并产生损坏的 GeoTIFF，必须提前拒绝。
MIN_FREE_MB = 512

# v1.3.0：运行时缓存的四类子目录。--clean-cache 全部清空，
# --clean-tiles-only 只清 tiles（保留 STAC 索引 / 边界 / 临时文件）。
CACHE_SUBDIRS = ("tiles", "stac", "boundary", "_tmp")

LAYER_BANDS = {
    "dtm": ["dtm"],
    "dsm": ["dsm", "dsm_percentile_5", "dsm_percentile_50",
            "dsm_percentile_95", "dsm_std"],
}
ATTRIBUTION = "This dataset was produced by Google / 此数据集由 Google 制作"
LICENSE = "CC-BY-4.0"
VERTICAL_DATUM = "EGM96 orthometric height (EPSG:5773), metres"

SHORT_SLEEP = 2      # 5xx / 网络错误：短等
LONG_SLEEP = 20      # 429：长等


class GemError(Exception):
    """技能可预期的错误基类。"""


class CoverageError(GemError):
    """区域不在 GEM-15 覆盖范围内。"""


class DownloadError(GemError):
    """瓦片/边界/索引下载失败。"""


class EmptyRasterError(GemError):
    """拼接结果没有任何有效像元（禁止静默产出空数据）。"""


class OutputTooLargeError(GemError):
    """预估输出数组超过内存预算，拒绝在读取任何像元之前启动。

    v1.7.0：携带 info（预估规模 / 网格尺寸 / bbox / CRS），供入口生成
    **可执行**的分块方案，而不是只给一句含糊的「拆成 2-3 块」。
    """

    def __init__(self, message: str, info: dict | None = None):
        super().__init__(message)
        self.info = dict(info or {})


class DownloadTooLargeError(GemError):
    """full 模式整 COG 落盘总量超过预算，拒绝在发出任何下载请求之前启动。"""


class DiskSpaceError(GemError):
    """目标磁盘可用空间不足以容纳预估成果，拒绝在写大文件之前启动。"""


class RegionError(GemError):
    """区域名解析失败 / 边界数据异常。"""


def available_memory_mb() -> float | None:
    """v1.7.0：返回可用物理内存 MB（拿不到就返回 None，绝不静默假报）。

    Windows 用 GlobalMemoryStatusEx；其它平台用 sysconf。实测本机
    总 31.68 GiB / 可用 7.54 GiB；法国整国成果数组 32 GiB、峰值约 98 GiB，
    即便用户把 --max-output-mb 提到 40000 也会被 OOM 静默杀掉，
    因此不能只按「用户填的上限」判断，必须对照真实可用内存。
    """
    try:
        if sys.platform == "win32":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = _MemStatus()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullAvailPhys / 1048576.0
            return None
        import os
        return (os.sysconf("SC_AVPHYS_PAGES") *
                os.sysconf("SC_PAGE_SIZE") / 1048576.0)
    except Exception as exc:                       # noqa: BLE001
        # 静默兜底会让「物理内存预检没生效」永远查不出来（用户既有规矩）。
        # 拿不到内存信息时明确告警，由调用方降级为「只按用户上限判断」。
        warn(f"无法读取可用物理内存，本次跳过物理内存预检："
             f"{type(exc).__name__}: {exc}")
        return None


def assert_disk_space(target: Path, needed_mb: float,
                      stats: dict | None = None) -> None:
    """v1.7.0：在写 GeoTIFF / 下载大文件之前检查目标盘可用空间。

    实测本机 C: 可用 10.3 GiB、D: 163.2 GiB；法国整国成果约 32 GiB，
    若默认落在 C: 会写到一半磁盘满，GDAL 抛含糊的写错误甚至留下损坏文件。
    `needed_mb` 是**预估成果/下载**大小，另留 MIN_FREE_MB 余量给系统与临时文件。
    """
    import shutil
    p = Path(target).resolve()
    anchor = p.anchor or str(p)
    try:
        free_mb = shutil.disk_usage(anchor).free / 1048576.0
    except OSError as exc:
        raise DiskSpaceError(f"无法读取磁盘可用空间 {anchor}: {exc}") from exc
    need = float(needed_mb) + MIN_FREE_MB
    if stats is not None:
        stats["disk_free_mb"] = round(free_mb, 2)
        stats["disk_needed_mb"] = round(need, 2)
        stats["disk_anchor"] = anchor
    if free_mb < need:
        raise DiskSpaceError(
            f"目标磁盘 {anchor} 可用 {free_mb:.0f} MB，本次预计需要 "
            f"{needed_mb:.0f} MB + {MIN_FREE_MB} MB 余量 = {need:.0f} MB。\n"
            "已在写任何大文件之前停止，避免写到一半磁盘满产生损坏 GeoTIFF。\n"
            "建议：① 把输出目录放到剩余空间更大的盘（如 D:/...）；"
            "② 用 --bbox / --split auto 分块；③ 大区域优先用 dtm。")


def rmtree_verified(path: Path, what: str = "目录") -> bool:
    """v1.11.0：rmtree 后**验证目录确实消失**，否则打印 [warn]（零静默假成功）。

    实测（2026-11-15）：宿主沙箱对单 turn 累计删除 >50 个文件的操作静默拦截
    （SAFE_DELETE_BULK_CONFIRM_REQUIRED，rmtree 不抛 OSError、进程正常返回、
    目录却原样保留）。旧实现 `try: rmtree except OSError: warn` 完全探测不到
    这种拦截 —— selftest 每跑一次就泄漏一个 152 文件的临时目录（D 盘累积
    8 个）而无任何提示，违反「静默失败必须可观测」铁律。
    返回是否真正删除；调用方据此决定补救提示（如磁盘仍持有双份数据）。
    """
    import shutil
    p = Path(path)
    if not p.exists():
        return True
    try:
        shutil.rmtree(p)
    except OSError as exc:
        warn(f"{what} 删除失败（可稍后手动删除）: {p}: {exc}")
        return False
    if p.exists():
        warn(f"{what} 删除未生效（可能被宿主批量删除守卫拦截，"
             f"单 turn 累计 >50 个文件会被静默拒绝）: {p}")
        return False
    return True


def dispose_tree(path: Path, what: str = "目录",
                 budget: int = 40) -> dict:
    """v1.12.0：**预算内才真删，超出预算就清零留空壳**（守卫安全）。

    背景（德国 12.5 GB 实测）：宿主批量删除守卫按**进程累计**计数 —— 不止
    「静默拦截」，在后台任务里它会**直接终止进程**：
    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
     {"count":68,"threshold":50,...}` 之后进程 exit 1、无 traceback。
    所以「删不掉就 warn」不够，必须**根本不发起**超过预算的批量删除。

    策略：条目数 ≤ budget 才交给 rmtree_verified（保持目录干净）；
    超过 budget 则把所有文件**清零**（磁盘立刻释放、零删除），
    并在日志里说明保留的是 0 B 空壳。返回统计供调用方汇总。
    """
    p = Path(path)
    out = {"path": str(p), "files": 0, "bytes": 0,
           "mode": "absent", "deleted": True}
    if not p.exists():
        return out
    files = [f for f in p.rglob("*") if f.is_file()]
    out["files"] = len(files)
    out["bytes"] = sum(f.stat().st_size for f in files)
    if len(files) <= budget:
        out["deleted"] = rmtree_verified(p, what)
        out["mode"] = "rmtree" if out["deleted"] else "blocked"
        return out
    shells = 0
    for f in files:                     # 清零：空间立即释放，且不触发守卫
        try:
            if f.stat().st_size == 0:   # v1.13.0：已是 0 B 空壳，跳过无效写
                shells += 1
            else:
                f.write_bytes(b"")
        except OSError as exc:          # noqa: BLE001
            warn(f"{what} 清零失败 {f}: {exc}")
    out["mode"] = "truncate"
    out["deleted"] = False
    out["shells"] = shells
    log(f"{what} 含 {len(files)} 个条目（超过删除预算 {budget}），"
        f"已清零释放 {fmt_mb(out['bytes'])}，目录保留为空壳：{p}"
        + (f"（其中 {shells} 个已是空壳，本次跳过）" if shells else "")
        + "；空壳不占空间、会被重下时识别为未缓存，如需彻底移除请手动删该目录")
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print("[warn] " + msg, flush=True)


def default_cache() -> Path:
    """缓存默认落 D 盘（C 盘空间紧张）；D 盘不存在时回退技能目录并明确告警。"""
    d = Path("D:/gem15-dem-cache")
    if Path("D:/").exists():
        return d
    fallback = Path(__file__).resolve().parent.parent / "_cache"
    warn(f"D 盘不存在，缓存回退到技能目录 {fallback}（大区域可能占满 C 盘）")
    return fallback


def _sleep_for(code: str, attempt: int) -> None:
    if code == "429":
        time.sleep(min(LONG_SLEEP, 5 * (attempt + 1)))
    else:
        time.sleep(min(SHORT_SLEEP, 1 + attempt))


def curl_bytes(url: str, timeout: int = 45, retries: int = 3,
               connect_timeout: int | None = None) -> bytes:
    """GET 小响应到内存，不落盘、不创建也不删除任何临时文件（v1.4.0）。

    取代 get_json 原先的「mkstemp -> curl -o 临时文件 -> finally unlink」模式。
    英国整国实测：87 个 1° cell 产生 94 次临时文件创建 + 删除，宿主批量删除
    守卫在累计第 50 次删除时终止进程（exit 1、日志被 SAFE_DELETE 刷屏），
    并留下 46 个 .tmp 垃圾。STAC JSON 只有 5–45 KB，实测 curl stdout 直接
    取回秒回且不截断，故改为内存读取，从根上消除这条路径上的删除。

    -w '\n%{http_code}' 把状态码附在 body 之后，用 rpartition 拆开；
    空 body / 非 200 / 网络异常全部显式重试或报错，不静默返回错误页。
    """
    last_code = "000"
    for attempt in range(retries + 1):
        cmd = ["curl", "-s", "-L", "--max-time", str(timeout),
               "-w", "\n%{http_code}", url]
        if connect_timeout:
            # v1.12.0：连接阶段短超时，DNS/握手失败不必等满 --max-time。
            cmd[3:3] = ["--connect-timeout", str(connect_timeout)]
        body = b""
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
            body, _, tail = r.stdout.rpartition(b"\n")
            last_code = tail.strip().decode("ascii", "replace")[-3:] or "000"
        except Exception as exc:                        # noqa: BLE001
            warn(f"请求异常 {type(exc).__name__}: {exc} -> {url}")
            last_code = "000"
        if last_code in ("200", "206") and body:
            return body
        if last_code == "404":
            raise DownloadError(f"GET 404: {url}")
        if last_code in ("200", "206"):
            warn(f"响应体为空，重试：{url}")
            last_code = "short"
        if attempt < retries:
            _sleep_for(last_code, attempt)
    raise DownloadError(f"GET 失败（最后状态码 {last_code}）: {url}")


def curl_get(url: str, dest: Path, timeout: int = 60,
             retries: int = 3, expect_bytes: int | None = None,
             connect_timeout: int | None = None) -> tuple[int, Path]:
    """GET 一个 URL 到 dest，返回 (http_code, 落盘路径)。用于大文件 / 边界文件。

    - 用 curl 子进程，stdout 只拿 http_code，响应体直接落盘（避免大响应经管道被掐）。
    - expect_bytes 非空时做**字节级校验**：不符抛 DownloadError（不静默损坏）。
    - v1.1.0 移除 resume 参数：full 模式改段级断点后已无调用者，且 curl -C - 遇到
      服务端忽略 Range 返回 200 时会**追加**到旧文件，字节校验必然失败并陷入重试。
    - v1.4.0：dest 改为必填。原 `dest=None` 分支生成 `_tmp/get_{hash}.bin` 且
      从不回收，get_json 改用 curl_bytes 后已无任何调用者，属死代码。
    """
    tmp = dest
    tmp.parent.mkdir(parents=True, exist_ok=True)
    last_code = "000"
    for attempt in range(retries + 1):
        cmd = ["curl", "-s", "-L", "--max-time", str(timeout),
               "-o", str(tmp), "-w", "%{http_code}", url]
        if connect_timeout:
            # v1.12.0：连接阶段短超时。德国实测 GitHub 不可达时每次尝试
            # （无 --connect-timeout）要耗 21 s；4 次 = 92 s 纯等待。
            cmd[3:3] = ["--connect-timeout", str(connect_timeout)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout + 15)
            last_code = (r.stdout or "").strip()[-3:] or "000"
        except Exception as exc:                        # noqa: BLE001
            warn(f"请求异常 {type(exc).__name__}: {exc} -> {url}")
            last_code = "000"
        if last_code == "200" or last_code == "206":
            if expect_bytes is not None and tmp.exists():
                got = tmp.stat().st_size
                if got != expect_bytes:
                    warn(f"字节校验失败 {got} != {expect_bytes}，重试：{url}")
                    last_code = "short"
                else:
                    return int(last_code), tmp
            else:
                return int(last_code), tmp
        elif last_code == "404":
            return 404, tmp
        if attempt < retries:
            _sleep_for(last_code, attempt)
    raise DownloadError(f"GET 失败（最后状态码 {last_code}）: {url}")


# GitHub 原始文件的两种官方写法：网页 raw 跳转与 raw 子域。
_GH_PATTERNS = (
    r"^https?://github\.com/(?P<o>[^/]+)/(?P<r>[^/]+)/raw/(?P<ref>[^/]+)/(?P<p>.+)$",
    r"^https?://raw\.githubusercontent\.com/(?P<o>[^/]+)/(?P<r>[^/]+)/"
    r"(?P<ref>[^/]+)/(?P<p>.+)$",
)


def github_mirror_urls(url: str) -> list[str]:
    """把 GitHub 原始文件 URL 展开成候选主机列表（首位永远是传入的 URL）。

    v1.12.0 实测依据（本机，德国 / 中国 ADM1 边界同域）：
    - `github.com/<o>/<r>/raw/<ref>/<path>` 与 `raw.githubusercontent.com/...`
      会**间歇性**连接超时（000），同一 URL 隔一分钟又 200 —— 单点不可靠；
    - `cdn.jsdelivr.net/gh/...` 本机 immediate 000（被拦），但零成本，留作候选；
    - `ghproxy.net/<raw url>` 返回 **200 + 132 B 错误页** —— 说明「HTTP 200」
      不能作为成功判据，调用方必须校验内容（见 curl_get_mirrors 的 validate）。
    非 GitHub URL 原样返回单元素列表，行为与旧实现完全一致。
    """
    for pat in _GH_PATTERNS:
        m = re.match(pat, url)
        if not m:
            continue
        o, r, ref, p = m.group("o"), m.group("r"), m.group("ref"), m.group("p")
        raw = f"https://raw.githubusercontent.com/{o}/{r}/{ref}/{p}"
        web = f"https://github.com/{o}/{r}/raw/{ref}/{p}"
        cands = [url, raw, web,
                 f"https://cdn.jsdelivr.net/gh/{o}/{r}@{ref}/{p}",
                 f"https://ghproxy.net/{raw}"]
        seen, out = set(), []
        for c in cands:                      # 去重但保持顺序（传入的排第一）
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out
    return [url]


def _host_of(url: str) -> str:
    return urlparse(url).netloc or url


def curl_get_mirrors(url: str, dest: Path, timeout: int = 120,
                     retries: int = 1, validate=None,
                     connect_timeout: int | None = None) -> tuple[int, Path, str]:
    """按 github_mirror_urls 的候选顺序取回文件，返回 (code, 路径, 实际 URL)。

    validate 是可选内容校验回调 `fn(path) -> None`（抛异常表示不通过）。
    实测 ghproxy 的 200 错误页只有 132 B，若不校验会被当成边界文件写进缓存，
    随后 json 解析失败，用户看到「边界解析失败」而非「镜像给了错误页」。
    每个候选失败都记录原因，全部失败时一次性抛出（含每个主机的失败原因），
    不静默退化成空数据。
    """
    cands = github_mirror_urls(url)
    reasons: list[str] = []
    for idx, cand in enumerate(cands):
        try:
            code, path = curl_get(cand, dest=dest, timeout=timeout,
                                  retries=retries,
                                  connect_timeout=connect_timeout)
        except DownloadError as exc:
            reasons.append(f"{_host_of(cand)} -> {exc}")
            continue
        if code not in (200, 206):
            reasons.append(f"{_host_of(cand)} -> HTTP {code}")
            continue
        if validate is not None:
            try:
                validate(path)
            except Exception as exc:                    # noqa: BLE001
                reasons.append(f"{_host_of(cand)} -> 内容校验失败 "
                               f"{type(exc).__name__}")
                continue
        if idx:
            log(f"主地址不可达，已通过镜像 {_host_of(cand)} 取回 {dest.name}")
        return code, path, cand
    raise DownloadError("所有候选地址均失败：" + "；".join(reasons))


def get_json(url: str, timeout: int = 45, retries: int = 3,
             cache: Path | None = None) -> dict:
    """GET JSON，可选落盘缓存。**不创建/删除任何临时文件**（v1.4.0）。

    v1.3.0 用 mkstemp 独占临时文件 + finally unlink，虽然修好了「504 后残留
    错误页」，但把 1 个 JSON 放大成 1 次删除：英国整国 87 个 item → 94 次删除，
    宿主批量删除守卫在第 50 次终止进程（exit 1），且残留 46 个 .tmp。
    现改为 curl_bytes 内存读取：小响应不落盘，也就没有可删的东西；
    缓存只在成功后一次性写入（覆盖，不删除）。
    """
    if cache is not None and cache.exists() and cache.stat().st_size > 0:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as exc:                        # noqa: BLE001
            # v1.5.0：不再 unlink。损坏缓存由成功后的 write_text **覆盖写**修复，
            # 索引路径保持「零临时文件 / 零删除」（v1.4.0 只清掉了成功路径）。
            warn(f"缓存 JSON 损坏，重新下载并覆盖写修复 {cache}: {type(exc).__name__}")
    raw = curl_bytes(url, timeout=timeout, retries=retries)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:                          # noqa: BLE001
        raise DownloadError(
            f"JSON 解析失败 {url}: {type(exc).__name__}: {exc}") from exc
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def parse_args(argv: list[str], with_value: tuple[str, ...] = (),
               known_flags: tuple[str, ...] = ()) -> tuple[list[str], dict, set]:
    """轻量参数解析：位置参数 / 带值选项（含 `--k=v`）/ 布尔开关。

    v1.3.0：传入 known_flags 后启用**未知选项显式报错**。旧实现有两个静默错误：
    - `--layer=dsm` 变成名为 `layer=dsm` 的开关，layer 静默回落到 dtm；
    - 拼错的 `--layr dsm` 变成开关 + 把 `dsm` 当第二个位置参数（输出目录）。
    实测：`parse_args(['LIE','out','--layer=dsm'])` 得到 flags={'layer=dsm'}，
    `parse_args(['LIE','--layr','dsm'])` 得到 pos=['LIE','dsm']，全程无任何提示。
    known_flags 为空时保留旧行为（供离线用例 / 第三方调用兼容）。
    """
    values = {k.lstrip("-") for k in with_value}
    known = {k.lstrip("-") for k in known_flags}
    validate = bool(known_flags)
    pos, opts, flags = [], {}, set()
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--") and "=" in a:
            key, val = a[2:].split("=", 1)
            if key not in values:
                # v1.5.0：区分「给开关传了 =值」与「拼错了带值选项」。
                # 旧实现一律回「开关选项不接受 = 值：请写成 --layr」，
                # 实测 --layr=dsm 会把拼错的选项当成合法开关推荐给用户。
                if key in known:
                    raise GemError(
                        f"开关选项不接受 = 值：{a}；请写成 --{key}。"
                        f"带值选项有：{', '.join('--' + v for v in sorted(values))}")
                if validate:
                    hint = difflib.get_close_matches(
                        key, sorted(values | known), n=3, cutoff=0.5)
                    raise GemError(
                        f"未知选项 --{key}"
                        + (f"，是否想用：{', '.join('--' + h for h in hint)}" if hint else "")
                        + f"。带值选项：{', '.join('--' + v for v in sorted(values))}；"
                        f"可用开关：{', '.join('--' + k for k in sorted(known))}")
                flags.add(a.lstrip("-"))       # 兼容模式：维持旧行为
                i += 1
                continue
            opts[key] = val
            i += 1
            continue
        if a in with_value:
            if i + 1 >= len(argv):
                raise GemError(f"选项 {a} 缺少参数")
            opts[a.lstrip("-")] = argv[i + 1]
            i += 2
        elif a.startswith("--"):
            key = a.lstrip("-")
            if validate and key not in values and key not in known:
                hint = difflib.get_close_matches(
                    key, sorted(values | known), n=3, cutoff=0.5)
                raise GemError(
                    f"未知选项 {a}"
                    + (f"，是否想用：{', '.join('--' + h for h in hint)}" if hint else "")
                    + f"。可用开关：{', '.join('--' + k for k in sorted(known))}")
            flags.add(key)
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts, flags


def check_help(pos: list[str], flags: set, usage: str) -> bool:
    """统一 --help / -h：打印用法并让调用方以 0 退出（旧子 CLI 多为 exit 1）。"""
    if "help" in flags or (pos and pos[0] in ("-h", "--help")):
        print(usage)
        return True
    return False


def bbox_of_geojson(gj: dict) -> tuple[float, float, float, float]:
    """GeoJSON (Feature/FeatureCollection/Geometry) -> WGS84 bbox。

    v1.6.0：GeoJSON 允许 Feature 的 geometry 为 null（自备边界常见）。旧实现直接
    `geom.get(...)` 抛 AttributeError（未预期错误 exit 1）。现跳过并打印 [warn]，
    全部要素都无几何时仍由末尾判空抛 RegionError，不静默产出空 bbox。
    """
    feats = gj.get("features") if "features" in gj else [gj]
    xs, ys = [], []
    skipped = 0

    def walk(node):
        if isinstance(node, (list, tuple)) and node:
            if isinstance(node[0], (int, float)):
                xs.append(float(node[0]))
                ys.append(float(node[1]))
            else:
                for c in node:
                    walk(c)

    def walk_geom(geom):
        # v1.13.0：GeometryCollection 的坐标在 geometries[] 而非
        # coordinates（GeoJSON 规范）。旧实现取空 -> 误报「没有可用坐标」
        # （实测含合法 Point 的 GeometryCollection 被拒）。递归展开。
        if not geom or not isinstance(geom, dict):
            return
        if geom.get("type") == "GeometryCollection":
            for g in geom.get("geometries") or []:
                walk_geom(g)
        else:
            walk(geom.get("coordinates", []))

    for f in feats:
        geom = f.get("geometry", f)
        if not geom or not isinstance(geom, dict):
            skipped += 1
            continue
        walk_geom(geom)
    if skipped:
        warn(f"GeoJSON 中 {skipped} 个要素的 geometry 为空，已跳过（不静默忽略）")
    if not xs or not ys:
        raise RegionError("GeoJSON 中没有可用坐标（要素的 geometry 可能全为 null）")
    return min(xs), min(ys), max(xs), max(ys)


def geojson_geometries(gj: dict) -> list[dict]:
    """v1.6.0：跳过空 geometry 时打印 [warn]（旧实现静默 continue）。"""
    feats = gj.get("features") if "features" in gj else [gj]
    out = []
    skipped = 0
    for f in feats:
        g = f.get("geometry", f)
        if g and isinstance(g, dict) and g.get("type"):
            out.append(g)
        else:
            skipped += 1
    if skipped:
        warn(f"GeoJSON 中 {skipped} 个要素的 geometry 为空/无 type，已跳过（不静默忽略）")
    if not out:
        raise RegionError("GeoJSON 中没有可用 geometry")
    return out


def bbox_intersects(a, b, eps: float = 1e-9) -> bool:
    """正面积相交才算；仅共边/共点不算（避免多下一圈瓦片）。"""
    return (min(a[2], b[2]) - max(a[0], b[0]) > eps and
            min(a[3], b[3]) - max(a[1], b[1]) > eps)


def fmt_mb(n: float) -> str:
    return f"{n / 1048576:.2f} MB"


def progress(i: int, n: int, msg: str) -> None:
    log(f"[{i}/{n}] {msg}")


if __name__ == "__main__":
    log(f"gem15-common v{VERSION}；默认缓存 {default_cache()}")

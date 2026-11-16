---
name: gem15-dem
version: "2.0.0"
display_name: "全球 GEM-15 高程下载器（15 m）"
display_name_en: "Global GEM-15 15m DEM Downloader"
description_zh: >-
  按全球国家 / 一级行政区名称、ISO3、经纬度 bbox 或自备 GeoJSON，分片下载 Google Research
  GEM-15 近全球 15 m 高程（DTM 裸地 / DSM 表面 5 波段），输出按边界裁切的 GeoTIFF
  与出版级预览 PNG。官方 STAC 驱动、COG 窗口按需取数、单文件断点续传，免 API Key。
description_en: >-
  Download Google Research GEM-15 near-global 15 m elevation (DTM or 5-band DSM)
  for any country / ADM1 / bbox / custom GeoJSON. STAC-driven tiled COG window reads,
  single-file resumable download, clipped GeoTIFF plus preview PNG, no API key.
agent_created: true
argument-hint: "<国家/地区名 或 ISO3> [输出目录] [--bbox minx,miny,maxx,maxy] [--layer dtm|dsm] [--mode window|full] [--single] [--bg white] [--no-clip] [--split auto|NxM] [--force-empty] [--max-output-mb 512] [--max-download-mb 2048] | 或 --clean-cache 清缓存"
---

# 全球 GEM-15 高程下载器（15 m）

按国家 / 一级行政区 / bbox / 自备 GeoJSON 下载 Google Research GEM-15 15 m 高程，
输出裁切 GeoTIFF + 出版级预览 PNG。历史变更与实测数据见 `CHANGELOG.md`。

## ⚠️ 覆盖限制（先读）

GEM-15 官方覆盖 60°S–84°N，**排除中国（含台湾、香港、澳门）、俄罗斯、蒙古、格陵兰、
南极洲**；印度境内高程有政策性偏移。输入排除区立即报错（退出码 2），绝不静默产出空文件；
台湾 / 港澳名称与 `TWN/HKG/MAC` 直传同样拦截，bbox / GeoJSON 按中心点精判。
中国区域请改用 `cn-dem`（Copernicus GLO-30，30 m）。用户说"全球任意国家"时，
要主动说明这是数据源本身的限制，不是技能缺陷。

## 快速上手

```bash
PY=C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
S=C:/Users/wangch/.workbuddy/skills/gem15-dem/scripts
$PY $S/gem15_dem.py 列支敦士登 out                    # 一键：边界 -> 选片 -> 取数 -> 裁切 -> 预览
$PY $S/gem15_dem.py LUX out --layer dsm --bg white     # 5 波段 DSM + 白底预览
$PY $S/gem15_dem.py fr out --country FRA --adm1 "Grand Est"   # 一级行政区
$PY $S/gem15_dem.py bcn out --bbox "2.10,41.35,2.25,41.48"    # 任意矩形
$PY $S/gem15_dem.py area out --geojson my.json --mode full     # 自备边界 + 整片落盘
$PY $S/gem15_dem.py LIE out --no-clip                    # 不掩膜不裁切，保留完整矩形
$PY $S/gem15_dem.py AUT out --split auto                 # 大国自动分块（内存恒定、断点续跑、空块跳过）
$PY $S/gem15_dem.py 韩国 out --single                    # v2.0：整幅单文件出图（不分块、内存恒定、内置金字塔 COG）
$PY $S/gem15_dem.py --clean-cache                        # 清全部运行时缓存（无需位置参数）
$PY $S/selftest.py                                       # 离线回归（不联网）
```

用户没说清范围时问一句国家/地区名，或直接给 bbox 示例。默认图层 `dtm`
（裸地高程 1 波段）；`--layer dsm` 出 5 波段（dsm / 5/50/95 分位 / std）。

## 数据源

| 项 | 值 |
|---|---|
| 高程 | Google Research GEM-15 v1，15 m，EGM96 正高（EPSG:5773），单位米 |
| 数据 | GCS 公开 COG：`gs://climate-and-sustainability/gem-15`，免 API Key |
| 索引 | STAC `.../gem-15/stac/catalog.json` → 297 个 10° 组 → 1° item；技能完全由 STAC 链接驱动，不拼记忆 id |
| 图层 | `dtm`（1 波段）/ `dsm`（5 波段）；有效值外为 NaN |
| 边界 | geoBoundaries gbOpen ADM0 / ADM1（WGS84），本地缓存，四候选镜像回退 |
| 许可 | CC-BY 4.0，需署名「此数据集由 Google 制作」；已写入 GeoTIFF 标签与 manifest |

## 环境

优先用隔离环境（Windows）：
`C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
依赖见 `requirements.txt`（rasterio / numpy / shapely / Pillow）。
网络一律走 `curl` 子进程 + GET（本机 POST 会被网关拦截返回 504，大响应会被掐断）。

## 核心机制

**两层分片**：① 按 STAC 1° cell 分片（只取与 bbox 正面积相交的片，纯海域 cell 预过滤跳过）；
② 每片按 COG 窗口按需取数（`/vsicurl/` 只发所需 tile 的 Range 请求）。另有第三层
**内存分片**：逐片 read→paste→立即释放，常驻内存不随片数累加。

**下载模式**：默认 `window`（只按需读窗口）；`--mode full` 用单文件 `curl -C -`
断点续传把整片 COG 落盘缓存（重试按「连续零进展」计失败，不是按次数）。
full 模式分块前会**整区预下载**：把全部块需要的唯一 cell 去重后一次并发下载
（实测 8 路聚合 ≈ 单路 3.4x），后续各块全命中缓存。

**缓存复用**：window 模式发现本地已有整片 COG（full / 预下载留下）时直接读本地
（实测同窗口 **35.2x** 提速），命中判定做字节数 + rasterio 双校验，坏缓存回退远程。

**整幅单文件（`--single`，v2.0.0）**：先整区落盘所需 COG，再按全局网格**逐横带**
流式拼接（每带约 384 MB，内存恒定与成果总大小无关），逐片从原生 UTM 网格
bilinear 重投影贴入——**天然无分块接缝**，产出一整幅 COG。韩国整国 7.54 GB
成果实测 340 s 全自动完成。适用「要一个完整文件」的场景；要断点续跑/逐块浏览
则用 `--split auto`。

**内存预检**：读取任何像元前按 `像元数 × 波段数 × 4 B` 估算成果规模，超
`--max-output-mb`（默认 512）或物理可用内存 80% 即拒绝，并自动生成可执行的
`_split_plan.json` 分块方案；`--mode full` 另受 `--max-download-mb`（默认 2048）限制。

**可观测性（禁止静默失败）**：判空（`CoverageError` / `EmptyRasterError`）、规模超限、
缓存损坏、窗口未对齐、跨带降级等全部显式报错（exit 2）或打 `[warn]`，不生成空文件。

## 输出规格

| 项 | 值 |
|---|---|
| 主成果 | `<区域>_gem15_<layer>.tif`：**COG 格式**（Cloud Optimized GeoTIFF，LAYOUT=COG + 内置金字塔，Web/QGIS 可直接流式访问）、float32 / NaN（界外显式 NaN，无黑边）、deflate + tiled + BIGTIFF=IF_SAFER、predictor=3；`--no-cog` 可退回普通 GeoTIFF |
| 坐标系 | 默认 `auto`：同 UTM 带原生 15 m；跨带自动降级 EPSG:4326 并打 `[warn]` |
| 预览 | `<区域>_gem15_preview.png`：先降采样再渲染，2–98% 分位拉伸 + 山体阴影，极简标题 / 罗盘式指北针 / 图形比例尺 |
| 附带 | 边界 GeoJSON、分片清单 items.json、manifest（分片清单、耗时、CRS、高程统计、峰值估算、许可） |
| 缓存 | 默认 `D:/gem15-dem-cache`（D 盘不存在时回退技能内 `_cache` 并告警）；`--cache` 改路径；`--clean-tiles-only` 只清瓦片保留边界 |

主要参数速查：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--layer` | `dtm` | `dtm`（1 波段）/ `dsm`（5 波段） |
| `--mode` | `window` | `window` 按窗口取数 / `full` 整片落盘缓存 |
| `--crs` | `auto` | 同带原生 UTM；跨带降级 EPSG:4326 |
| `--split` | `off` | `auto` 自动分块 / `NxM` 手工指定；断点续跑、空块跳过 |
| `--single` | 关 | **整幅单文件出图**：不分块、逐带流式拼接、内存恒定；与 `--split` 同传时 --single 优先 |
| `--max-output-mb` | `512` | 成果数组预估上限；超限拒绝并生成分块方案 |
| `--max-download-mb` | `2048` | full 模式整 COG 落盘总量上限（发请求前校验） |
| `--no-clip` | 关 | 不做边界掩膜与裁切，保留完整矩形（注意近海块会带海底高程） |
| `--force-empty` | 关 | 清 `_empty.json` 空块标记并强制重判 |
| `--no-cog` | 关 | 跳过 COG 转换，保留普通 GeoTIFF（大国分块可省每块一次全文件拷贝时间） |
| `--bg` / `--plain` / `--title` / `--maxside` | — | 预览底图 / 去注记 / 标题 / 最大边长 |

## 脚本说明

| 脚本 | 职责 |
|---|---|
| `gem15_dem.py` | 一键入口，串全流程 + 写 manifest；预期错误统一 exit 2 |
| `resolve_region.py` | 名称 / ISO3 / bbox / GeoJSON → 边界（含排除区判定） |
| `stac_index.py` | STAC 索引 → 相交 1° cell（动态反推组名，带 proj 元数据供离线规划） |
| `fetch_tiles.py` | full 模式：单文件 `curl -C -` 续传 + 字节校验 |
| `build_mosaic.py` | 离线规划窗口 → 内存预检 → 流式拼接 → 掩膜 → GeoTIFF；window 模式缓存复用 |
| `preview_dem.py` | 先降采样再渲染的出版级预览 PNG |
| `selftest.py` | **150 项**离线回归（default 1.5.1 与 cnclimate 1.5.2 双环境均通过） |

## 常见坑

- **山区黑洞/黑斑**：GEM-15 对水体、冰雪、盐田等做 no-data 掩膜，属数据源特性
  （黑边才是 0 值，本技能界外写 NaN 已修）。预览透明区加 `--bg white` 更直观。
- **负高程不是 bug**：露天矿、圩田会真实低于海平面（德国实测 −295.68 m 为莱茵褐矿）。
  `--no-clip` 时近海块带海底高程，且有源侧异常深值（北海有成片 −246~−276 m），
  要纯陆地成果不要传 `--no-clip`。
- **绝对精度**：相对机载 LiDAR 有 -4.8 ~ +1.9 m 区域均值偏移，工程用途用控制点去偏。
- **大国**：加 `--split auto`；分块不减少总数据量，磁盘仍需约 32 GiB（deflate 后更小），
  输出与缓存放 D 盘。不要只提高 `--max-output-mb` 硬扛（物理内存预检会拒绝，防 OOM）。
- **边界下不下来**：geoBoundaries 的 GitHub 源间歇不可达，技能已四候选镜像回退 +
  内容校验；仍失败改用 `--bbox` / `--geojson`，缓存里已有边界时用 `--clean-tiles-only` 别全清。
- **打包 / 上传**：导出前删 `scripts/__pycache__/`（仅支持两级目录）；运行时缓存默认在 D 盘。

## 变更记录

- **v2.0.0（当前）**：**`--single` 整幅单文件出图**——逐带流式拼接（内存恒定、
  天然无接缝）、高程分位数直方图精确累计、默认 COG。韩国整国 7.54 GB 实测 340 s；
  修复两处真实组合错误（item bbox 度/米混用、掩膜几何未投影到成果 CRS——
  后者正是 project_geometries docstring 记录的历史教训）；--single 未显式设
  下载限额时自动放宽并 [warn]。新增 2 探针（网格对齐 / 最小端到端+解析值核对），
  **154/154** 双环境通过。
- v1.16.0：**默认输出 COG**——写完 GeoTIFF 后原地 CreateCopy 转为
  Cloud Optimized GeoTIFF（LAYOUT=COG + 自动金字塔 AVERAGE + predictor=3 保留），
  `--no-cog` 跳过；转换失败保留原 GTiff 并 `[warn]` 不静默。新增 2 项回归
  （cog_layout / cog_no_tmp_left），**152/152** 双环境通过。
- v1.15.2：文档全面精简——历史细节与逐版本实测数据收敛到 `CHANGELOG.md`，
  本文档只保留核心用法与机制。
- v1.15.1：全面文档更新（对照代码修正 8 类过期表述）。
- v1.15.0：window 模式本地缓存复用（实测 35.2x）+ 过时文案修正。
- v1.14.0：整区预下载（full 分块前唯一 cell 去重一次并发下载）。
- v1.13.0：全脚本审计修复 8 处 + `--force-empty`。
- v1.12.x：德国整国实测驱动——单文件 `curl -C -` 续传取代分段、镜像回退、
  空块兜底、MemoryError 子进程重试、predictor=3 写出、原地分位。
- v1.11.0 及以前：CLI 一致性、黑盒矩阵修复、中国范围判定、海域预过滤、
  大国分块、参数校验等，详见 `CHANGELOG.md`。

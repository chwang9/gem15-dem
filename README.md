# gem15-dem · 全球 GEM-15 高程下载器（15 m）

**版本 v2.0.0** · WorkBuddy 技能 · 中文名 / 版本号 / metadata / selftest 齐备

按**全球国家或地区**（国家名、ISO3、一级行政区、经纬度 bbox、自备 GeoJSON）
分片下载 Google Research **GEM-15** 15 m 高程，输出按边界裁切的 **GeoTIFF** 与
**预览 PNG**。免 API Key。历史变更与逐版本实测数据见 `CHANGELOG.md`。

---

## 1. 一句话能做什么

```bash
PY=C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe
$PY C:/Users/wangch/.workbuddy/skills/gem15-dem/scripts/gem15_dem.py 列支敦士登 out
```

产出：`out/列支敦士登_gem15_dtm.tif`、`out/列支敦士登_gem15_preview.png`、
边界 GeoJSON、分片清单与 manifest。

---

## 2. ⚠️ 覆盖限制（务必先读）

GEM-15 官方声明：覆盖 **60°S–84°N**，**排除中国（含台湾、香港、澳门）、俄罗斯、
蒙古、格陵兰、南极洲**；印度境内高程有政策性偏移。因此"全球任意国家"受数据源限制：

- 输入排除区（名称 / ISO3 / bbox / GeoJSON 中心点精判）→ 立即 `CoverageError`
  退出码 2，**不会**静默产出空 GeoTIFF；
- 台湾 / 香港 / 澳门（中文名或 `TWN/HKG/MAC` 直传）同样归并中国排除路径；
- 中国区域请改用 `cn-dem`（Copernicus GLO-30 30 m）。

其它：水体（+9 m 缓冲）、冰雪、盐田等像元被掩膜为 no-data（山区预览有黑斑，
属数据源特性）；相对机载 LiDAR 有 -4.8 ~ +1.9 m 区域均值偏移，绝对精度需控制点去偏。

---

## 3. 数据源与许可

| 项 | 值 |
|---|---|
| 高程 | Google Research GEM-15 v1，15 m 网格，EGM96 正高 EPSG:5773，单位 m |
| COG | `https://storage.googleapis.com/climate-and-sustainability/gem-15/`（公开，免 Key） |
| STAC | `.../gem-15/stac/catalog.json` → 297 个 10° 组 → 1° item |
| 图层 | `dtm` 1 波段（裸地）/ `dsm` 5 波段（dsm + 5/50/95 分位 + std） |
| 边界 | geoBoundaries gbOpen ADM0 / ADM1（WGS84） |
| 许可 | **CC-BY 4.0**，须署名「此数据集由 Google 制作」，已写入 GeoTIFF 标签与 manifest |
| 引用 | Batchu, V. V. 等 (2026). *GEM-15: Global Surface and Terrain Elevation Models (15 m)*. EarthArXiv, doi:10.31223/X55N75 |

---

## 4. 安装 / 环境

```bash
C:/Users/wangch/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pip install -r requirements.txt
```

依赖：`rasterio / numpy / shapely / Pillow`。网络全部走 `curl` 子进程 + GET
（本机 POST 会被网关拦截 504）。缓存默认 `D:/gem15-dem-cache`，D 盘不存在时回退
技能内 `_cache` 并告警。

---

## 5. 用法

```bash
S=C:/Users/wangch/.workbuddy/skills/gem15-dem/scripts

# 国家（中文 / 英文 / ISO3 均可）
$PY $S/gem15_dem.py 列支敦士登 out
$PY $S/gem15_dem.py LUX out --layer dsm        # 5 波段 DSM
$PY $S/gem15_dem.py LUX out --bg white --plain # 白底 / 无注记预览
$PY $S/gem15_dem.py LIE out --no-clip          # 不掩膜不裁切，保留完整矩形

# 一级行政区 / 任意范围
$PY $S/gem15_dem.py 法国 out --country FRA --adm1 "Grand Est"
$PY $S/gem15_dem.py bcn out --bbox "2.10,41.35,2.25,41.48"
$PY $S/gem15_dem.py area out --geojson my.json

# 下载模式与缓存
$PY $S/gem15_dem.py LIE out --mode full        # 整片 COG 落盘（curl -C - 续传）
$PY $S/gem15_dem.py LIE out --cache D:/my_cache
$PY $S/gem15_dem.py --clean-cache              # 清全部四类缓存
$PY $S/gem15_dem.py --clean-tiles-only         # 只清瓦片，保留索引与边界

# 大区域：默认 512 MB 上限，超限在读像元前报错并自动生成 _split_plan.json
$PY $S/gem15_dem.py AUT out --split auto       # 自动分块 + 断点续跑 + 空块跳过
$PY $S/gem15_dem.py 韩国 out --single          # v2.0：整幅单文件（不分块、内存恒定）

# 离线回归（不联网）
$PY $S/selftest.py
```

主要参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--layer` | `dtm` | `dtm`（1 波段）或 `dsm`（5 波段） |
| `--mode` | `window` | `window` 按 COG 窗口取数；`full` 整片落盘缓存 |
| `--crs` | `auto` | 同 UTM 带原生 15 m；跨带自动降级 EPSG:4326 |
| `--workers` | `4` | cell 级并发数 |
| `--buffer` | `0` | 扩大取数 bbox 的米数（不改变掩膜边界） |
| `--no-clip` | 关 | 不做边界掩膜与裁切，保留完整矩形 |
| `--bg` | `transparent` | 预览底图 `transparent` / `white` |
| `--plain` | 关 | 预览图去掉标题 / 指北针 / 比例尺 |
| `--title` / `--maxside` | 区域名 / 1600 | 预览标题与最大边长 |
| `--max-output-mb` | `512` | 成果数组预估上限，另对照物理可用内存，防止 OOM |
| `--split` | `off` | `auto` 自动分块 / `NxM` 手工指定；内存恒定、断点续跑、空块跳过 |
| `--single` | 关 | **整幅单文件出图**：逐带流式拼接、内存恒定、天然无接缝；韩国 7.5 GB 实测 340 s |
| `--max-download-mb` | `2048` | `full` 模式整 COG 落盘总量上限（发请求前校验）；--single 未显式设置时自动放宽 |
| `--force-empty` | 关 | 清 `_empty.json` 空块标记并强制重判 |
| `--no-cog` | 关 | 跳过 COG 转换，保留普通 GeoTIFF |
| `--refresh` / `--clean-cache` / `--clean-tiles-only` | — | 重建边界缓存 / 清全部缓存 / 只清瓦片 |

> ⚠️ 边界源（GitHub）不可达时别用 `--clean-cache` 全清，用 `--clean-tiles-only`，
> 或改用 `--bbox` / `--geojson` 绕开边界下载。

---

## 6. 输出规格

| 项 | 值 |
|---|---|
| GeoTIFF | **COG 格式**（LAYOUT=COG + 内置金字塔 AVERAGE，可 Web 流式访问；`--no-cog` 跳过）、float32、`nodata=NaN`（界外显式 NaN，无黑边）、deflate + tiled、BIGTIFF=IF_SAFER、predictor=3 |
| 坐标系 | 同带：原生 UTM（15 m 正方形像元）；跨带：EPSG:4326 自动降级并 `[warn]` |
| 预览 PNG | RGBA，先降采样再渲染；2–98% 分位拉伸 + 山体阴影；极简标题、罗盘式指北针、图形比例尺 |
| 附带文件 | `*_boundary.geojson`、`*_gem15_items.json`、`*_gem15_manifest.json`（分片清单、耗时、CRS、高程统计、峰值估算、许可） |

---

## 7. 核心机制

**两层分片**：① 按 STAC 1° cell 分片（只取正面积相交片，纯海域 cell 预过滤跳过）；
② 每片按 COG 窗口按需取数（`/vsicurl/` Range 请求）。另有第三层内存分片：
逐片 read→paste→立即释放，常驻内存不随片数累加。

**下载**：`full` 模式单文件 `curl -C -` 断点续传（重试按「连续零进展」计失败）；
分块前整区预下载（唯一 cell 去重后一次并发，8 路聚合 ≈ 单路 3.4x）。
`window` 模式命中本地整片缓存时直接读本地（实测 35.2x 提速），坏缓存回退远程。

**预检**：读取任何像元前校验下载总量、成果规模、物理内存、磁盘空间，超限拒绝并
自动生成 `_split_plan.json`；所有失败路径显式报错或 `[warn]`，禁止静默产出。

**整幅单文件（`--single`，v2.0.0）**：先整区落盘所需 COG，再按全局网格逐横带
流式拼接（每带约 384 MB，内存与成果总大小无关），逐片从原生网格重投影贴入，
天然无分块接缝；高程分位数用直方图增量累计。韩国 7.54 GB 成果实测 340 s 全自动。

关键实测结论（详见 CHANGELOG）：小 JSON 索引并发 16 最优；GDAL `/vsicurl/`
窗口并发反优化（慢 2.6x）保持串行；本地窗口读 vs 远程 **35.2x**。

---

## 8. 目录结构（两级，符合 WorkBuddy 上传规范）

```
gem15-dem/
├── SKILL.md            # frontmatter：name / version / display_name / description_* / agent_created / argument-hint
├── README.md           # 本文件
├── CHANGELOG.md        # 结构化变更表 + 逐版本实测数据
├── _user_meta.json     # name / display_name / version / installedAt / source
├── requirements.txt
└── scripts/
    ├── gem15_dem.py        # 一键入口
    ├── resolve_region.py   # 国家 / 行政区 / bbox / geojson -> 边界
    ├── stac_index.py       # 官方 STAC -> 相交 1° cell
    ├── fetch_tiles.py      # full 模式：单文件 curl -C - 续传
    ├── build_mosaic.py     # 离线规划窗口 / 内存预检 / 流式拼接 / 掩膜 / GeoTIFF
    ├── preview_dem.py      # 先降采样再渲染的预览 PNG
    ├── countries_zh.json   # 中文名 / 英文名 / ISO3 映射
    ├── fixtures_stac.json  # 真实 STAC 样本（离线回归用）
    └── selftest.py         # 154 项离线回归（双环境通过）
```

> 导出前删除 `scripts/__pycache__/`（三级目录会触发"Skill 包仅支持两级目录结构"）。
> 入口脚本已设置 `sys.dont_write_bytecode=True`。

---

## 9. 致谢与署名

高程数据由 **Google** 制作（GEM-15，CC-BY 4.0）；边界来自 **geoBoundaries gbOpen**。
技能参考了本机 `cn-dem` 技能的规范、分片下载与黑边修复经验。
使用成果时请保留 GeoTIFF 标签中的 `GEM15_ATTRIBUTION` 与许可信息。

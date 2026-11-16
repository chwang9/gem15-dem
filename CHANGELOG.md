# 变更日志 · gem15-dem

版本号在 `SKILL.md` frontmatter、`README.md`、`_user_meta.json` 与本文保持一致。

## v2.0.0（2026-11-16）· `--single` 整幅单文件出图（152 → 154 项回归）

把韩国实测验证过的流式整幅拼接固化为技能正式能力——大版本核心特性：
**一条命令产出一整幅完整国家 GeoTIFF（不分块、内存恒定、无接缝）**。

| 项 | 实测 / 设计 |
|---|---|
| 新增 `build_streaming()` | 全局网格逐横带处理（自适应带高，默认 384 MB 预算），与带相交的本地 COG 逐片 bilinear 重投影贴入（`init_dest_nodata=False` 保已写有效值）+ 边界掩膜 + 分带写出。内存恒定约 1~1.2 GB，与成果总大小无关；逐片从原生网格直投全局网格，**天然无分块接缝相位问题** |
| 高程统计 | 分位数用直方图增量累计（[-1200, 9000] m、0.5 m 分箱），不复制大数组 |
| CLI | `--single` 标志：先整区落盘所需 COG，再流式拼接；与 `--split` 同传时 --single 优先并 `[warn]`；不受 `--max-output-mb` 约束（内存恒定），磁盘预检按下载量×3.85 估算 |
| 下载限额 | --single 本质是整区落盘：仅当用户**未显式**设 `--max-download-mb` 时自动放宽默认 2048 并 `[warn]`（韩国 2637 MB 即被默认值拦下，实测驱动）；显式设置则尊重并可能报错 |
| 实测中修复的两个真实 bug | ① item bbox（度）与目标网格（UTM 米）单位混用，行列映射全部落空 -> 静默空成果；② 掩膜几何未投影到成果 CRS -> `geometry_mask` 整体错位裁出全 NaN——**正是 `project_geometries` docstring 记录的历史教训**，本次改为直接复用该函数 |
| 真实验证（韩国 --single） | 26 片缓存全命中（0 下载），整幅 40300×46789 @ 4326，**340 s 全自动**产出一整幅 + 预览 + manifest；有效 523,333,081 像元，与外部验证脚本结果（523,329,840）偏差 0.0006% 互相印证；预览目检半岛/济州/郁陵/独岛完整、无接缝无空洞 |
| 回归 | 新增 `stream_grid_align`（网格对齐纯函数）+ `stream_e2e_tiny`（2 片相邻 UTM 合成端到端：LAYOUT=COG + 接缝带 97.7% + **中心像元解析值误差 <0.5 m**），**154/154** 双环境通过 |

## v1.16.0（2026-11-16）· 默认输出 COG（150 → 152 项回归）

按用户要求「技能转出来的 GeoTIFF 要转为 COG 格式」。先实测证实再实现：

| 项 | 实测 / 设计 |
|---|---|
| 环境能力探针 | 双环境 GDAL 3.12：`rasterio.shutil.copy(driver="COG")` 后 `LAYOUT=COG`、**predictor=3 保留**、自动金字塔（AVERAGE）且可读——最小合成 1024² 全链路验证 |
| 实现 | `build_mosaic.to_cog()`：write_geotiff 后 CreateCopy → `.cog.tmp.tif` → `os.replace` 原子替换；`build()` 新增 `cog=True` 参数；CLI `--no-cog` 跳过；`gem15_dem` ctx + `_tile_argv` 转发（分块子进程同样生效） |
| 失败路径 | 转换失败保留原 GTiff + `[warn]`，不打断主流程、不静默；临时文件清理无残留 |
| 默认开启的影响 | 每块成果多一次全文件拷贝（德国量级单块约 +30~60 s）+ 金字塔体积（约 +35%）；换来看图/Web 端流式访问能力。大国批量可用 `--no-cog` 省时 |
| 回归 | 新增 `cog_layout`（布局/predictor/金字塔断言）与 `cog_no_tmp_left`（无临时文件残留）2 探针，**152/152** 双环境通过 |
| 附加产出 | 上一轮的韩国整幅 tif 已原地转 COG：LAYOUT=COG、predictor=3、金字塔 [2,4,8,16,32,64,128]、1.43→1.96 GB、195 s，抽样窗口仍 99% 有效 |

## v1.15.2（2026-11-16）· 文档精简：聚焦核心技能（纯文档，无代码变更）

按用户要求「文档里面不需要很多描述聚焦核心技能就行」，把 SKILL.md（586 → 137 行）与
README.md（575 → 145 行）大幅精简：

| 处理 | 内容 | 去向 |
|---|---|---|
| **删除** | SKILL.md「环境」节的 v1.2–v1.12 逐版本叙事（约 45 行）；v1.2–v1.7 六张历史实测大表（约 100 行）；脚本说明里的逐版本变更流水（每脚本只留一句职责）；「判空与可观测性」的 4b–4h 逐版本条目（收敛为一段原则）；变更记录压成单行摘要列表 | 历史细节全部已在 `CHANGELOG.md` 与历史版本条目中，无信息丢失 |
| **删除** | README 顶部 v1.12–v1.15 逐版本 blockquote（约 110 行）；第 7 节全部历史探针表；第 8 节 v1.1–v1.15 逐版本专项验证表（约 240 行） | 同上，收敛到 CHANGELOG |
| **保留** | frontmatter、覆盖限制、快速上手 / 用法、数据源与许可、输出规格、参数表、核心机制（分片 / 下载 / 缓存复用 / 预检 / 可观测）、脚本职责一句话表、常见坑、目录结构、署名 | — |
| **新增** | SKILL.md「核心机制」一节：两层分片 + 内存分片、window/full 下载模式、整区预下载、缓存复用、内存预检、禁止静默失败——原先散落在长文里的机制集中一处 | — |

文档结论：SKILL.md 现在是「怎么用 + 核心机制 + 关键限制」三件事；CHANGELOG.md
承担全部历史与实测数据。**150/150 回归通过**（版本断言同步 1.15.2）。

**v1.15.2 真实验证（韩国整国，2026-11-16）**：

| 项 | 结果 |
|---|---|
| 范围 | KOR ADM0 bbox (124.61, 33.20, 130.92, 38.62)，含西端远岛与独岛；26 个 1° cell（约 2.7 GB） |
| 磁盘预检 fail-fast | 输出放 C 盘时预估 10.5 GB > 可用 10.2 GB，**写大文件前**拒绝并给可执行建议（改 D 盘后通过）——防线首次在真实场景触发 |
| 跨带降级 | 韩国 ADM0 横跨 UTM 51/52 两带 → 自动降级 EPSG:4326 + `[warn]`，符合设计 |
| 整区预下载 | 26 片一次并发落盘（cache 2.6 GB），20 块 build 全部 0 新下载；未命中缓存的单块对照读取 3.3 s vs 命中 0.0 s |
| 分块结果 | 5×4=20 块：18 ok + 2 空块（r2c0 黄海、r2c4 东海纯海域，判空兜底 + 标记 + 继续） |
| 断点续跑 | 重跑 20 块 0.01 s 全部秒过（18 cached + 2 cached_empty） |
| 验收（通用脚本） | 索引完整、18/18 落盘可读、nodata=NaN、并集 bbox 完全盖住计划 bbox、13 相邻对重叠 259~631 px |
| 接缝 | 9 对接缝 mean 0.009~1.28 m；8 对 max ≤ 0.84 m（与德国 0.089 同量级）；r2c2↔c3 max 14.6 m 经自身错位 1px 对照（同处 max 10.4~41.8 m）证实为**陡坡亚像素相位效应**（太白山脉南段），非数据缺陷。验收判据修订：接缝差 ≤ 2× 自身错位量级（绝对 1 m 阈值不适用于陡峭地形） |
| 预览抽查 | 山体阴影 + 高程配色正常、水系掩膜清晰（黑色为 GEM-15 水体 no-data，数据源特性）、指北针/比例尺/极简标题齐备，无渲染缺陷 |

注：首轮长跑进程退出码非 0 为宿主 safe-delete 守卫在末尾清理阶段拦截所致
（split manifest 已写、数据完整，重跑 EXIT=0 验证），非技能缺陷。

## v1.15.1（2026-11-16）· 全面文档更新（纯文档与版本号，无代码变更）

逐条对照 v1.15.0 代码通读 SKILL.md（576 行）与 README.md（556 行）全文，证实并修正 8 类过期 / 错误：

| # | 位置 | 问题 | 实测证据 | 修正 |
|---|---|---|---|---|
| 1 | SKILL.md selftest 交叉验证段 | 写「default 1.5.1 环境已不在本机」 | 实测 default 环境（rasterio 1.5.1 / shapely 2.1.2）在本机且 **selftest 150/150 通过** | 双环境复核结论恢复：v1.15.0 起 default（1.5.1）与 cnclimate（1.5.2）均 150/150 |
| 2 | SKILL.md 判空段 | 「分段字节校验失败」（分段机制 v1.12.0 已删） | — | 改「续传字节校验失败」 |
| 3 | SKILL.md 两处历史观察项 | 「full 段下载缺 -L」「STAC 相对 href」未标注已修 | v1.8.0 已修（README 已有删除线标注，SKILL 漏同步） | 补「v1.8.0 已修」标注 |
| 4 | SKILL.md 常见坑 | 「C: 实测仅余 10 GiB」机器专属过时数字 | 当前 C 盘余量已远小于此 | 改「输出目录与缓存请放 D 盘（本机 C 盘空间紧张）」 |
| 5 | SKILL.md v1.15.0 条目 | 头部写「142→150 项回归两轮」，结尾写「146 → 150」，口径不一 | v1.15.0 实际 146→150 | 统一为 146 → 150 |
| 6 | SKILL.md 脚本说明 | gem15_dem / build_mosaic / preview_dem 三条缺 v1.12.2–v1.15.0 行为 | — | 补：空块两支兜底 / MemoryError 子进程重试 / force-empty / 整区预下载 / CLI 数值校验 / window_sources / 单行列预览 GemError |
| 7 | README 验证节 | ①节标题停在「v1.0.0 基线 + v1.1.0 / v1.2.0」；②v1.12.2 验收表 selftest 写 142/142（当时实为 134/134）；③缓存占用快照被读成当前状态；④目录结构写「selftest 94 项」；⑤缺 v1.13–v1.15 专项验证 | v1.12.2 时 selftest=134（132→134） | ①标题改「按版本分节」；②改 134/134 并注明；③标注历史快照；④改 150 项；⑤新增 v1.13–v1.15 验证表（35.2x 基准 / A/B / 预下载五场景 / 审计 8 条 / 阶梯） |
| 8 | 版本号 | 文档修正发布 | — | 1.15.1（五处一致） |

回归：**150/150**，default（rasterio 1.5.1）与 cnclimate（rasterio 1.5.2）双环境各跑一次。

## v1.15.0（2026-11-16）· 文案更新 + window 模式本地缓存复用（146 → 150 项回归）

### 代码优化（先实测证实再改）

| # | 发现 | 实测证据 | 修复 |
|---|---|---|---|
| 1 | window 模式即使本地已有整片 COG（full 模式或 v1.14.0 整区预下载留下）仍走远程 `/vsicurl/` | 同一 `w8s53` COG 的 2048² 窗口：本地 **0.21 s / 75.9 MB/s** vs 远程 **7.42 s / 2.2 MB/s**，**35.2x 差距**（3 轮交替实测，远程含 VSI 缓存预热） | 新增 `window_sources()`：命中判定复用 `_cache_ok`（字节数与 STAC `file:size` 一致 **且** rasterio 可打开）→ 读本地；size 不符或打不开 → 回退远程 + `[warn]`，坏缓存不误用；命中数入 `stats["window_cache_hits"]` 可观测 |
| 2 | （顺带）缓存里残留 v1.12 前分段机制的死目录 `tiles/foo/w9s47.parts/` | 分段机制已在 v1.12.0 整体移除，该目录永不再被读写 | 手动清除；clean_cache 本可覆盖，非代码缺陷 |

**真实端到端 A/B**（2 片同批次、同参数）：命中缓存的 `w8s53` 窗口读取 **0.0 s**，
未命中的 `w8s54` 走远程 **3.3 s**，成果 GeoTIFF 正常产出。

### 文案更新（通读全量文档后逐条核对代码证实）

| # | 位置 | 过时内容 | 修正 |
|---|---|---|---|
| 1 | SKILL.md `description_en` | "segmented resumable download"（分段已在 v1.12.0 删除） | "single-file resumable download" |
| 2 | SKILL.md 流程示例 | `fetch_tiles.py … --chunk-mb 16`（该参数已废弃，传入会触发 `[warn]`） | 去掉参数 |
| 3 | SKILL.md 脚本说明 | selftest 写「**102 项**」、版本阶梯停在 131 | **150 项**；阶梯补 v1.12.x–v1.15.0（131→142→146→150） |
| 4 | README 实测表 | 「full 模式采用**串行分段** + 每段字节校验 + 单文件续传」自相矛盾 | 改为「单文件 `curl -C -` 续传 + 字节校验」并注明旧表述已修正 |
| 5 | README selftest 阶梯 | 停在 v1.12.0 131/131 | 补 v1.13.0 142/142、v1.14.0 146/146、v1.15.0 150/150 |
| 6 | fetch_tiles `USAGE` | 仍在广告 `--chunk-mb 16` | 从用法行移除并注明已废弃 |

### 技能优化

- `argument-hint` 补 `--force-empty`（v1.13.0 引入的用户开关此前未进提示词）。

### 回归

- selftest **146 → 150 项**（新增 `window_cache_hit` / `window_cache_size_mismatch` /
  `window_cache_missing` / `v115_signature`），全量通过。

## v1.14.0（2026-11-16）· 运行时性能优化：整区预下载（142 → 146 项回归）

德国整国 48 块实测归因：**下载占 build 墙钟 73.2%**，而每块只有 0–4 个新 cell，
块内 `workers=8` 大部分空转，实测并发加速仅 **2.08x**；93 个唯一 cell 被 244 次
块引用（83 片被 2–4 块共用）。本轮把下载从「逐块各自下」改为「整区一次并发下」。

### 改动清单

| # | 改动 | 实测证据 | 效果 |
|---|---|---|---|
| 1 | 新增 `prefetch_for_split()`：full 模式分块 build 前，把**整区未完成块**需要的唯一 cell 去重后一次 8 路并发下载 | 8 路 GCS 聚合 **25.34 MB/s ≈ 单路 3.4x**（4 MB × 8 路实测）；德国场景 93 片去重后一次下完，后续各块 `download_items` 全部命中缓存秒过 | 消除块间重复下载、抬高并发利用率 |
| 2 | 断点续跑安全：只处理未完成块（缺 tif+manifest，或空块标记在 `--force-empty` 下待重判），已建成果不重下 | selftest `prefetch_skips_done`：全完成 → 0 次 `download_items` 调用 | 重跑不浪费流量 |
| 3 | 仅 `mode=full` 生效；window 模式不落盘缓存，预下载空操作 | selftest `prefetch_window_noop`：0 次请求 | 不破坏 window 语义 |
| 4 | 预下载的批量校验传 `max_download_mb=inf` 跳过整批限额 | selftest `prefetch_budget_no_reject`：用户设 5000 MB 不再被整区 12 GB 误拒；磁盘预检与逐块 build 的本块校验均保留 | 修「整区/逐块口径不一致误拒」 |
| 5 | 下载失败不静默：保留已下缓存供续传并抛 `GemError` | 复用 `_resume_download` 单文件续传（v1.12.x） | 断点安全 |

### 验证

- selftest **142 → 146 项**（新增 `prefetch_unique_cells` / `prefetch_skips_done` / `prefetch_window_noop` / `prefetch_budget_no_reject`），全量通过。
- 真实验证：6 片共约 **926 MB** 并发下载一次完成，落盘与日志正常。
- selftest 签名测试的 10 处版本文案统一为 1.14.0（断言此前已对齐，仅消息滞后）。

## v1.13.0（2026-11-15）· 全脚本审计 + 逐条实测：8 处修复（134 → 142 项回归）

德国整国实测收尾后，按「先实测再修复」做了一轮全脚本审计：两路独立审计共报 11 条候选，
每条用最小合成输入或真实 CLI 实测核对——**证实 8 条、证伪 2 条、误报 1 条**，只修证实的 8 条。

### 修复清单

| # | 问题 | 实测证据 | 修复 | 严重度 |
|---|---|---|---|---|
| 1 | auto 分块 `gx` 触 64 上限截断后 `gy` 未回补 | 狭长 bbox `k=100 → gx=64, gy=1`，est=600 MB > max 512，方案自相矛盾 | 用截断后 gx 重算 gy（gy=2、est=300） | 高 |
| 2 | build_mosaic 的 5 数值参数 + mode 零校验 | `--workers abc` 等 6 个非法入参实测 rc=1 + traceback | 加 `num()` 前置校验，实测全部 rc=2 显式报错 | 中 |
| 3 | `percentiles_inplace` 完整定义两次 | AST：489/517 行两份，后者覆盖前者 | 删重复定义 | 低 |
| 4 | 单行/列 DEM 预览 `np.gradient` 崩溃 | (1,10)/(10,1)/(1,1) 全抛 ValueError | `render` 入口前置 GemError | 低 |
| 5 | GeometryCollection 合法 GeoJSON 被误判「无坐标」 | 含 2 个 Point 的 GC 抛 RegionError | 递归展开 `geometries[]` 算 bbox | 中 |
| 6 | 空块标记永久锁定、无刷新手段 | `refresh` 只用于 STAC，empty 分支无条件跳过 | 新增 `--force-empty` 清标记重跑 | 中 |
| 7 | 清理超预算后 282 个 0 B 空壳重复无效写 | 3 轮清理空壳数恒定 282 | `dispose_tree` 跳过已 0 B 的、日志明确；不动删除语义 | 低 |
| 8 | selftest `fetch_cli_validated` 偶发 rc=1 | 真凶 `OpenBLAS error: Memory allocation still failed after 10 retries`（此前误判为沙箱拦截） | 黑盒 CLI 子进程限 BLAS/OpenMP/MKL 线程数=1 | 低 |

### 证伪 / 误报（未改）

- **跨目录删除累计触发守卫**（审计猜想）：实测 4 子目录各 30 文件共 120 个一次性真删，rc=0、无 SAFE_DELETE → 不成立，不动。
- **磁盘预检下载量高估近 2 倍**（审计发现 2/3）：`download_total_mb=16404` 是**整区未压缩上界**，
  实际 tif 8563 MB 是 deflate+predictor=3 压缩后成果，二者口径本就不同，README 早已注明
  「16404 是按未压缩 4 B/px 的保守上界」→ 误报，不动。

### 回归

selftest 134 → **142/142**（新增 8 条：auto_split_refit_gy / build_cli_validated /
no_dup_percentiles / preview_thin_explicit / geometry_collection_bbox / force_empty_refresh /
dispose_shell_skip / v113_signature），落盘日志连跑 3 次全通过。

## v1.12.2（2026-11-15）· 德国 48 块收尾：纯海域块中断整批 + 长跑进程内存耗尽

触发：v1.12.1 修完后继续跑德国 48 块批，先在第 42 块 `MemoryError` 中断、
再在第 43 块 `CoverageError` 中断。两次都**不是数据问题，是"批量调度没兜住"**。

| # | 问题 | 实测（德国 6×8 = 48 块） | 修复 | 探针 |
|---|---|---|---|---|
| 1 | **STAC 阶段判空的纯海域块会中断整批** | 第 43 块 r7c0（54.09–55.06N / 5.87–7.40E，纯北海）STAC 返回 0 片 → `CoverageError` 直穿到主入口 → 整批退出，**前 42 块白跑约 2 小时**（成果虽在，但批处理没走完、也没有汇总 manifest）。v1.7.0 立的"空块不中断整批"只兜住了取数**之后**才判空的 `EmptyRasterError`，漏了 STAC 阶段就判空的这一支 | `run_split` 新增 `except CoverageError` → `_mark_empty()` 写 `_empty.json` + `status=empty`、`reason=no_stac_tiles` 后**继续**；`EmptyRasterError` 分支改用同一个 `_mark_empty()` 助手 | `split_coverage_empty_continues` |
| 2 | **长跑进程内存耗尽后无自愈** | 第 42 块 reproject 需要 `(1, 7096, 5097) float32` = **138 MiB** 时分配失败（机器 32 GB、当时可用 8.6 GB；前 41 块全部成功）。旧实现在此直接抛 GemError 中断，只能靠人重跑。**决定性证据**：同参数的全新进程随后 42.9 s 就把这块跑完了 —— 长跑进程的虚拟内存碎片与已提交页不会因 `gc.collect()` 归还 | `_retry_tile_in_child()`：捕获 `MemoryError` 后**换全新进程重跑该块**（`sys.executable` + 同一套 `_tile_argv`，1 h 超时），成功则记为 `ok`（note 标明"由新进程重试完成"）并继续；仍失败才抛原来的可执行 GemError，并附子进程末尾 3 行输出 | `split_oom_child_retry`、`split_memory_error_actionable`（补子进程 mock） |

附带：selftest 里 `64b` 探针**重复存在两份**（同一 check 跑两遍），本次删除重复项；
新增的重试路径会让第二份真的起子进程并把那块跑成功，导致断言拿不到异常 —— 属
"重复代码 + 新路径"组合出来的假失败。

### 48 块整批最终验收（汇总 manifest 逐条核对）

| 项 | 结果 |
|---|---|
| 清单条目 / 计划块数 | 48 / 48，index 无缺失、无重复 |
| 状态分布 | `ok` 5 + `cached` 42 + `empty` 1（r7c0 纯北海）= 47 有效块 |
| 成果总量 | **8563.6 MB**（清单预估 16404 MB；预估按未压缩 4 B/px，实际 deflate+predictor=3 约 2.2 B/数值像元） |
| 网格一致性 | 全部块步长 0.000135656°，集合只有一个值 |
| 覆盖完整性 | 成果范围 L5.7898 B47.2317 R15.0543 T55.1127，**完全覆盖**计划 bbox，且相邻块**全部有重叠**（无缝隙） |
| 接缝数值一致性 | 19 条接缝各抽 4000 个同坐标点：\|Δh\| 均值 0.003–0.089 m、p99 ≤0.544 m、<1 m 占 99.6–100% |
| 墙钟 | 首轮 1h58m（41 块后中断）+ 续跑 7m52s（5 块 + 1 空块 + 42 块断点跳过）；缓存 12.2 GB / 成果 8.5 GB |
| 负高程核查 | r03c00 −295.68 m = 莱茵褐矿 Hambach 坑底（源值 −51.77 m 起、坑深约 293 m）；r06c01 −275.41 m = 汉堡湾外海（源值 −276.92 m，GEM-15 近海海底值）—— 均忠实于源，不是重投影坏值 |

### 回归

selftest 132 → **134/134**（新增 `split_coverage_empty_continues`、`split_oom_child_retry`；
`split_memory_error_actionable` 补子进程 mock 并断言新增的重试说明）。

## v1.12.1（2026-11-15）· 德国 48 块续跑暴露：慢链路续传被「次数预算」误杀 + 重叠量文档失真

触发：继续跑 v1.12.0 修完的德国整国批（48 块），日志里出现
`w14s48 第 2 次尝试补齐（0 -> 164934753 B）` 与 `w15s47/w15s48` 连续 3 次尝试。
逐个查实后确认：**续传本身没坏，是重试预算的口径错了**，而且贴着失败边缘。

| # | 问题 | 实测（德国 6×8 整批日志） | 修复 | 探针 |
|---|---|---|---|---|
| 1 | 续传重试预算按**尝试次数**而非**进展**计费，慢链路上等价于给单文件设字节上限 | 单连接 0.35–0.79 MB/s × `--max-time 150` = 每次只回 52–118 MB，而单 cell 有 **156–176 MB**；`w15s47`（53.0→166.5→175.7 MB）与 `w15s48`（167.1→167.9 MB）都已用满第 3 次。旧条件 `attempts <= retries` 只给 **4 次**机会，再有一次带宽波动就 `DownloadError` —— 而分块批是**串行**的，一个 cell 失败等于 48 块跑到一半全废（已建块虽能续跑，但整批中断） | 预算改为「连续零进展次数」：有进展就继续（`stalled` 清零），连续 `retries+1` 次零进展才判定失败，另设 `max_attempts=16` 兜底防病态死循环 | `resume_progress_budget`（新增）/ `resume_keeps_partial`（按新语义重写：零进展 3 次放弃、已收字节保留、0 次 unlink） |
| 2 | 补齐日志打印的是**进入函数时**的字节数，读起来像整块重下 | `w14s48 第 2 次尝试补齐（0 -> 164934753 B）`：实际第 1 次已收 62.9 MB，第 2 次是**从 62.9 MB 续到** 164.9 MB，`0` 只是入参 `resumed_from`。真按日志理解会误判「续传无效」而去改下载机制（本次差点这么干） | 改成「本次新增 X B，共 Y/Z B；进入本函数时已有 N B」，零进展时另附「连续 N 次零进展」 | 同 #1（探针断言新文案） |
| 3 | 分块计划/汇总的说明写「块间可能有 ≤PAD(48) 像元重叠」，与实测差一个量级 | 相邻块实际重叠：c00∣c01 **586** 像元、c01∣c02 **164**、c02∣c03 **545**、c03∣c04 **982** 像元。成因：块输出范围取「该块源窗口并集」（`--no-clip` 保留完整拼接网格），比计划 bbox **每边最多宽约 0.067°（≈490 像元 / 5 km）**；**全量实测 19 条接缝**：水平 165/546/557/564/574/587/607/626/983/1000 像元、垂直 233/241/262/263/535/672/689/689 像元，PAD(48) 只是源窗口外扩像素，两者不是一个东西 | `split_plan` / `split_summary` 的 `note` 按实测改写，明确「165~1000 像元、重叠区同坐标高程差均值 ≤0.09 m、拼接时任取一块」 | 无（文案，随 manifest 落盘可核） |

### 本轮实测数据（德国 DEU ADM0，48 块批）

- **接缝一致性**：相邻块重叠区内各抽 4000 个**同地理坐标**随机点比对，|Δh| 均值
  **0.011–0.074 m**、p99 ≤ 0.478 m、max ≤ 5.92 m，<1 m 占 **99.8–100%**
  （差异来自陡坡上两次独立重采样，非拼接错位）→ 任取一块拼接无跳变。
- **压缩参数确实落盘**：`IMAGE_STRUCTURE` 读回 c02/c03/c04 有 `PREDICTOR=3`
  （c00/c01 是改参数前写的，无 PREDICTOR）。按**有效像元**计字节：
  3.24 / 3.22 B/px → **2.24 / 2.13 / 2.19 B/px**（体积 −32%）。
  注意 `ds.profile` 不回读 `predictor`/`zlevel`（GDAL 只当创建选项），
  核对必须读 `tags(ns='IMAGE_STRUCTURE')`。
- **内存回收有效**：`gc.collect()` 后进程 RSS 回落到 **91.5 MB**，本批峰值
  **1202 MB**（旧实现在第 3 块就 `MemoryError`）。
- 单连接吞吐方差极大：同一批 cell 里 156 MB 有一根连接 **79 s 跑完（1.97 MB/s）**，
  也有 150 s 只走 53 MB（0.35 MB/s）——按平均值给 `--max-time` 定值必然踩坑，
  这也是 #1 改成按进展计费的原因。

### 回归

selftest 131 → **132/132**（新增 `resume_progress_budget`，按新语义重写
`resume_keeps_partial`）。

## v1.12.0（2026-11-15）· 大国实测（德国 93 片 / 12.5 GB）：边界镜像、单文件续传、删除预算、写出优化、内存

触发：用户「找个大点国家测下，并修复问题」。以**德国**（DEU ADM0，bbox 9.17°×7.79°，
跨 3 个 UTM 带，93 片 COG / 12482 MB，`--split auto` 6×8 = 48 块）做真跑。
5 个问题**全部只有大国 / 长跑才暴露**，小国（列支敦士登 1 片）永远碰不到。

| # | 问题 | 修复前实测 | 修复 | 探针 |
|---|---|---|---|---|
| 1 | 边界源 GitHub **间歇性**不可达，且全程只试一个地址 | 德国首次运行：同一 geoBoundaries URL 连续 4 次 **21 s 连接超时（code=000）**，纯等待 **92 s** 后整国流程失败；一分钟后同 URL 返回 200 / 2.8 MB / 3.3 s。镜像实测：jsDelivr 本机 immediate 000（被拦），**ghproxy 返回 200 但只有 132 B 错误页** | `github_mirror_urls()`（传地址 + raw 子域 + web raw + jsDelivr + ghproxy 去重展开）+ `curl_get_mirrors()`（逐候选尝试、**校验内容**、主地址失败时标注用了哪个镜像）；`curl_get`/`curl_bytes` 增 `--connect-timeout`（`CONNECT_TIMEOUT=12`），不可达主机 ~12 s 失败而非耗满 `--max-time`；全失败时逐主机列出原因 | `mirror_urls_expand` / `connect_timeout_applied` / `mirror_fallback_validated` / `boundary_error_hint`（按新契约同步） |
| 2 | 分段下载被截断后**整段重下**，白丢已收字节 | 12.5 GB 下载中 16 MiB 段频繁 `code=206 但只收到 60–95%`（got=16464595/16777216、9625590/16777216…），旧实现整段清零重下 | 先改为段内续传（保留已收字节）；随后被 #3 整体取代 | `resume_download_continues` |
| 3 | **宿主批量删除守卫会杀进程**（大国必死） | 德国第 3 片合并成功后清理 `.parts`（11 段 + 11 个续传临时文件）时：`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":68,"threshold":50,"scope":"turn"}` → **进程被直接终止（exit 1、无 traceback）**。守卫按**进程累计**计数（22+22+23+1=68），93 片 × 23 条目 = 2000+ 次删除，切批次也必然越线；另有磁盘双倍占用与合并多一次全量拷贝 | **移除整套分段机制**：`download_one` 改为单文件 `curl -C -` 续传（GCS 支持 Accept-Ranges，实测 8 MB 前缀 → 190 MB 完整，单连接 2.29 MB/s），无中间文件、无删除、无拷贝；`segment_ranges` / `parts_manifest` / `ensure_parts_manifest` / `_truncate_parts` / `_range_download` 全部删除；`clean_cache` 与所有清理改走新增的 `dispose_tree()`：**条目 ≤40 才真删，超出则清零留 0 B 空壳**（空间同样释放，一次批量删除都不发起） | `no_segment_machinery` / `dispose_budget` / `clean_cache_budget_safe` / `download_no_parts` / `resume_keeps_partial` / `resume_curl_flags` |
| 4 | 写 GeoTIFF 的压缩参数不是最优 | 微基准（7866×12073 float32 = 87.7 M 像元）：默认 z6/p2 **39.5 s / 271.3 MB** | 改 `predictor=3 + zlevel=4`：**19.1 s / 194.7 MB**（快 2.1×、体积 −28%，48 块省约 20 分钟与 3.6 GB）；数值结果不变 | `geotiff_compression_opts` |
| 5 | 分块顺序跑**内存累积**，第 3 块 MemoryError | 首轮：`numpy._core._exceptions._ArrayMemoryError: Unable to allocate 190. MiB for shape (1, 6364, 7809)` → 未预期异常 traceback exit 1，用户不知道已建的块有效、也不知道能续跑。成因：`np.percentile` 对 87.7 M 像元各复制一份（+702 MB）＋ RSS 不回收 | ①新增 `percentiles_inplace()`：`ndarray.partition()` **原地**算，linear 插值公式与 `np.percentile` 完全一致（探针对比 <1e-3），峰值少 2×n×4 B；②每块结束 `gc.collect()`；③`MemoryError` 显式捕获为 GemError，报出「已完成/跳过 N/48 块，重跑自动断点跳过」并给三种补救（关程序重跑 / `--split` 加倍 / `--mode window`） | `percentiles_inplace_exact` / `split_memory_error_actionable` |

附带：`--chunk-mb` 因分段机制移除而失效，**显式 `[warn]` 说明已废弃**（不做静默忽略）。

### 回归

selftest 123 → **131/131**（cnclimate venv，rasterio 1.5.2 + shapely 2.2.0），
新增 8 项、按新契约重写 6 项旧探针。真实端到端：德国 93 片 / 12482 MB / 48 块；
列支敦士登 full 模式 84 s 正常。

## v1.11.0（2026-11-15）· CLI 一致性收尾 + 静默忽略类修复 + 守卫拦截可观测

触发：用户「怎么还有这么多问题，认真审查一下，修复技能，优化代码，更新文档」。
对全部 7 个脚本做无预设通读，8 个可疑点**全部先最小合成证实再修复**（含一条由
selftest 后台运行被宿主守卫拦截而实证暴露的问题）。

| # | 问题 | 修复前实测 | 修复 | 探针 |
|---|---|---|---|---|
| 1 | selftest 临时目录清理被宿主守卫静默拦截 | 后台跑 selftest：rmtree 不抛 OSError、进程「成功」返回，**D 盘每跑一次泄漏一个 152 文件的目录**（已累积 8 个），零提示 | 新增 `rmtree_verified()`：**删后验证目录确实消失**，被拦时 `[warn]`+路径；自检开始时自动扫除历史 `gem15_selftest_*` 残留，下次运行自愈 | `rmtree_verified_observable` / `stale_tmp_sweep` |
| 2 | stac_index.py CLI 零错误包装（v1.10 漏掉的第 4 个 CLI） | bbox 非数字 / `--layer foo` / `--workers abc` 全部裸 ValueError traceback **exit 1** | 显式校验 + GemError → `[错误]` + **exit 2**（layer 校验在联网之前）；`select_items` 的 layer 异常从 ValueError 改 GemError | `stac_cli_validated` |
| 3 | fetch_tiles.py items 形状零校验 | items 是 dict / list[str] → `it.get` AttributeError traceback exit 1；`--layer foo` 不校验，错层名当缓存目录一路下到网络失败才报错 | items 须为非空 list、元素须带 cell/url；layer 前置校验（dtm/dsm） | `items_shape_validated` / `fetch_layer_validated` |
| 4 | build_mosaic.py items 形状零校验 | items 是 dict / list[str] → `item["proj_transform"]` TypeError traceback exit 1（v1.10 只修了文件不存在与坏 JSON） | items 须为非空 list、元素须带五项 STAC 元数据；边界须为含 type 的 GeoJSON 对象 | `items_shape_validated` |
| 5 | `--adm1` 与 `--bbox/--geojson` 同给 | **adm1 静默忽略**（与 v1.10 修的 `--country` 互斥同类）：用户以为拿了行政区，实际是矩形 | 互斥显式报错 exit 2 | `adm1_mutex` |
| 6 | `--country X` 时名称完全被无视 | `gem15_dem.py 日本 out --country FRA` **静默跑法国**（同类静默忽略） | 名称可解析且与 --country 指向不同国家时显式报「冲突」exit 2；名称与 country 一致或不可解析（作标签）放行并提示 | `country_name_conflict` |
| 7 | window 模式超限 warn 在海域预过滤之前发出 | 双岛+海域场景：海域 400 MB 片已被跳过（取数 400 MB < 上限 500 MB），warn 仍按全量 800 MB / 3 片报警 | warn 移到预过滤之后，按真正取数的片估算并注明「已扣除海域/空隙 N 片」；真超限时照常报（防漏报），`fetch_total_mb` 无条件入 stats | `window_warn_postfilter` |
| 8 | full 模式断点目录清理被守卫拦截时静默 | `download_one` 合并成功后 rmtree(parts) 被拦：磁盘双倍占用（成果+断点段）零提示（同 #1 机制） | 改用 `rmtree_verified`，被拦时 `[warn]` 并明示「磁盘仍持有约 X MB（合计 2×）」 | `parts_cleanup_warn`（按新契约同步） |

配套：`resolve_region.py` main 的 `except Exception` 收窄为 `except GemError`（代码
缺陷不再被包装成 exit 2 的「可预期错误」，与 gem15_dem 主入口对齐，未预期异常
保留 traceback exit 1）。

### 回归

selftest 114 → **123/123**（cnclimate venv，rasterio 1.5.2 + shapely 2.2.0）。
新增 9 项探针（4 项 subprocess 真实黑盒：三个 stac CLI 用例、四个 items 形状用例、
fetch layer 用例）。真实端到端：列支敦士登 window 全流程 37.9 s 成果正常；
`--adm1+--bbox`、`日本+--country FRA` 真实 CLI 均 rc=2 且零残留目录。

### 守卫语义实证（本机环境）

单次 rmtree 60/60/160 文件在前台（可提权）全部放行；后台任务（无法交互提权）
152 文件被静默拦截（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，rmtree 不抛 OSError、
目录原样保留）。结论：**删除是否被拦取决于运行上下文，代码不能假设 rmtree
成功，必须删后验证**——这正是 `rmtree_verified` 的存在理由。

### 脚手架备注（第 11、12 次踩坑，同类复发）

①print 写进 `redirect_stdout` 被吞（S5/S6 首次证实因此空输出）——这个坑仍在复发；
②探针先真删目录再断言「目录仍存在」（顺序反了），以及 list comprehension 的
表达式在元素非 dict 时调 `.get` 直接 AttributeError（校验代码自己裸崩）——
**探针与校验代码同样要按「先证实再断言」的自证流程过一遍**。

## v1.10.0（2026-11-15）· 独立黑盒矩阵：三个独立 CLI 错误处理统一、互斥参数、失败不白建目录

触发：用户「高级测试人员，测试技能，优化技能修复技能」。不带既往自检预设，从 CLI
用户视角设计 31 例真实 subprocess 黑盒矩阵（非法值 / 边界值 / 异常文件 / 互斥参数 /
输出目录卫生），**10 例 FAIL 全部证实后修复**。

| # | 用例 | 修复前实测 | 修复 | 探针 |
|---|---|---|---|---|
| 1 | `--geojson <目录>` | PermissionError traceback **exit 1** | resolve() geojson 分支读文件包 RegionError（路径无效/读取失败/不是合法 JSON 三种文案） | `geojson_wrapped` |
| 2 | `--geojson 坏JSON` | JSONDecodeError traceback exit 1 | 同上 | `geojson_wrapped` |
| 3 | fetch_tiles.py items 缺失 | FileNotFoundError traceback exit 1 | 文件存在性显式检查 + JSON 解析包装 | `fetch_cli_validated` |
| 4 | fetch_tiles.py `--workers 0` / `--timeout 0` / 非数值 | **参数零校验**，静默透传 | `num()` 前置校验（与 gem15_dem 同规则） | `fetch_cli_validated` |
| 5 | preview_dem.py `--maxside abc` | int() ValueError traceback exit 1 | main 包 try + 显式转 GemError | `preview_cli_exit2` |
| 6 | preview_dem.py `--maxside 1` / `--bg Bad` | GemError 但 **exit 1**（main 无包装） | 统一 `[错误]` + **exit 2** | `preview_cli_exit2` |
| 7 | build_mosaic.py items 裸读 + `--layer foo` | FileNotFoundError 先崩（layer 校验在文件读取**之后**） | layer 校验前移 + 文件判空 + exit 2 | `build_cli_layer_first` |
| 8 | `--country TWN --bbox 法国` | **TWN 静默忽略**，跑起法国全流程（首次跑 120 s 超时才暴露） | 互斥显式报错（下沉 `resolve()`，gem15_dem 与独立 CLI 双入口生效） | `exclusive_inputs` |
| 9 | 3 个位置参数 | 第 3 个静默忽略 | 「最多 2 个位置参数」exit 2 | `pos_args_capped` |
| 10 | 极区 / 纯海洋 bbox（STAC 判空 exit 2） | 留下只含 boundary.geojson 的半成品目录 | 建目录推迟到 **STAC 选片成功之后**，判空类失败零残留 | `stac_fail_no_outdir` |

注：OutputTooLargeError 的 `_split_plan.json` 是有意保留的可执行方案，不受影响。

### 回归

selftest 106 → **114/114**（cnclimate venv，rasterio 1.5.2 + shapely 2.2.0）。
新增 8 项：`geojson_wrapped` / `fetch_cli_validated` / `preview_cli_exit2` /
`build_cli_layer_first` / `exclusive_inputs` / `pos_args_capped` /
`stac_fail_no_outdir` / `v110_signature`。其中三个 CLI 行为用 **subprocess 真实
黑盒**（`sys.executable` 调脚本、断言 rc 与 stderr 无 Traceback），不再只测函数层。

### 脚手架备注

黑盒脚本自身两处修正：①互斥用例最初用完整 gem15_dem CLI 跑，合法输入会走完整
STAC 流程 120 s 超时（这本身就是发现 #8 的途径），改为 resolve 层判定；②`rc==0`
作「期望值」被 `if exp_rc and ...` 的 falsy 判断吞掉，pos3 用例假 PASS。

## v1.9.0（2026-11-15）· 台湾实测驱动的中国范围修复：台湾/港澳归并 CHN、bbox 中心点精判

触发：用户「优化代码，修复问题，更新文档，用台湾测试一下」。台湾测试一次暴露三个真问题，
全部先实测证实再修复；每处边界行为都有真实地理对照。

### 实测发现（修复前）

| # | 证实方式 | 结果 |
|---|---|---|
| 1 | 名称表检索 | `countries_zh.json` **无「台湾/香港/澳门」条目**：输入只得到「无法解析」（无 cn-dem 指引） |
| 2 | ISO3 直传探针 | `TWN/HKG/MAC` 3 字母 passthrough **完全绕过** `assert_allowed`，会去 geoBoundaries 拉边界并走 STAC |
| 3 | 台湾 bbox 联网 E2E | **GEM-15 STAC 对台湾区域实际有 cell 条目**：14 片 / 826 MB（w120s23 186.8 MB、w121s24 176.1 MB 等），bbox 走完 STAC 规划 + 统一网格 + 磁盘预检（产出 split_plan）才被内存预检拦下 —— 「bbox 落在排除区由 STAC 判空兜住」的契约失效 |
| 4 | 香港 bbox 联网对照 | gbOpen `HKG`/`MAC` API **404**（无此条目）；CHN **ADM0 不含港澳**（香港中心点 114.05,22.35 真实漏判） |
| 5 | ADM1 检索 | gbOpen CHN **ADM1 = 34 个省级行政区**，实测含 `Taiwan Province`、`Hong Kong SAR`、`Macau SAR` |
| 6 | 香港 simplified 几何 | HK SAR MultiPolygon 3 个 part **均不含新界大部**（点在 bounds 内、多边形外）—— simplified 对小面积要素失真严重 |

### 修复

| # | 修复 | 说明 |
|---|---|---|
| 1 | `countries_zh.json` CHN 别名补 `台湾\|中国台湾\|台湾省\|Taiwan\|香港\|中国香港\|Hong Kong\|澳门\|中国澳门\|Macao\|Macau` 等 | 名称路径归并 CHN → CoverageError（exit 2，指向 cn-dem） |
| 2 | `resolve_region.CHINA_REGION_ISO = {TWN/HKG/MAC}` + `assert_allowed` 前置归并 | ISO3 直传同样落排除路径，文案明确「是中国的一部分」 |
| 3 | 新增 `assert_not_china(bbox)`，接入 bbox 与自备 GeoJSON 两条路径 | 粗判 `CHINA_BBOX=(73,3.5,136,54.5)` 不相交**零网络**放行；相交才拉精判边界；以区域 **bbox 中心点**为代表点（邻国 bbox 矩形擦到中国不误伤） |
| 4 | 精判源 = geoBoundaries CHN **ADM1** 简化版（独立缓存键） | 单一数据源即覆盖「中国（含台湾、港澳）」；任何失败 `[warn]` 放行（可观测，不静默） |
| 5 | 小面积要素（<0.5 平方度）bounds 近似判定 | 修复 simplified 香港失真漏判；放宽方向是「多拒、指向 cn-dem」，符合契约；大要素（台湾 3.2 平方度）保持精确多边形 |
| 6 | `EXCLUDED_NOTE` 明确「排除中国（含台湾、香港、澳门）」 | 名称表 / SKILL.md / README 同步 |

### 真实地理对照（联网，修复后）

| 输入 | 期望 | 实测 |
|---|---|---|
| `gem15_dem.py 台湾` | 拦截 exit 2 | ✅ 归并 CHN，无输出目录 |
| `gem15_dem.py TWN` | 拦截 exit 2 | ✅ 同上 |
| bbox 香港 (113.6,22.0,114.5,22.7) | 拦截 exit 2 | ✅ 中心点精判命中 |
| bbox 澳门 (113.4,22.05,113.65,22.25) | 拦截 exit 2 | ✅ |
| bbox 台湾 (119.9,21.9,122.1,25.4) | 拦截 exit 2 | ✅ 修复前走完规划 68.5 s、产出 split_plan；修复后秒拦 |
| bbox 越南北部 (104.5,20.5,106.5,21.8) | 放行 exit 0 | ✅ 粗判相交但中心在河内，不误伤 |
| bbox 日本九州西部 (128.5,31.5,130.5,33.5) | 放行 exit 0 | ✅ |

### 回归

selftest 102 → **106/106**（cnclimate venv，rasterio 1.5.2 + shapely 2.2.0）。
新增 4 项：`china_regions_named`（6 个名称全归并 CHN）、`china_regions_iso_blocked`
（TWN/HKG/MAC 直传拦截 + 文案断言）、`bbox_china_blocked`（台湾精确拦截 / 香港
bounds 近似 / 越南放行 / 法国粗判零网络）、`v19_signature`。
CLI 失败路径矩阵 4 → 6 例（新增台湾 / TWN，均不建输出目录）。

## v1.8.0（2026-11-15）· 审计优化：海域预过滤、full 分段 -L、断点零删除、相对 href 归一化

触发：用户「优化技能」。流程：完整通读 7 脚本标记可疑点 → 6 项猜想逐一用最小合成
输入证实（一次性脚本 V1–V6，全部离线 mock）→ 修复 → 8 项新探针固化 → 102/102。

| # | 证实（修复前实测行为） | 修复 | 回归探针 |
|---|---|---|---|
| 1 | `_range_download` 的 curl 缺 `-L`（`curl_bytes` / `curl_get` 均有），上游 3xx 重定向会空转耗尽重试 | 补 `-L`；Range 头随重定向转发，字节级校验语义不变 | `range_curl_follows_redirects` |
| 2 | `ensure_parts_manifest` 指纹不匹配走 unlink 循环（实测 2 次）；大面积指纹变化会像 v1.4.0 的 STAC 索引那样撞宿主批量删除守卫（约第 50 次删除终止进程） | 旧段**截断为 0 B**（零删除）：0 长度过不了 `size == want` 复用校验，`curl -o` 覆盖写直接重下，与 v1.5.0 坏段处理同一哲学 | `parts_mismatch_zero_delete` |
| 3 | 独立 CLI `build_mosaic.py --layer foo` 在 `LAYER_BANDS[foo]` 抛 `KeyError`（未预期错误 traceback exit 1） | `build()` 入口前置校验 → `GemError`（主入口 exit 2） | `build_layer_validated` |
| 4 | STAC asset href 原样透传；STAC 规范允许相对 href（RFC 3986），一旦出现会静默 404 | 新增 `resolve_asset_href` 按 item URL 归一化 + `[warn]`；绝对 URL 原样返回（fixtures 12 个真实样本全部绝对，现行行为零影响） | `relative_href_resolved` |
| 5 | 双岛合成实测：中间纯海域 cell 与边界多边形无任何重叠，却因与区域 bbox 正面积相交被整片取数（full 白下整 COG / window 白读远程窗口），读完全 NaN 后才丢弃 | 新增 `part_bboxes` + `offshore_prefilter`：cell bbox 与边界**所有环外接框**（含 `--buffer` 外扩）均无正面积相交 → 跳过取数。判据是保守上界（多边形 ⊆ 各环外接框之并），**绝不误跳**；未知几何类型 / bbox 矩形边界直接放弃预过滤 | `offshore_prefilter_pure` |
| 6 | （同上） | **仅 clip 模式**启用（`--no-clip` 保留完整矩形会含界外像元，不适用）；统一网格仍按全部规划分片计算；端到端双跑对照 | `offshore_skip_identical_output` |
| 7 | `assert_output_fits` 报错硬编码「本机 C: 实测仅约 10 GiB」机器专属文案，其他环境信息失真 | 移除该短语，任何环境输出一致 | `output_message_neutral` |

### v1.8.0 验证结果

| 项 | 结果 |
|---|---|
| selftest | **102/102 通过**（94 → 102，新增 8 项；旧探针 `parts_manifest` 按零删除新契约同步更新） |
| 环境 | cnclimate venv（rasterio 1.5.2 / shapely 2.2.0）。default（1.5.1）环境已不在本机；insar_venv 缺 requirements 声明的 shapely，非有效环境（未擅自安装） |
| 零回归 | 单岛 / bbox 矩形边界 / 无海域 cell 时预过滤为空操作，代码路径不变；双岛端到端开启 / 关闭预过滤成果 transform / 形状 / 像元含 NaN 分布逐位一致 |
| 放弃项 | 用 shapely 做精确「cell × 多边形」相交判定以跳过更多沿海 cell——生产链路引入几何库精确求交在复杂 ADM0 多边形上有性能与边界情形风险，环外接框保守上界已保证零误跳，收益比不划算 |

## v1.7.0（2026-11-15）· 大国 / 大文件：预检前移、总量与磁盘内存防护、自动分块

触发：用户「认真的修复问题，优化技能，注意大文件，大国」，承接法国整国实测
（147 cell / 成果 32647 MB / 跨 3 个 UTM 带）暴露的规模瓶颈。

流程：完整审计 8 脚本 → 双环境 85/85 基线 → 廉价探针测真实量级（AST 顺序 / STAC
`file:size` 汇总 / `disk_usage` / `GlobalMemoryStatusEx`）→ 最小合成逐条证实 →
只修证实项 → 9 个新探针同步 `EXPECTED_KEYS` → 双环境 94/94 → 黑盒 CLI 矩阵 →
真实分块端到端 + 逐像元比对 + 断点续跑 + 空块 + 零回归。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | **修复预检顺序真 bug**：`build()` 把 `decide_crs` / `grid_for` / 四项预检全部移到 `download_items()` 之前 | AST 实测 v1.6.0 `download_items()` 在 **442 行**、`assert_output_fits()` 在 **460 行**；`full` 模式法国整国会先把 **147 片 / 19705 MiB（19.24 GiB）** 落盘，才报「成果数组 32647 MB 超限」 | 最小合成（2 片各 1.2 GB）证实预检阶段 `download_items` 调用 **0 次**、缓存目录未创建、异常 `DownloadTooLargeError`；selftest `preflight_before_download` 固化调用行号 473/475/479/496 |
| 2 | 新增 `assert_download_fits()` + `--max-download-mb 2048`（`build()` 与 `download_items()` 双入口防御） | 已落盘法国 items 现场汇总：147 片、中位 158.3 MB、合计 **19.24 GiB**；原代码 `max_download` / `disk_usage` 出现 **0 次** | selftest `download_total_guard`（enforce 开关 / 缺 `file:size` 显式报错）；`full` 真实下载 23.97 MB 并把 `planned_mb` 写入 manifest |
| 3 | 新增 `assert_disk_space()`：在下载与写 GeoTIFF 前检查目标盘，另留 512 MB 余量 | 实测 C: 可用 **10.3 GiB**、D: 163.2 GiB，而整国成果约 **32 GiB**；落到 C: 会写满并产生损坏文件 | selftest `disk_space_guard`（mock 小盘拒绝 + 正常记录 `disk_free_mb`）；错误信息给「换盘 / 分块 / 用 dtm」建议 |
| 4 | 新增 `available_memory_mb()`（Windows `GlobalMemoryStatusEx`），`assert_output_fits` 增加物理内存对照 | 总 31.68 GiB / 可用 **7.71 GiB**，整国峰值估算 **97941 MB**；若用户按旧提示把 `--max-output-mb` 提到 40000，数组上限放行但进程会被系统 **OOM 静默杀掉** | `--max-output-mb 40000` 真实 CLI **1.4 s / exit 2** 明确拒绝；selftest `physical_memory_guard`（可用 100 MB 时拒绝峰值 1144 MB） |
| 5 | 超限时入口生成**可执行** `<stem>_split_plan.json`，`OutputTooLargeError` 携带 `info`（规模 / 网格 / bbox / CRS / 下载量） | v1.6.0 只给「用 --bbox 拆成 2–3 块」的含糊建议；法国整国实测需 90 块 | 整国默认 / `full` 均 **1.5 s / 1.7 s** exit 2、缓存 tiles **0 文件**，方案 10×9=90 块 / 每块 362.74 MB，错误信息含 `--split auto` 一键命令 |
| 6 | 新增 `--split off\|auto\|NxM`：自动分块逐块复用已验证的单块路径，统一 CRS、`--no-clip`、断点续跑 | 按 512 MB 预算法国整国需 90 块；每块独立数组 → **内存恒定**；`whole_crs` 由全部 items 一次决定，避免各块 UTM / 4326 混杂 | 真实 `--split 2x2` 勃朗峰：**4 块 / 12.39 s**，统一 `EPSG:32632`；selftest `split_opt_validation` / `split_plan_geometry` / `split_argv_and_resume` |
| 7 | 分块预算按**物理可用内存封顶**：`min(用户上限, avail×0.8/3)` | 二次证伪：`--max-output-mb 40000` 若按 40000 推导会切出每块 **16 GB** 的方案，仍 OOM | 实测封顶 **2073.81 MB** → 25 块 / 每块 1305.88 MB / 峰值 3918 MB < 可用×0.8 = 6221 MB |
| 8 | 分块遇到纯水域 / 境外 / 冰雪空块：捕获 `EmptyRasterError`，写 `[warn]` + `_empty.json` 后**继续**，汇总区分 `ok`/`cached`/`empty`/`cached_empty` | 大国 90 块必然含空块；v1.6.0 一块空数据会中断整批（几十小时白跑） | 真实海陆交界 `--split 2x2`：4 块中 **2 块纯水域**，`['ok','ok','empty','empty']`、整批 **rc=0**；selftest `split_empty_tile_continues` |
| 9 | window 模式远程读入超 `--max-download-mb` 时打印 `[warn]`（不阻止，但可观测） | 大国每片窗口接近整片 COG（8288² ≈ 262 MB），法国合计 **37.6 GiB 远程读入**；用户容易误以为「没下载就没流量」 | 错误信息与 `build.stats.download_total_mb` / `download_cells` 均入 manifest |
| 10 | `selftest.py` **85 → 94 项**（9 个新 key 同步 `EXPECTED_KEYS`），版本五处同步 1.7.0 | 用户既有规范：新增探针必须加进断言列表 | rasterio 1.5.1 / 1.5.2 **双环境各 94/94** |

### v1.7.0 验证结果

| 验证 | 结果 |
|---|---|
| 离线回归 | default（rasterio **1.5.1**）**94/94**；cnclimate（**1.5.2**）**94/94** |
| 黑盒 CLI 矩阵 | `--split banana/0x2/65x2`、`--max-download-mb 0/abc` 全部 `GemError` / exit 2、**0 traceback**、**不建输出目录**；`--help` exit 0 |
| 法国整国（默认 / full / 提高上限） | **1.5 s / 1.7 s / 1.4 s** exit 2，缓存 tiles **0 文件**；分别生成 90 块 / 90 块 / 25 块方案 |
| 分块数据一致性 | 4 块与整块 **809846 个重叠像元最大绝对差 0.0**、冲突 0；额外 52386 像元 = 上轮证实的梯形斜边掩膜挖空（`--no-clip` 保留） |
| 断点续跑 | 同命令重跑 **1.54 s**、4 块全跳过、无 GeoTIFF 重写 |
| 默认路径零回归 | 勃朗峰复跑与 v1.6.0 **GeoTIFF 逐像元完全一致（含 NaN，最大差 0.0）**、manifest **10 项全 SAME**、预览 PNG **像素完全一致** |
| `full` 模式真实下载 | window 廉价探针先确认 `w3s42` 有 12.6% 有效值，再下载 **23.97 MB / 16.8 s** 成功产出 `[1,598,444] EPSG:32631` |
| 成果 / 缓存 | 新 stats：`download_total_mb` / `download_cells` / `disk_free_mb` / `disk_needed_mb` / `available_memory_mb` 全部可观测 |

**脚手架又错 3 次（同类，已固化进说明）**：
① `print` 写进 `redirect_stdout` 被吞（P1 异常类型行消失）；
② mock `shutil.disk_usage` 返回 tuple，而生产用 `.free` → `AttributeError`（**测试替身类型必须与生产对齐**，改 `SimpleNamespace`）；
③ `v16_signature` 残留 `len(EXPECTED_KEYS)==92` 导致 94 项回归假失败。
另：探针 61 初次写成绕来绕去的 `or` 表达式，已简化为直白三条件——**断言失败先怀疑断言 / 脚手架**。

**观察项（真实数据下不触发、本轮不改）**：`full` 模式判空仍在整片下载之后
（海洋片实测白下 4 MB；下载前小窗口探测会增加 147 次请求，收益有限）；
STAC asset 相对 href；`grid_for` 基于 plan 而实际窗口回退时已有 `[warn]`；
`index_workers` 无上限；full 段下载缺 `-L`；`gb_geojson --refresh` 仍有 ≤2 次 unlink。

## v1.6.0（2026-11-15）· 独立黑盒复测：参数健壮性、数据正确性与口径诚实

触发：用户「怎么还有这么多问题，认真测试并修复问题」。本轮**不沿用之前自检的结论**，
改为先建立独立基线 → 黑盒 CLI 矩阵 → 静态审计 → 逐条最小合成证伪 → 只修证实项。

流程：双环境 75/75 基线 + 全脚本 AST 编译 + 规范五处核查 → **29 例真实命令行矩阵**
（退出码 / traceback / 是否白建目录 / 是否卡进联网）→ 静态扫描全部 `except` 与参数校验
→ 8 组最小合成输入 / 微基准逐条证伪 → 只修 10 类证实项 → 10 个新探针同步
`EXPECTED_KEYS` → 双环境 85/85 → 黑盒矩阵重跑 → 4 组真实端到端 + 逐像元比对。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | 删除 `outdir.mkdir()` / `cache.mkdir()` 的提前调用，改为**第一次真正写成果时**（`boundary_file.parent.mkdir`）才建目录 | 黑盒 29 例实测：v1.5.0 有 **13 个失败用例留下输出目录**（`--layer bad` 之外的 `RegionError` / `CoverageError` / bbox 非法 / 数值非法全中），v1.5.0 声称的「参数校验前移」只覆盖了 `run()` 内显式校验的那几项 | selftest `output_dir_late_create`；矩阵重跑残留目录 **13 → 0** |
| 2 | 四个数值参数补范围校验：`workers ≥ 1`、`timeout ≥ 1`、`chunk_mb > 0`、`buffer_m ≥ 0` | 黑盒实测 `workers 0/-3`、`timeout 0/-5`、`chunk_mb 0/-4`、`buffer -1000` **全部通过校验**并走到联网阶段；`timeout=0` 使 `curl --max-time 0` 变成无超时、又被 `subprocess.run(timeout=timeout+15)` 在 15 s 兜底误杀；`chunk_mb=0` 被 `max(1MiB,…)` 静默改成 1 MiB | selftest `numeric_range_validation`（7 个非法值全 `GemError` 且不留目录） |
| 3 | 新增 `validate_crs_opt()`，在入口参数段 + `decide_crs`（独立 CLI）双处前置校验 `--crs` | 最小合成实测 `--crs bad` / `999999` / `EPSG:999999` 抛 **`rasterio.errors.CRSError`**（非 `GemError`）→ `[未预期错误]` + traceback + **exit 1** | selftest `crs_validation`（3 个非法值 + `decide_crs` 直调均为 `GemError`）；`EPSG:4326` 仍合法 |
| 4 | 新增 `normalize_nodata()`：`read_one_window` 读取时把非 NaN 的 `src.nodata` 掩成 NaN | 合成 COG 声明 `nodata=-32768`：100 个填充像元被 `np.isfinite` 判为有效贴入，成果 `min=-32768 m`（**静默数据错误**）。旧代码返回 `src.nodata` 却从不使用，隐含「GEM-15 一定是 NaN」的假设 | selftest `nodata_non_nan_masked`（掩为 NaN + `[warn]`）；真实 GEM-15 `nodata=NaN` 时零复制，LIE 成果逐像元不变 |
| 5 | `bbox_of_geojson` / `geojson_geometries` 跳过空 geometry 并打印 `[warn]`，全空仍判空 | 自备 GeoJSON 含 `geometry:null` 时 `bbox_of_geojson` 抛 **`AttributeError`**（未预期 exit 1）；`geojson_geometries` 旧实现静默 `continue` | selftest `geojson_null_geometry` |
| 6 | `paste` 去掉 2D `filled` 掩膜，改为**逐波段** `np.isfinite(region[b])` 判空 | 最小合成 2 波段 / 2 片完全重叠（片 A 波段0 有效、波段1 NaN；片 B 波段1 有效）→ 片 B 的波段1 **整片被跳过**；真实 `outputs/gem15_lie_dsm` 的 5 波段中 `dsm_std` 与波段0 的 NaN 掩码有 **41 个像元不同** | selftest `multiband_first_valid`；dsm 真实端到端 `[5,154,108]` 5 波段 descriptions 正确 |
| 7 | `--title ""` 回落时 manifest 记录**生效标题**，并新增 `query.title_input` 存原始输入 | 最小 mock 实测：`render` 收到 `'probe'`，`manifest.query.title` 却是 `''`（成果不可复现，v1.5.0 新增字段的语义漏洞） | selftest `title_manifest_matches_render` |
| 8 | `--no-clip` 不再无条件 `project_geometries`，几何投影移进裁切分支 | `no_clip=True` + `geometry:null` 边界仍抛 `AttributeError`；投影结果本就未被使用（无用计算 + 潜在失败） | selftest `no_clip_needs_no_geometry`；真实 CLI `[1,1749,938]` |
| 9 | `assert_output_fits` 新增 `build.estimated_peak_mb`（≈3× 成果数组）并在报错中写清峰值口径；**判定逻辑不变** | 合成 1000²→600²：紧凑化复制瞬间全网格 + 副本共存 **5.19 MB**，而 `estimated_output_mb` 只报成果 **1.37 MB**（**3.8×**）；旧文档称「保守上界」实为**下界**，用户按 512 MB 估计机器内存会实际 OOM | selftest `peak_memory_estimate`；LIE manifest `6.26 / 18.77 MB` |
| 10 | `selftest.py` **75 → 85 项**（10 个新 key 全部同步 `EXPECTED_KEYS`），三处版本断言同步 1.6.0 | 用户既有规范：新增探针必须加进断言列表，否则用例空转 | rasterio 1.5.1 / 1.5.2 **双环境各 85/85** |

### v1.6.0 验证结果

| 验证 | 结果 |
|---|---|
| 离线回归 | default（rasterio **1.5.1**）**85/85**；cnclimate（**1.5.2**）**85/85** |
| 黑盒 CLI 矩阵重跑 29 例 | 残留输出目录 **13 → 0**、**0 个 traceback**、非法参数全部 `GemError` / exit 2 且不进入联网阶段；`--help` / `-h` / `--clean-cache` / 无参数退出码不变 |
| LIE 端到端（自备边界 / window / 白底，STAC 缓存命中） | **9.09 s**；与 v1.5.0 manifest **10 项全 SAME**（`crs` / `resolution` / `output_shape` / `valid_pixels` 617724 / `valid_ratio` / 5 个高程统计）；GeoTIFF `[1,1657,846]` **逐像元完全一致（含 NaN），最大绝对差 0.0**；transform / CRS / nodata 相同；预览 PNG **像素完全一致**（平均差 0.0） |
| dsm 多波段真实 CLI | **5.23 s**，`[5,154,108]`，band descriptions 为 5 个正确名称，各波段有效像元一致、无 `-32768` 填充值 |
| `--no-clip` 真实 CLI | **15.08 s**，`[1,1749,938]` 完整网格，有效 **1486932** 像元，`compact_copy=false` |
| 跨 UTM 带真实 CLI | **10.4 s**，2 个带 `[32632,32633]` 降级 `EPSG:4326`，`[1,742,152]`，2 条 `[warn]` 与 manifest 降级原因均可观测 |
| 规范核查 | 8 脚本 AST 全过；版本号五处一致 1.6.0；frontmatter 7 字段齐备；0 个 `.pyc`、0 个三级目录 |

### v1.6.0 口径说明、脚手架修正与放弃项

| 项 | 说明 |
|---|---|
| `estimated_peak_mb` | 是**估算值**（成果数组 ×3），不是实测进程峰值；真实系数随 `grid_pixels / output_pixels`、波段数、掩膜比例变化（合成 1000²→600² 实测 3.8×）。`--max-output-mb` 的**判定逻辑未改**，避免改变既有区域能否跑通、保证成果逐像元不变 |
| `paste` 去掉 `filled` | 逐波段判空后不再常驻一块 `(height,width)` 全网格 bool 掩膜；`n_contrib` 改为「本片至少为一个波段新增有效值的像元数」，掩膜一致时与旧口径数值相同（LIE 逐像元已验证） |
| **断言脚手架又错一次** | `peak_memory_estimate` 初次 `85/85` 前失败：断言拿**已四舍五入**的 `estimated_output_mb=3.81` 乘 3 得 `11.43`，去比**未舍入**的 `3.8147×3=11.44`。生产代码无误，改 0.02 容差后通过。**断言失败先怀疑断言**（本会话内第 3 次同类） |
| `selftest` 清理改为可观测 | `finally` 原用 `shutil.rmtree(tmp, ignore_errors=True)`，本轮扫描发现 D 盘累积 **16 个**历史自检临时目录（114–141 文件/个）却从未有任何提示。改为捕获 `OSError` 打印 `[warn]` + 路径；本轮 20 项临时目录 / 日志已全部清理，**用户 STAC 与边界缓存保留** |
| P7 探针 `print` 写在 `redirect_stdout` 里 | 输出被吞、看似「用例没跑」，把 `print` 移出 `with` 后才拿到 `AttributeError` 证据。与 v1.5.0 记录同类 |
| 放弃：把峰值系数纳入 `--max-output-mb` 判定 | 会改变既有语义（卢森堡 146 MB ×3 = 438 MB 仍可过、但边界区域的预算含义变了），且缺少端到端峰值实测支撑。本轮只做「口径诚实 + 可观测」 |
| 观察项（真实数据下不触发，记录不改） | ① STAC asset href 相对路径：6 个真实 fixtures 全为绝对 URL；② `grid_for` 基于 plan 而实际窗口回退时已有 `[warn]` 且 `paste` 会裁到网格内；③ `index_workers = max(16, workers)` 无上限（v1.5.0 已记）；④ `_range_download` 缺 `-L`（真实 GCS 不重定向）；⑤ `gb_geojson --refresh` 仍有 ≤2 次 `unlink`（远低于宿主守卫阈值） |

## v1.5.0（2026-11-15）· 语义正确性、写出内存与全链路零删除

触发：用户「继续优化技能」。流程：**重新通读全部 8 个脚本 → 标记 14 个可疑点
（不边读边改）→ 逐条用最小合成输入 / 微基准证实 → 只修证实的 10 类问题 →
新增 13 个回归探针 → 双环境 75/75 → 全新缓存端到端 → 与 v1.4.0 逐像元比对**。
基线 62/62（rasterio 1.5.1 / 1.5.2 双环境）。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | `--no-clip` 改为**真正保留完整拼接网格**：只在 `not no_clip` 时做掩膜与外接框裁切 | README 早已写「不做边界掩膜，保留矩形」，但旧实现只跳过掩膜、仍按 `clip_bbox_for(boundary)` 裁切。最小合成：网格 **198×198**，`no_clip=True` 与 `False` 都输出 **106×106**（只保留 **28.7%**） | selftest `no_clip_keeps_grid`：`True -> [198,198]`、`False -> [106,106]` |
| 2 | 裁切后 `np.ascontiguousarray` 紧凑化 + `del` 全网格 / `filled` / `outside` / `valid` / `v`；`stats` 增加 `grid_pixels` / `compact_copy` / `released_mb` 让优化可观测 | 4000² 全网格 61 MB、裁切视图 8.6 MB：`del` 原数组后 RSS **+0.0 MB**（numpy 视图持有 base），紧凑化后 **−52.5 MB**；写出阶段常驻 **+63.9 MB → +8.6 MB（−86%）**，耗时 0.50 s → 0.62 s（+0.12 s） | selftest `compact_copy_releases_grid`；日志打印 `released_mb` |
| 3 | 紧凑化**不加手工阈值** | 实测 `np.ascontiguousarray(全切片) is 全切片`（零复制）、子视图才真复制；由 numpy 连续性判断自适应，避免拍脑袋定比例 | selftest `ascontiguousarray_zero_copy` |
| 4 | 实际 COG 波段数与 `LAYER_BANDS[layer]` 声明不符时显式 `DownloadError` | 合成 1 波段 COG 配 `dsm`（声明 5 波段）→ 旧实现在 `paste` 抛 `IndexError: index 1 is out of bounds for axis 0 with size 1`（未预期错误 exit 1） | selftest `band_count_mismatch` |
| 5 | `run()` 全部参数校验前移到 `outdir.mkdir()` 之前；数值选项包成 `GemError`；`bg` / `maxside` 也前置 | 实测 `--layer bad` 会先 mkdir 再抛错，白建输出目录（与 v1.3.0 修的 `--clean-cache` 同类） | selftest `bad_args_no_outdir` |
| 6 | `parse_args` 区分「拼错的带值选项」与「给开关传 `=值`」 | 旧实现 `--layr=dsm` 回「开关选项不接受 = 值；**请写成 --layr**」，把拼错项当合法开关推荐；只有 `--clean-cache=1` 才是真开关 | selftest `unknown_equals_hint` |
| 7 | `--maxside` 双入口前置校验（≥2） | `maxside=0/-1` → 降采样成 1×1 → `np.gradient` 抛 `ValueError: Shape of array too small`（未预期错误 exit 1 + traceback） | selftest `maxside_validation` |
| 8 | full 模式分段失败**不再 unlink**（坏段截 0 B）；`merged.rename` 改 `merged.replace`；`rmtree(ignore_errors=True)` 改为可观测 `[warn]` | 实测 `curl -o` 会把已存在 500 B 文件**截断覆盖**成 10 B；`Path.replace` 在 Windows 上可覆盖目标（700 B → 21 B）。旧实现 `retries=2` 连续失败产生 **3 次 unlink**；断点目录清理失败被静默吞掉 | selftest `range_no_delete` / `parts_cleanup_warn`；v1.4.0 记为「观察项」的 full 段删除本轮彻底消除 |
| 9 | `get_json` 损坏缓存改**覆盖写**修复，索引路径真正零删除 | v1.4.0 只清掉成功路径的临时文件，缓存 JSON 损坏时仍有 **1 次 unlink**。现先 `warn` 再直接 `write_text` 覆盖 | selftest `get_json_zero_delete` + 既有 `bulk_index_no_delete`（100 次索引 0 删除） |
| 10 | `gb_geojson` 主链接缺失回退 alt 时打印 `[warn]` + 真实字段名 | 旧实现 `meta.get(key) or meta.get(alt)` 静默回退，且日志按 `key`（而非实际选中字段）判断：ADM0 主链接缺失时**输出为空**，用户不知道用了简化版边界 | selftest `boundary_fallback_visible` |
| 11 | `manifest.query` 补 `bg` / `title` / `maxside` / `plain`，与 `render()` 实参一致 | 旧 manifest 无法复现预览样式 | selftest `preview_meta_in_manifest`（mock 全链路后读 manifest + 检查 `render` kwargs） |
| 12 | `selftest.py` **62 → 75 项**（新增 13 个 key 全部同步 `EXPECTED_KEYS`） | 两处版本断言同步到 1.5.0 | rasterio 1.5.1 / 1.5.2 **双环境各 75/75** |

### v1.5.0 验证结果

| 验证 | 结果 |
|---|---|
| 离线回归 | default（rasterio **1.5.1**）**75/75**；cnclimate（**1.5.2**）**75/75** |
| 微基准（合成 4000² → 裁到 1500²） | 写出阶段常驻 **+63.9 MB → +8.6 MB（−86%）**；复制+写出 **0.50 s → 0.62 s**；`ascontiguousarray` 全切片零复制 |
| 全新缓存端到端（window，LIE / dtm / 白底） | **112.78 s**，网格 938×1749 → 成果 `[1,1657,846]`、有效 **617724** 像元、高程 **424.79–2495.55 m**；日志「裁切后紧凑化：释放全网格约 0.9 MB」；与 v1.4.0 manifest **10 项全 SAME**、GeoTIFF **逐像元一致（含 NaN），最大差 0.0** |
| `--no-clip` 真实 CLI | 同城同边界 **12.14 s**：日志「保留完整拼接网格 938x1749」，成果 `[1,1749,938]`、有效 **1486932** 像元 (90.6%)、`compact_copy=false` / `released_mb=0.0`，与裁切版形成明确对照 |
| 全新缓存端到端（full，LIE / dtm / 白底） | **523.92 s**：10 段 / **153.30 MB** 整片 COG 落盘（下载 421.58 s），日志无分段校验失败、无清理告警；`merged.replace` + `rmtree` 收尾后 `.part` / `.parts` / `.merging` / `.tmp` **全部为 0**；成果与 window 模式 manifest **10 项全 SAME**、GeoTIFF **逐像元一致（最大差 0.0）** |

### v1.5.0 口径说明与放弃项

| 项 | 说明 |
|---|---|
| −86% 的口径 | 是**裁切后写出阶段常驻 RSS** 的合成微基准（4000² → 1500²，裁切保留约 14% 像元），**不是端到端峰值**；真实收益取决于 `grid_pixels / output_pixels` 比值。`stats.released_mb` 按 `像元差 × 波段数 × 4 B` 估算，可由日志核对 |
| +0.12 s | 紧凑化复制的代价，在合成 4000² 上测得（含 deflate 写出）。大区域写出阶段更长，复制成本相对更小 |
| `Path.replace` | 实测在 Windows 覆盖成功、源文件自动消失；未引入 `os` 依赖，语义与 POSIX `rename` 一致 |
| full 下载 153.30 MB / 421.6 s | 日志里的「新下载聚合」是**墙钟摊出来的总量**，不是单连接速率。本轮单 cell 无并发、实测约 0.36 MB/s，明显低于历史 1.61 MB/s，属网关波动，**不作为调参依据**；full 模式下 `window` 读取仅 0.1 s，说明整片落盘后本地读取确实快 |
| full 验证缓存的清理日志 | 执行 `--clean-cache --cache D:/gem15_v15_full_cache` 报 **0 文件 / 0.00 MB**，但清理前 `du` 实测 tiles **154M**、完整 tif 1 个（与 v1.3.0 同类现象，**原因未查明，不猜**）。另造 20 MiB 合成瓦片复测，`clean_cache` 准确报 **20.00 MB / 2 文件**、清理后 0 文件，统计逻辑无问题。故文档只写「清理后 0 文件」这一可核实结论 |
| 放弃：`stac_index.LAT_LIMITS/LON_LIMITS` 硬编码 fallback | 仅 `available=None` 的离线用例走该分支；生产路径一律用 collection 真实 child 组反推。上界 80 已覆盖 84°N 所在的 `p80` 带，实测无漏片，不做无依据的「动态下界」改造 |
| 放弃：`index_workers = max(16, workers)` 加上限 | 只有用户显式传 `--workers` 才会提高 STAC 并发；擅自封顶会改变既有语义，记为观察项 |
| 放弃：自备 `--bbox` / `--geojson` 也做排除区检查 | 用户显式指定矩形 / 自备边界，且 `EmptyRasterError` 会判空；与「国家名自动解析」语义不同 |
| 观察项：`ensure_parts_manifest` 清旧断点段仍用 `unlink` | 仅在指纹不匹配 / 无指纹旧缓存时触发（单 cell 约 10 段），是**正确性必需**（否则旧段被误复用），保留 |

## v1.4.0（2026-11-15）· 大区域索引零临时文件（英国整国实测触发）

触发：用户要求「用英国测下技能」。流程同前：廉价探针确认边界源恢复 → 整国真实跑
→ **失败暴露新 bug** → 最小复现证实 → 修复 → 固化回归 → 双环境验证
→ 全新缓存重跑 → 与 v1.3.0 逐像元比对 → 英国子区域出真实成果。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | `get_json` 改为 `curl_bytes` **内存读取**，不再 mkstemp + unlink；新增 `curl_bytes`（`-w '\n%{http_code}'` + `rpartition` 拆 body/code，空体 / 非 200 / 404 显式报错） | 英国整国 87 个 1° cell → 1 collection + 6 group + 87 item = **94 次临时文件创建 + 删除**；宿主批量删除守卫在累计第 **50** 次终止进程：进度停在 **40/87**、日志 50 行 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`、`exit 1`（无 traceback）、残留 **46 个 `.tmp`** | selftest `get_json_memory_read` / `get_json_tmp_cleanup` / `curl_bytes_split`；全新缓存重跑 **87/87**、**0 个 `.tmp`**、12 s |
| 2 | `curl_get` 的 `dest` 改必填，删除 `dest=None` 死分支；清掉 `tempfile` / `os` 两个死 import | 该分支生成 `_tmp/get_{abs(hash(url))}.bin` **且从不回收**，改内存读取后已无任何调用者（全脚本仅 `resolve_region.gb_geojson` 带 dest 调用） | selftest `v14_signature`；全部脚本编译通过 |
| 3 | `selftest.py` **58 → 62 项**（新增 4 个探针 key 全部同步 `EXPECTED_KEYS`） | 用例 20 的测试替身原本 mock `curl_get`，生产函数换成 `curl_bytes` 后必须逐参数对齐，否则 TypeError 会绕过 `except` | rasterio 1.5.1 / 1.5.2 **双环境各 62/62** |
| 4 | 文档明确「大区域先跑整国看预检、再用 `--bbox` 分块出图」的可执行路径 | 英国 bbox 实测 `(-8.6454, 49.8848, 1.7549, 60.8434)`，同带 15 m 口径约 **13.2 GB**；跨 3 个 UTM 带降级 EPSG:4326 后预检报 **24948.58 MB / 6540.12 M 像元** | 整国 `--max-output-mb 512` → `OutputTooLargeError`、`exit 2`，**读任何像元之前**停止；输出目录只有边界与 items.json，无 tif |

### v1.4.0 验证结果

| 验证 | 结果 |
|---|---|
| 离线回归 | default（rasterio **1.5.1**）**62/62**；cnclimate（**1.5.2**）**62/62** |
| 全新缓存整国（中文名「英国」） | 边界源已恢复（GitHub raw `206`）；6 个 10° 组 / 305 个 cell / **87 个命中**；跨 **3 个 UTM 带 [32629, 32630, 32631]** 降级 EPSG:4326；预检 **24948.58 MB** → `exit 2`，**12 s**，**0 个 `.tmp` 残留**、0 tiles |
| 英国湖区（分块 1） | `--bbox -3.30,54.35,-2.95,54.65`，2 cell / EPSG:32630，网格 1613×2325，**57.94 s**；GeoTIFF `[1,2233,1521]` 10.78 MB、有效 **3,266,746** 像元 (96.2%)、高程 **28.43–973.98 m**（Scafell Pike 官方 978 m，量级吻合）；预览 1090×1600 白底 |
| 英国本尼维斯（分块 2） | `--bbox -5.10,56.75,-4.80,57.00`，2 cell / EPSG:32630，**16.57 s**；GeoTIFF `[1,1894,1277]` 7.18 MB、有效 **2,170,225** 像元 (89.7%)、高程 **−6.84–1297.5 m**（Ben Nevis 官方 1345 m，量级吻合）；预览 1079×1600 |
| 数据零变化 | LIE / 自备边界重跑 **13.12 s**；与 v1.3.0 的 manifest **10 项全 SAME**，GeoTIFF `[1,1657,846]` **逐像元完全一致（含 NaN）**，transform / CRS / nodata 相同，最大绝对差 **0.0** |

### v1.4.0 口径说明与放弃项

| 项 | 说明 |
|---|---|
| 13.2 GB vs 24948.58 MB | 前者是**同带原生 15 m** 的离线粗算（外接框约 658×1212 km）；后者是技能预检值，英国跨 3 个 UTM 带后降级到 EPSG:4326、像元非正方形，**保守上界**进一步放大。两者口径不同，不可互相代替，也不要把前者当预检结果 |
| `SAFE_DELETE_BULK_CONFIRM_REQUIRED` | 宿主环境的批量删除守卫（单轮累计 50 次删除触发），不是技能自身异常；技能不做任何绕过，而是**消除不必要的删除**。前台命令实测可在无沙箱模式运行，但技能不应依赖该差异 |
| `--mode full` 段合并仍会逐段 `unlink` | v1.4.0 记为观察项；**v1.5.0 已消除**（实测 `curl -o` 覆盖 + `Path.replace`，改坏段截 0 B），见 v1.5.0 变更 8 |
| 高程峰值与官方山顶值差几十米 | 15 m DTM 栅格、像元中心采样与垂直基准口径不同；只用于量级交叉核对，**不作为精度结论** |

## v1.3.0（2026-11-15）· 健壮性、缓存治理与写出零复制（先测量，后动手）

流程：通读全部 8 个脚本 → 标记可疑点 → 每条用**最小合成输入 / 廉价探针**先证实再改
→ 固化进 `selftest.py`（新增探针同步 `EXPECTED_KEYS`）→ 两套 Python 环境交叉验证
→ 全新缓存端到端复跑并与 v1.2.0 成果**逐像元比对** → 清空运行时缓存。

**基线先暴露一个测试脚手架 bug**：换到 rasterio 1.5.2 跑旧 selftest 只有 **44/45**，
`average_keeps_nodata` 假失败。进一步在两个版本上测量后确认，是断言给
`bilinear` 加了方向/实现细节假设，不是生产代码回归。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | **修正 `average_keeps_nodata` 断言**：只断言「average / nearest 与 `read_masks` 真值逐像元一致」，bilinear 差异仅记录 | 400×200 小图：rasterio **1.5.2** 下 average=nearest=mask=**49**、bilinear=**57**（更多，旧断言 `bilinear <= average` 失败）；**1.5.1** 下 bilinear 与真值掩码**完全一致**（差异 0）；6000² 大图（v1.2.0 实测）bilinear **2704** < average **2809**（更少）。方向随尺度/版本变化 | 两个环境各 **58/58**；弃用 bilinear 的结论不变（大图会侵蚀 nodata） |
| 2 | `parse_args` 支持 `--k=v`，传入 `known_flags` 后未知选项显式 `GemError` + 相近候选；开关不接受 `=值` | 最小复现：`['LIE','out','--layer=dsm']` → `flags={'layer=dsm'}`（layer 静默回落 dtm）；`['LIE','--layr','dsm']` → `pos=['LIE','dsm']`（**值被当输出目录**），全程无提示 | selftest `cli_equals_form` / `cli_unknown_flag` / `cli_switch_equals` |
| 3 | `--clean-cache` 提前到 `if not pos` 之前，**不需要位置参数**；不再白建输出目录 | 实测 `gem15_dem.py --clean-cache` 旧行为：打印帮助、**exit 1** | selftest `clean_cache_no_pos`；真实 CLI `exit 0` |
| 4 | `clean_cache` 从「只清 tiles」扩展为 `scope=all` 清 `tiles/stac/boundary/_tmp` 四类 + 分项释放量；新增 `--clean-tiles-only` 保留旧语义 | **本轮开始时** `du` 实测 tiles 154 MB / boundary 728 KB / stac 237 KB / _tmp 4 KB；旧日志只报 tiles 却写「已清空」 | selftest `clean_cache_scope`（tiles-only 释放 128 B 且保留其余三类；all 释放 384 B）；真实清理日志 4 文件 / **0.70 MB**（清理时 tiles 已空，见本节口径说明）；另造 20 MiB 合成瓦片验证统计准确（日志 **20.00 MB**） |
| 5 | `get_json` 用 `mkstemp` 独占临时文件，`finally` 无条件回收；临时文件跟随 `--cache` 根目录 | 旧实现只在 JSON 解析段有 `finally`：模拟 **HTTP 504** 与 `curl_get` 抛异常，两条失败路径都在 `_tmp/` 留下错误页（实测残留 16 B）；`abs(hash(url))` 命名还有同 URL 并发覆盖风险 | selftest `get_json_tmp_cleanup`（两条路径均无 `gem15_get_*.tmp` 残留） |
| 6 | STAC link id 解析改为「`title` 优先 → href 任一段按命名正则匹配 → `[warn]` 跳过」 | 旧取 href 首段：绝对 URL → `https:`，`../gem-15/p40_p0/catalog.json` → `gem-15`，两者最终会误报 `CoverageError`；真实缓存里 href 是 `./p40_p0/catalog.json`，故一直没暴露 | selftest `link_ids_absolute`（三种 href 均得 `p40_p0`，非法 id 打印 warn） |
| 7 | `write_geotiff` 的 `arr.astype(np.float32)` 改 `np.asarray` | 成果数组本就是 float32，`astype` 默认 `copy=True`。Windows `GetProcessMemoryInfo` 微基准：6000² 单波段 **137.3 MB** 数组，旧写法 **+137.4 MB RSS**，新写法 **+0.0 MB**（同一对象）。口径：**单次数组复制的常驻集增量**，非端到端峰值；512 MB 上限成果约省 512 MB 是**线性外推**，不是实测 | selftest `geotiff_no_copy`（`np.shares_memory(written, arr)` 为真）；真实 GeoTIFF 逐像元一致 |
| 8 | 裁切框改用 `clip_bbox_for(boundary)`（**原始边界外接框**），与 `--buffer` 外扩后的取数 bbox 解耦 | 旧实现用 buffer 后的 bbox 算裁切窗口；掩膜仍按原边界，外扩部分永远无效 → 成品多一圈全 NaN 空白 | selftest `clip_bbox_buffer`（裁切框 `(9.5,47.0,9.6,47.1)`，与外扩框 `(9.4,46.9,9.7,47.2)` 不同） |
| 9 | `--bg` 值显式校验（只接受 `transparent` / `white`）；非法值抛 `GemError` | 旧实现任何非法值都落到 else 分支当透明，`--bg blue` 静默生效 | selftest `background_validation` |
| 10 | 五个子 CLI 的 `--help` 统一 exit 0（`check_help` helper） | 旧实现只有主入口修过，子 CLI `--help` 因位置参数不足打印用法后 **exit 1** | selftest `cli_help_all`（5 个模块退出码全 0） |
| 11 | 边界 GeoJSON 下载失败时清除损坏缓存并给出四条可执行建议 | 端到端实测：geoBoundaries API **200 / 1.17 s**，但 `github.com/.../raw/...` **000 / 21 s**；`raw.githubusercontent.com`、`cdn.jsdelivr.net`、simplified/topojson 同域全部 000。旧错误只有「GET 失败 000」，用户无从下手 | selftest `boundary_error_hint`；用 `--geojson` 绕开后端到端 **18.61 s** 跑通 |
| 12 | `selftest.py` **45 → 58 项**（新增 13 个探针 key 全部同步 `EXPECTED_KEYS`）；版本号同步四处 | 用户既有规范：新增探针必须加进断言列表，否则用例空转；技能改版必须同步 SKILL.md / README.md / CHANGELOG.md / `_user_meta.json` / `VERSION` / selftest | **rasterio 1.5.1 与 1.5.2 双环境各 58/58** |

### v1.3.0 验证结果

| 验证 | 结果 |
|---|---|
| 离线回归 | default（rasterio **1.5.1**）**58/58**；cnclimate（rasterio **1.5.2**）**58/58** |
| 全新缓存端到端 | 自备边界 + `--cache D:/gem15_verify_cache`，LIE / dtm / window / `--bg white`，**18.61 s**，1 个 `w9s47` cell，EPSG:32632 |
| 与 v1.2.0 成果比对 | manifest 的 `crs` / `resolution` / `output_shape` / `valid_pixels` / `valid_ratio` / 5 个高程统计共 **10 项全 SAME**；GeoTIFF `[1,1657,846]` **逐像元完全一致（含 NaN）**，transform / CRS / nodata 相同，最大绝对差 **0.0** |
| 缓存清理 | 主缓存 `D:/gem15-dem-cache` 执行 `--clean-cache` → `exit 0`、四类子目录全部删除、**剩余 0 文件**；日志 `4 个文件 / 0.70 MB（boundary 0.70 MB，tiles 0.00 MB）`。验证缓存 `D:/gem15_verify_cache` 同样清空（0.04 MB）；技能内 `scripts/__pycache__` 8 个 `.pyc` 删除 |

### v1.3.0 明确放弃 / 观察项

| 项 | 实测 / 依据 | 处理 |
|---|---|---|
| 给 geoBoundaries 加备用 CDN 回退 | 五个候选 URL 全部 `000`，只有 `www.geoboundaries.org` 元数据 API 通；同一 commit 的 `gjDownloadURL` / `simplifiedGeometryGeoJSON` / `tjDownloadURL` 全在 GitHub 同域 | **不做无依据的 URL 猜测**。改为错误信息给 `--bbox` / `--geojson` / 保留缓存三条建议，并保留 `--clean-tiles-only` |
| 对 `STAC item.raster:bands[0].nodata` 做数值化 | 真实 STAC 里是字符串 `"nan"`，但 `build_mosaic` 只用 `np.isfinite` 判有效，该字段未参与任何计算 | 记为观察项，不改动（避免为不改数据的字段引入新口径） |
| 把 `v = out[0][valid]` 的统计副本也去掉 | 大成果下会物化一份有效值数组，但 `np.nanpercentile` 内部同样需要展平副本 | 无零风险替代，本轮不动；已有 `--max-output-mb` 上限兜底，不做猜测式重构 |

> 口径提醒：`astype` 的 137.4 MB 是单次复制的**常驻集增量**，512 MB 是线性外推；
> `download_items` 的「聚合速率」是墙钟×并发摊出来的总量，不是单连接速率。
> **缓存释放量以执行时日志为准**：本轮开始时 `du` 测到 tiles 154 MB，但真正执行
> `--clean-cache` 时该目录已空（非本轮代码删除，原因未查明，不猜）；为排除统计 bug，
> 另造 20 MiB 合成瓦片复测，日志准确报出 `tiles 20.00 MB`。故文档不写「释放 154 MB」，
> 只写「清理后剩余 0 文件」这一可核实的结论。

## v1.2.0（2026-11-14）· 内存与规模优化（先测量，后动手）

流程：重新通读全部 8 个脚本 → 标记 8 个新可疑点 → 每条用最小合成数据 / 廉价探针 /
微基准**先证实再改** → 固化进 `selftest.py` → 真实端到端复跑。**只修证实过的问题。**
本轮主线是上轮未覆盖的方向：**大区域时的内存可行性**。

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | **预览图改为先降采样再渲染**：`ds.read(1, out_shape=..., Resampling.average)` 按 `--maxside` 直接读成最终尺寸，不再全分辨率算 colorize / gradient / hillshade 后 resize | 3000² 合成：**585 MB / 2.93 s → 210 MB / 1.57 s**；6000² 合成：**2160 MB / 19.98 s → 224 MB / 1.97 s**（峰值 **9.6×**、耗时 **10.1×**）。旧实现峰值随输入面积线性放大 | 真实卢森堡成果 5867×5423：**201.8 MB / 1.44 s**；两张 PNG 平均像素差 3.64/255，视觉验收右图更自然（左图残留交叉网格噪点）；selftest `preview_downsample` |
| 2 | **输出规模预检**：`assert_output_fits` 按 `网格像元数 × 波段数 × 4 B` 估算，超 `--max-output-mb`（默认 512）抛 `OutputTooLargeError`，附分块 / 换 dtm / 提高上限三条建议 | 实测推算（同带 15 m 口径）：列支敦士登 1.4 M 像元 / 5.3 MB、卢森堡 21.2 M / 80.8 MB、奥地利 746.5 M / **2847.6 MB**、法国本土 5468.5 M / **20860.9 MB**；跨带降级还会放大（奥地利 bbox 实测 1275.5 M / 4866 MB） | 奥地利 bbox 36 个 cell 在**读取任何像元之前**报错、退出码 2、无 tif；`--max-output-mb 0.1` 端到端触发、给 0 报 `GemError`；selftest `output_size_guard` |
| 3 | **流式拼接**：STAC 元数据离线规划窗口 → 提前算统一网格 → 逐片 read→paste→`del`，不再把全部分片数组常驻 `windows` | 实测 STAC asset 的 `proj:transform` / `proj:shape` / `proj:epsg` 与真实 COG 逐项一致（两个不同 UTM 带、两种 shape，窗口四元组与 `window_transform` 全等）；合成 4 片 N=4500 常驻 RSS **1139 → 830 MB（-27%）** | 列支敦士登与 v1.1.0 manifest **9 项指标全 SAME**（含 617724 有效像元、424.79–2495.55 m）；selftest `plan_window_matches_cog` |
| 4 | **跨带 reproject 只分配局部网格**，不再每片建 `(count, height, width)` 的全网格临时数组 | 旧实现跨带时临时数组与 `out` 等大，是大区域内存的第二处黑洞 | 局部 vs 全网格：有效像元掩码**逐像元一致**，平滑地形数值 mean 0.022 m / max 0.064 m；卢森堡 4 片跨 2 带真实跑通；selftest `local_reproject_equal` |
| 5 | `read_one_window` 接受 STAC 规划窗口并**校验实际 COG**；不符打印 `[warn]` 回退现场重算；`astype` 改 `np.asarray` 避免 float32 无谓复制 | 元数据可能过期，静默沿用会错位；源数据本就是 float32（实测 `nodata=nan` / `dtype=float32`） | selftest `plan_mismatch_fallback`；跨带 / dsm / full 三条链路回归 |
| 6 | 预览 hillshade 的 y/x 分辨率分开取（`res_y` / `res_x`），不再都用 `res_x` | 跨带 EPSG:4326 输出像元非正方形，旧代码两轴用同一值 | 列支敦士登 / 卢森堡白底图视觉验收通过 |
| 7 | `stac_index.select_items` 带出 `proj_transform` / `proj_shape` / `nodata`，缺字段显式报错；两处索引拉取改 `as_completed` | v1.1.0 已证实小 JSON 并发 16 最优；顺序等待会让进度被最慢请求卡住 | 卢森堡 4 片、奥地利 36 片索引正常；selftest `v12_signature` |
| 8 | 清理误导性死常量 `LAT_MIN/LAT_MAX=(-60,84)`；索引结果按 cell 排序保证 `items.json` 可复现 | Grep 确认**无任何使用**，且与真实 `m70_m50` 组（覆盖到 -61°）矛盾，是 v1.1.0 修复漏片时的遗留 | selftest `v12_signature` 断言常量已删除；AST 无残留 |
| 9 | 错误信息口径修正：`{mb:.0f}` 改 `.2f`、建议值加 `max(1, ceil(...))` | 端到端实测 `--max-output-mb 0.1` 时显示「0 MB 超过上限 0 MB」且建议 `--max-output-mb 0` | 修复后显示「0.19 MB … 上限 0.10 MB … `--max-output-mb 1`」 |
| 10 | `selftest.py` 38 → **45 项**；新增 7 个探针全部同步进 `EXPECTED_KEYS` | 用户既有规范：新增探针必须加进断言列表，否则用例空转 | **45/45 通过**（离线，最小合成 GeoTIFF + 297 个真实 child 组 fixtures） |

### v1.2.0 明确放弃的方案（有实测依据，避免以后重复踩坑）

| 方案 | 实测 / 依据 | 放弃原因 |
|---|---|---|
| **把分片数组 spill 到磁盘 memmap / np.save 再读回** | STAC asset 自带 `proj:transform` / `proj:shape`，可**纯离线**算出窗口与统一网格（已实测与真实 COG 全等） | 零额外网络、零额外磁盘即可流式，spill 只会增加数 GB 临时磁盘 I/O 与清理逻辑 |
| **两趟打开 COG（第一趟只读头部算网格）** | 同上；且 `/vsicurl/` 打开实测约 2.8 s/片，两趟等于翻倍网络开销 | 元数据已够用，无需第二次打开 |
| **预览降采样用 `Resampling.bilinear`** | 6000² 合成图：average / nearest / read_masks 的 nodata 均 **2809**，bilinear 只有 **2704** | bilinear 会侵蚀 nodata 边界；average 还顺带平滑高频噪声，视觉更自然 |
| **要求局部重投影与全网格「位级一致」** | 病态高频锯齿（相邻像元跳变 899 m）差异 mean 3.28 m；平滑地形仅 0.022 m / max 0.064 m；两种地形有效掩码都逐像元一致 | GDAL 近似 warp transformer 在不同 destination 尺寸下不保证位级一致；正确验收口径是「掩码逐像元一致 + 0.1 m 容差」，不是位级相等 |

> **口径说明（重要）**：
> ① 输出规模推算是**同带原生 15 m 口径**；跨 UTM 带降级到 EPSG:4326 后像元非正方形，
> 像元数与内存会进一步放大（奥地利同带 746.5 M / 2847.6 MB，跨带实测 1275.5 M / 4866 MB）。
> ② 流式内存微基准用合成地形生成，生成过程的临时数组会同时抬高两条曲线的**峰值**，
> 因此以**常驻 RSS 差值 -27%** 为准；峰值差只作参考，不当作单次行为的定论。
> ③ 预览渲染的两组数值是独立子进程用 `GetProcessMemoryInfo` 取的**峰值工作集**，
> 含 Python / rasterio 基础占用（约 53 MB），不是纯数组大小。
> ④ 真实端到端耗时含网络抖动：卢森堡 167.64 s 中 4 片窗口读取分别为
> 48.1 / 10.0 / 61.5 / 26.3 s，属单次请求方差，不作为吞吐结论。

## v1.1.0（2026-11-14）· 审计后优化（先测量，后动手）

流程：通读全部 8 个脚本 → 标记 8 个可疑点 → 每个猜想用最小合成数据 / 廉价探针
**先证实再改** → 改完固化进 `selftest.py` → 真实端到端复跑。**只修证实过的问题。**

| # | 变更 | 依据 / 测量（ground truth） | 验证 |
|---|---|---|---|
| 1 | **修复南纬 -61° 漏片**：候选 10° 组改为解析 collection 的真实 child 组名 + 10° 外接框正面积相交，不再由硬编码 `LAT_LIMITS=(-60,80)` 生成 | 实测 collection 确有 `m70_m50` 组（cell `wm45sm61`，纬度 -61°）；旧 `groups_for_bbox((-44.8,-60.8,-44.6,-60.6))` 返回 `[]` → 直接 `CoverageError`；297 个真实组名全部可解析，0 个失败 | selftest 新增 `parse_group` / `fixture_children` / `dynamic_groups_*`；端到端从 CoverageError 变为成功产出 GeoTIFF（`wm45sm61`，EPSG:32723，185 有效像元，退出码 0） |
| 2 | **修复 `stac_index.py` 独立 CLI 参数失效**：`--layer` / `--cache` 加入 `CLI_WITH_VALUE` | 实测 `parse_args([...,'--layer','dsm','--cache','D:/x'], ('--workers',))` 把两个值塞进位置参数、`opts={}`；带 `--layer dsm` 跑 CLI 输出仍是 `/dtm/zall/` | 修复后独立 CLI 输出 `/dsm/zall/`；selftest `cli_stac_args` |
| 3 | **修复 `--help` 假失败**：`--help` 进 flags、`-h` 进 pos，两者统一打印帮助并退出 0 | 实测 v1.0.0 `--help` 打印了帮助但退出码 **1**（`return 0 if pos else 1`），`-h` 为 0 | 修复后两者均 0；selftest `cli_help_exit` |
| 4 | **断点缓存加指纹**：`.parts/_manifest.json` 记录 url / size / chunk_bytes / segments，不匹配即 `[warn]` 并清空重下 | 最小合成数据证实：16 MiB 与 32 MiB 分段的**尾段偏移+长度完全相同**，仅按「段号 + 长度」复用无法区分 URL / chunk 变化 | selftest `parts_manifest` 三态；真实链路注入 STALE 指纹 → 打印 warn、清空旧段、重下 380176 B、COG 可打开 |
| 5 | **STAC 索引并发 8 → 16**（`INDEX_WORKERS`） | 微基准：9 个 item / 12 个请求，全新缓存，workers 2/8/16 = **8.54 / 6.06 / 4.23 s**；复测 8 = 4.62/6.28 s、16 = 4.43/4.34 s，16 更快且方差更小。口径：小样本含网关抖动，且**只适用于小 JSON** | selftest `index_workers_default`；列支敦士登端到端 10.63 s（缓存热） |
| 6 | **窗口重试去掉固定 `sleep(2)`**：改短等递增且最后一次失败不等 | v1.0.0 `retries=2` 时每次失败固定等 2 s，含**最后一次无意义的 2 s**，合计白等 6 s | selftest 用 mock 记录：新行为只等 `[1,2]` 共 3 s，且捕获 `DownloadError` |
| 7 | `download_items` 改用 `as_completed` + 结果按 cell 排序 | 原先按提交顺序等待，进度打印会被最先提交但最慢的片卡住（可观测性问题，不改总耗时） | full 模式首次 / 缓存复跑均正常 |
| 8 | 死代码清理：`curl_get` 移除 `resume` 参数 | Grep 确认 full 模式改分段后**无任何调用者**；且 `curl -C -` 遇服务端忽略 Range 返回 200 时会**追加**到旧文件，字节校验必然失败并陷入重试 | AST + Grep 无残留；8 脚本语法全过 |
| 9 | 代码注释与实现同步：`gem15_common.py` 顶部仍写「整文件下载用单连接」，与 v1.0.0 已改的 16 MiB 串行分段矛盾 | 文档与代码不一致会误导后续调参 | 改为串行分段 + 明确记录并发窗口放弃依据 |
| 10 | `selftest.py` 28 → **38 项**；`fixtures_stac.json` 固化 297 个真实 child 组 | 新增探针 key 全部同步进 `EXPECTED_KEYS`（防止用例空转） | 38/38 通过 |

### v1.1.0 新增的「明确放弃」（实测反直觉，避免以后重复踩坑）

| 方案 | 实测 | 放弃原因 |
|---|---|---|
| **并发打开多个 `/vsicurl/` COG 窗口** | 4 片串行 **18.87 s** vs 4 线程并发 **48.89 s**（0.39×），数组逐像元一致 | 慢 2.6 倍；与「STAC 小 JSON 并发有收益」相反，说明并发收益取决于请求类型。窗口读取保持串行 |
| **按同带/跨带动态缩小 PAD（48→4）** | 冷缓存 17.08 s → 10.85 s（看似 1.57×），**热缓存复测 10.29 s vs 10.55 s 无差** | 那 6.8 s 是单次慢请求方差（用户既有教训：失败请求耗时方差极大），不能归因于 PAD；不做无收益重构 |
| **用 `collection.item_assets` 省掉逐 item 请求** | 实测 `item_assets` 只有波段名 / dtype / nodata / 分辨率，**不含 href / size / epsg**；组 catalog 的 item link 也只有 rel/href/type/title | 逐 item 请求是 STAC 结构决定的必需路径，只能靠缓存与并发缓解 |

> 口径说明：STAC 并发微基准是 9 个 item 的小样本，两轮数值含网关抖动；
> 结论只用于「小 JSON 请求并发度」这一项，不外推到大 COG 或 GDAL 窗口读取。

## v1.0.0（2026-11-14）· 首版

| # | 变更 | 依据 / 测量 | 验证 |
|---|---|---|---|
| 1 | 建立技能骨架：SKILL.md frontmatter（name / version / display_name / description_zh / description_en / agent_created / argument-hint）+ README + CHANGELOG + _user_meta.json + requirements + selftest | 参照 `cn-dem` 技能规范；用户明确要求 README 与 WorkBuddy 规范 | 文件齐备，版本号四处一致 |
| 2 | 数据源接入：GCS 公开 COG + 官方静态 STAC（catalog → collection 297 组 → 10° 组 catalog → 1° item → assets），免 API Key | 实测 `stac/catalog.json` 638 B、`collection.json` 45 KB；item 带 `file:size` / `proj:epsg` / `raster:bands` | `fixtures_stac.json` 固化 6 个真实 item，selftest 交叉校验命名/URL/epsg |
| 3 | 全球区域解析：中文名 / 英文名 / ISO3 → geoBoundaries ADM0；`--adm1` 一级行政区；`--bbox` / `--geojson` | 实测 geoboundaries API 可达（0.9–3 s）；`raw.githubusercontent.com` / `cdn.jsdelivr.net` 本机不可达；ADM1 完整版 72.7 MB、简化版 1.67 MB | 列支敦士登边界 bbox 9.4717–9.6357E / 47.0484–47.2706N，1 要素 |
| 4 | 覆盖排除显式判空：CHN/RUS/MNG/GRL/ATA 名称直接报错；bbox 落在排除区由 STAC 0 片兜住 | 官方声明覆盖 60°S–84°N 并排除上述区域；STAC 实测 `p30_p110` 组不存在 | 名称与 bbox 两种输入均退出码 2，提示改用 cn-dem，不生成空文件 |
| 5 | 分片架构：① 按 1° cell 分片，② 每片按 COG 窗口（`/vsicurl/` Range）按需取数，为默认 `window` 模式 | `/vsicurl/` 打开 2.76 s；256² 窗口 2.61 s、1024² 窗口 5.25 s；山区单 COG dtm 153 MB / dsm 791 MB | 列支敦士登端到端 42.79 s，只读 938×1749 窗口 |
| 6 | `full` 模式改为 **16 MiB 串行分段 + 段级断点缓存 + 逐段字节校验**；明确放弃字节级并发分块与整文件单连接 | 实测：16 MiB 串行 1.61 MB/s；8 并发 1 MiB 0.70 MB/s；16 并发 2 MiB 0.73 MB/s；8 分块 par=4 0.32 MB/s；整文件 153 MiB 单连接 368.1 s（6.8 MiB 处截断） | 首次 134.4 s（1 次分段截断自动重下），缓存复跑 0.09 s |
| 7 | 拼接：numpy first-valid；同 UTM 带零重采样，跨带逐片 reproject 到 EPSG:4326 并打印 `[warn]` 写入 manifest | 实测相邻 cell 的 15 m transform 对齐；`w11s47`=32632 / `w12s47`=32633 跨带 | 跨带小 bbox 6.1 s，2 片，降级可观测 |
| 8 | 掩膜几何投影到成果 CRS（`rasterio.warp.transform_geom`），显式 NaN 防 0 值黑边 | 真实端到端暴露：WGS84 边界直接喂 UTM 网格的 `geometry_mask` 会整体错位 | 列支敦士登成果 `any_zero_or_negative32768=False`，界外 NaN |
| 9 | 统一网格改为以窗口自身像素原点对齐 | 最小合成数据暴露：强制对齐绝对 0 原点会多一行一列并整体错位（129×65 ≠ 128×64） | 修复后 128×64；新增"窗口未对齐" `[warn]` |
| 10 | 判空与可观测：0 片 / 空窗口 / 0 有效像元全部抛异常；缓存损坏、分段校验失败、几何字体缺失均打印 `[warn]` | 既有工程教训：静默 `except` 会伪装成功 | selftest `require_valid_empty` / `cache_*` / `decide_crs_degrade` 均断言 |
| 11 | 预览图：2–98% 分位拉伸 + 双轴米制 hillshade；极简标题、罗盘式指北针、图形比例尺；nodata 透明 / `--bg white` | 用户既有科研级出图规范（剔除元数据副标题、线状与图形要素、真实克制） | 视觉验收通过；标题改为单次描边避免中文糊字 |
| 12 | 输出 GeoTIFF 写入许可、署名、垂直基准、cell 清单标签；manifest 记录分片/耗时/统计/降级 | CC-BY 4.0 署名要求 | rasterio 读回 tags 核验 |
| 13 | `selftest.py` 28 项离线回归 + `fixtures_stac.json`；EXPECTED_KEYS 强制新增探针同步断言 | 用户既有规范：最小合成数据优先、新增探针必须加进断言列表 | 28/28 通过 |

### 明确放弃的方案（保留依据，避免以后重复踩坑）

| 方案 | 放弃原因 |
|---|---|
| 字节级并发 Range 分块（8–16 并发） | 本机 GCS 实测 0.32–0.73 MB/s，比单连接 1.89 MB/s 慢 2.6–5.9 倍；与 AWS S3 的经验相反 |
| 整文件单连接下载大 COG | 153 MiB 响应在 6.8 MiB 处被网关截断，断点续传后仍耗时 368.1 s |
| 依赖 `raw.githubusercontent.com` / `cdn.jsdelivr.net` 取边界 | 本机实测均超时不可达；改用 `www.geoboundaries.org` API + `github.com/.../raw/` |
| 用 `pyproj.Transformer.transform_bounds` 做坐标转换 | 该方法属于 `rasterio.warp`，pyproj 上会抛 `TypeError`；被静默 except 吞掉会让边界避让全程失效 |
| 对 GEM-15 的 no-data 空洞做插值补洞 | 水体/冰雪/低纹理掩膜是数据源特性，插值会伪造高程；保持真实 NaN |

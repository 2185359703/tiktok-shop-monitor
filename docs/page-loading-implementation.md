# 页面读取层的实现与启用

读取层已于 2026-10-16 部署到 [ShopScope](https://shopscope-monitor.top/)，完成 46,901 个商品的当前观察和最新有效快照回填。增量汇总、Redis 缓存、独立分析进程、前端打包及 HTTP/2 已启用。最终应用版本为 `82e2785`，详情见 [最终验收记录](evidence/page-loading-final-20261016.json)；此前上线记录保留在 [第一阶段记录](evidence/page-loading-release-20261016.json)。

最终版本通过 653 项 Python 检查和 7 项前端缓存检查，2 项依赖独立 Redis 测试环境的检查跳过；线上已确认 Redis 写入和读取缓存。持续采集期间，最后三次采样的商品投影最老等待为 6–8 秒，默认图表最大数据年龄为 66–68 秒。缓存页面返回的点击到下一绘制帧实测约 122 毫秒。当前跨网络首屏仍有 5.25–13.13 秒波动，尚未达到 1–2 秒目标；服务端预热启动接口样本为 276–404 毫秒。首次访问等待和并发容量不能用隔离库或缓存命中数据替代。

设计依据见 [页面加载性能设计](2026-10-15_页面加载性能设计.md)。实现保留 FastAPI、MySQL 和 RabbitMQ，使用原生异步数据库驱动。生产部署分为三个分析进程：商品更新与轻量计数、默认图表、复杂筛选与匹配。CPU 密集统计不能占住商品更新的事件循环；复杂分析和报告仍共享重计算租约。

## 已实现的读取链路

| 页面资源 | 新行为 |
| --- | --- |
| 商品列表、图表下钻 | 只查当前页，使用额外一行判断下一页；不 COUNT 全市场、不读取历史快照、不现场聚合 SKU |
| 商品总数与覆盖率 | 独立统计请求；后台仅聚合窄商品投影，不重复计算整个工作台 |
| 市场总览、店铺统计 | 读取已发布摘要；保留最新有效快照口径，响应不含完整商品数组 |
| 工作台 | 读取已发布的一代统计，按当前主题返回；保留历史质量指标、精确中位数和 SKU 观察成员规则 |
| 事件 | 按观察时间/事件 ID 分页；游标固定筛选、窗口和已提交投影发布序号；数量单独加载 |
| 任务与匹配 | 列表先显示，状态数量、候选建议独立补充；匹配摘要不再顺带重算候选 |
| 报告 | 页面只读任务清单和已保存摘要；预览、下载指向明确的已完成报告 |
| 商品详情 | 基础信息、图片、规格、历史独立显示；初次只载入一页规格与历史，沿用“更多”入口 |

前端缓存按查询条件复用、合并在途请求，预算为 50 个响应/8 MiB。失效时保留旧数据；旧请求不能覆盖新筛选。未知总数显示“正在统计”，不当作零。统计没有成品时快速返回 202，过期但仍可用时返回上次结果并合并排队刷新。

总览和工作台即使超过常规陈旧期，也保留上次成功图表，并明确显示“缓存已过期”和生成时间；不会把它标成最新结果。任务、事件和商品数量仍使用各自的短期缓存规则。刷新已排队时，读取请求和维护任务都不重复锁住队列行。

## 配置与迁移

`ANALYTICS_ENABLED=1` 控制业务事务中的变更标记和 v2 读取层。所有会写商品、快照、图片、店铺或关系的进程必须运行带标记的新代码；不要只更新 API。价格、库存和图片变化都不能只靠原 `data_version` 判断。

在项目根目录预览与检查增量迁移：

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_analytics
.\.venv\Scripts\python.exe -m scripts.migrate_analytics --check
```

第一条完全离线；第二条只检查已配置数据库。执行发布迁移的命令为：

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_analytics --execute
.\.venv\Scripts\python.exe -m scripts.migrate_analytics_rollups --execute
.\.venv\Scripts\python.exe -m scripts.migrate_recent_index --execute
```

008 迁移创建 `product_read_models`、`event_read_models`、`analytics_refresh_requests` 和 `analytics_snapshots`，不 ALTER 原业务表。随后执行 `python -m scripts.migrate_ui_indexes --execute`，为投影分页、店铺/标题/SKU/库存排序、最近详情和任务/报告查询补充七个索引。009 使用在线索引 DDL，不修改列和业务行；重复执行会跳过已有索引。对应 SQL 为 [008_analytics_read_models.sql](../migrations/008_analytics_read_models.sql) 和 [009_ui_read_indexes.sql](../migrations/009_ui_read_indexes.sql)。完整模式快照已同步注册模型。

若检查发现原有功能的表结构差异，先完成对应版本的既有迁移；008 不会修复这些差异。刷新表使用独立的待处理索引，已完成的商品不会在每轮领取时被全表扫描。

010 新增 `analytics_product_rollups` 和 `product_read_attributes`，不修改原业务表。每个口径按商品 ID 划分最多 128 个商品的分片。发布商品投影时，在同一事务中替换分片成员并重算该片摘要；修订号阻止迟到的回填覆盖新贡献，回滚也会撤销摘要变化。默认图表只合并摘要，保留全部价格以计算精确中位数、原始属性标签和 SKU 计数。新鲜度分桶按读取时刻计算；店铺名称从当前店铺表读取。

属性标签随商品投影在同一事务中替换，颜色/尺码和服装属性筛选使用独立的值索引，避免逐页展开全部商品 JSON。前缀索引只用于缩小候选范围，完整标签继续按数据库的比较规则校验，重复标签不会重复返回商品。首次回填完成前使用原有筛选实现。

011 在线新增专用于最近观察排序的紧凑覆盖索引。实际 MySQL 执行计划确认它直接倒序读取索引；原宽索引在这条查询上仍触发全范围排序。翻页先取 ID，再读取这一页的 JSON，类目/属性筛选同样沿有序索引推进。

分片首次回填可中断恢复，完成前仍使用原来的完整查询。任意过滤继续沿用 SQL 过滤语义；不使用默认范围摘要替代筛选结果。已经启用 v2 的服务器可用发布脚本的 `--phase upgrade` 添加新表并启动候选 API/分析服务；等待 `scripts.analytics_status` 的 `rollup_backfill.ready` 后再执行 `--phase cutover`。升级会保存上一份公开覆盖配置，复用基础设施和原有采集参数。

迁移后更新并平滑排空旧写入进程，再启用标记。后台运行器自动执行可恢复回填、缺失 ID 核对、投影追平和默认统计预热：

```powershell
$env:ANALYTICS_ENABLED='1'
$env:ANALYTICS_CONCURRENCY='1'
.\.venv\Scripts\python.exe -m scripts.run_analytics --status
.\.venv\Scripts\python.exe -m scripts.run_analytics --once
.\.venv\Scripts\python.exe -m scripts.run_analytics
```

`--status` 只读；`--once` 会处理一个调度批次和配置允许的派生工作项；常驻模式持续处理。运行器不请求 TikTok，不创建网站采集任务。首次回填可能较久，`ready` 仅在源 ID 覆盖完整且投影积压已追平后变为 true；统计快照尚未生成的区域返回 pending，不能将空缓存显示为零条业务数据。

生产 Compose 可在原部署组合后追加 [compose.analytics.yml](../deployment/compose.analytics.yml)，并将 `ANALYTICS_IMAGE` 设为本次 API/Worker 使用的同一冻结镜像。覆盖文件启用写入标记并增加独立分析服务；它不能代替先更新原服务镜像。还需检查原有静态前端和补采状态模块的只读挂载，避免旧目录覆盖新镜像。

当前 API 与后台使用原生异步数据库入口，SQL 会话预算在连接归还前恢复。每批最多领取 8 个商品，批量预取店铺、图片、SKU 和历史事实；所有成员通过租约校验后一起发布。刷新期间到达的新修订仍保留待处理状态，失败批次中的成员分别重试。统计/报告共享租约，失去租约的旧执行者不能发布。

刷新队列使用 READ COMMITTED 和待处理索引，统计来源使用独立的 REPEATABLE READ 视图。统计沿主键每次读取 512 条必要字段，不传输图片链接等无关大字段，也不创建完整投影 ORM 对象。相同筛选下的 24 小时、7 天和 30 天统计在同一读取视图内共用一次商品聚合，再分别计算事件窗口。商品列表先从索引选出一页 ID，再读取该页 JSON；需要将未知值放在末尾时，分别读取已知/未知区间的有界排序前缀，再合并成一页，不做全量 COUNT，也不让全表 JSON 参与排序。

商品投影持续增量更新，市场、工作台和商品覆盖率摘要的新鲜期为 60 秒，从来源视图时间计时；事件统计为 30 秒，任务数量为 15 秒。`analytics` 使用 `--role projections`（0.25 核、192 MiB），`analytics-summaries` 使用 `--role summaries`（0.6 核、256 MiB），`analytics-heavy` 使用 `--role heavy`（0.15 核、256 MiB），总 CPU 上限为 1 核。各进程有独立的数据库连接池和刷新槽。默认图表合并分片时主动让出执行权，保证租约续期。`--once` 仍处理所有种类，适用于手动诊断。

API 配置 `REDIS_URL` 时缓存统计响应，最多保留 32 个键、每键 200 KiB。新鲜图表最多缓存 30 秒，且不会跨过来源视图的新鲜期；其他响应最多缓存 3 秒。键包含数据库命名空间和完整查询范围。Redis 超时或不可用时回读 MySQL，短暂熔断避免重复等待；pending 和超大响应不缓存。并发相同查询共用一次数据库读取，一个请求取消不取消其他请求。Redis 既有采集限速键和 `noeviction` 策略保持原设置。

## 验证入口

前端模块和样式使用固定版本 esbuild 合并，运行时不需要 Node.js。修改前端源文件后运行 `npm ci --ignore-scripts`、`npm run build:frontend`，并提交生成的 bundle 和 `build-manifest.json`；测试会检查源文件与产物哈希。页面在脚本下载期间就请求 bootstrap，小尺寸 Logo 保留原来的显示尺寸与图案，减少启动链路和传输量。

HTTPS 入口通过 `deployment/enable_ui_http2.py --project-root /opt/tiktok-shop-monitor --execute` 启用 HTTP/2。脚本只接受已存在且仅属于 ShopScope 域名的站点文件，保存原配置、校验 Nginx 后平滑重载；不改证书、路由或其他站点配置。首次 bootstrap 位于样式表之前，避免等待 CSS 才开始请求。

同一次页面切换中的重绘合并到一个动画帧，避免缓存命中时重复创建工作台图表。仅修改前端时可使用 `deployment/publish_frontend.py`：它先确认全部应用容器对应当前后端版本，再逐文件比较新旧后端代码、配置、模板和迁移；存在后端差异会拒绝静态发布。通过后更新静态入口，并在活动发布记录中单独保存前端提交号。

默认工作台启动时请求 `bootstrap?initial=workbench`，把已经发布的 24 小时工作台结果与脚本并行下载，并预填五个主题的缓存。存在店铺 URL 筛选、窗口不匹配或快照尚未生成时，继续使用正常的独立请求。bootstrap 不执行统计构建；过期缓存保留原时间并明确显示陈旧状态，后台重新获取最新版本。

当前 4 GiB 服务器的 MySQL 容器上限为 1280 MiB，缓冲池为 512 MiB；此前 1 GiB 上限造成了持续内存回收。`deployment/tune_mysql_cache.py --project-root /opt/tiktok-shop-monitor --execute` 会验证项目归属、备份并更新 Compose 配置，再热调整容器上限，避免下次部署恢复旧值。该操作不重启 MySQL。

以下测试使用内存数据库或专用本地 MySQL 测试库，不允许把业务库赋给 `MYSQL_TEST_URL`：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ui_read_models.py tests/test_ui_read_models_mysql.py tests/test_analytics.py tests/test_schema.py tests/test_frontend.py -q
node --test tests/ui_query_cache.test.mjs
```

MySQL 测试库必须是尚不存在的 `tiktok_shop_monitor_test_*`，主机仅允许 localhost/127.0.0.1；测试夹具负责创建和删除。测试覆盖分页 SQL 不含全范围聚合、原工作台数值对照、快照陈旧读取、修订合并、事务回滚、游标隔离、MySQL 会话预算恢复和原生异步 API。

实际响应继续使用现有图表字段，刷新状态与版本放在 `_meta`，避免整体改写所有渲染器。根路径和 `/shopscope/` 的静态资源、API、字体与报告下载均使用对应部署前缀。

可使用 `tests/serve_ui_read_models.py --help` 启动明确标记为 TEST FIXTURE 的独立浏览器环境。该入口只接受全新的本地测试库，不能指向已有库。浏览器检查已确认分页请求与摘要请求分开，翻页、筛选、主题切换、事件和报告清单使用实际 FastAPI/MySQL 链路；这不构成生产规模延迟验收。

初始验证记录见 [实施验证摘要](evidence/page-loading-implementation-20261015.json)。2026-10-16 的完整回归为 632 passed、2 skipped；跳过项依赖 Redis。此后的查询计划和前端状态调整分别通过相关 MySQL/读取层检查及前端检查，具体版本记录在上线验收文件中。前端请求缓存的 5 项 Node 检查通过。

正式 HTTPS 入口已验证工作台、总览、店铺、商品翻页、匹配、事件、任务和报告页面。工作台主题切换正常，字体加载成功，浏览器控制台无错误；普通页面不请求整份历史报告。服务器本机接口与浏览器跨网络耗时分别记录，不能混为一个延迟指标。

## 回滚与待测范围

回滚需要先停止派生分析，再按服务版本/开关回退；保留业务历史、派生表和已生成报告。已启用的新接口报错或回填未完成时，前端不会自动请求旧完整报告来兜底。仅显式关闭 `ANALYTICS_ENABLED` 或对接旧版 API 时采用兼容读取路径。

完整 46,901 条投影的只读工作台构建曾用时约 155 秒。新的 [5 万商品隔离测量](evidence/page-loading-rollups-local-20261016.json) 验证了汇总与全量扫描的完整结果一致：工作台构建从 13.429 秒降至 1.169 秒；每种请求重复 20 次，所测分页 p95 为 16–361 毫秒、统计读取 p95 为 28–42 毫秒。测试关闭 Redis，包含第 1/500/1900 页、价格/标题/店铺排序和类目/颜色/关键词筛选。这些是本地确定性合成数据的测量，生产网络耗时和并发容量需要分别记录。

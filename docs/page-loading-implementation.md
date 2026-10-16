# 页面读取层的实现与启用

本次代码实现了独立的商品/事件读取表、持久统计缓存、后台刷新、v2 页面接口和前端分区加载。生产启用需要迁移、更新所有业务写入进程并完成历史回填。仅部署代码不会改变现有 `ANALYTICS_ENABLED` 开关；本文命令用于单独启用与回填该读取层。

设计依据见 [页面加载性能设计](2026-10-15_页面加载性能设计.md)。实际实现复用了原生异步 API 路由、MySQL 驱动和分析运行器；耗时统计与报告共享一个重计算租约。小批次投影并发由 `analytics_concurrency` 控制，正常运行设为 1，首次回填设为 4。

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
```

008 迁移创建 `product_read_models`、`event_read_models`、`analytics_refresh_requests` 和 `analytics_snapshots`，不 ALTER 原业务表。随后执行 `python -m scripts.migrate_ui_indexes --execute`，为投影分页、最近详情和任务/报告查询补充三个覆盖索引。009 使用在线索引 DDL，不修改列和业务行；重复执行会跳过已有索引。对应 SQL 为 [008_analytics_read_models.sql](../migrations/008_analytics_read_models.sql) 和 [009_ui_read_indexes.sql](../migrations/009_ui_read_indexes.sql)。完整模式快照已同步注册模型。

若检查发现原有功能的表结构差异，先完成对应版本的既有迁移；008 不会修复这些差异。刷新表使用独立的待处理索引，已完成的商品不会在每轮领取时被全表扫描。

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

刷新队列使用 READ COMMITTED 和待处理索引，统计来源使用独立的 REPEATABLE READ 视图。统计沿主键每次读取 512 条必要字段，不传输图片链接等无关大字段，也不创建完整投影 ORM 对象。相同筛选下的 24 小时、7 天和 30 天统计在同一读取视图内共用一次商品聚合，再分别计算事件窗口。商品列表先从覆盖索引选出一页 ID，再读取该页 JSON；最近观察时间和 ID 同向排序，其他排序也不携带全表 JSON 参与排序。

商品投影持续增量更新，市场、工作台和商品覆盖率摘要的刷新间隔为 300 秒；事件统计为 30 秒，任务数量为 15 秒。后台总预算通过 `ANALYTICS_BUILD_BUDGET_SECONDS` 配置，默认 300 秒。页面和商品投影的单条 SELECT 限制为 3 秒，后台统计为 30 秒；连接归还前恢复原设置。首次回填临时使用最多 1 核 CPU，正式切换后恢复 0.5 核。现有 Redis 的用途和淘汰策略不变。

## 验证入口

以下测试使用内存数据库或专用本地 MySQL 测试库，不允许把业务库赋给 `MYSQL_TEST_URL`：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ui_read_models.py tests/test_ui_read_models_mysql.py tests/test_analytics.py tests/test_schema.py tests/test_frontend.py -q
node --test tests/ui_query_cache.test.mjs
```

MySQL 测试库必须是尚不存在的 `tiktok_shop_monitor_test_*`，主机仅允许 localhost/127.0.0.1；测试夹具负责创建和删除。测试覆盖分页 SQL 不含全范围聚合、原工作台数值对照、快照陈旧读取、修订合并、事务回滚、游标隔离、MySQL 会话预算恢复和原生异步 API。

实际响应继续使用现有图表字段，刷新状态与版本放在 `_meta`，避免整体改写所有渲染器。根路径和 `/shopscope/` 的静态资源、API、字体与报告下载均使用对应部署前缀。

可使用 `tests/serve_ui_read_models.py --help` 启动明确标记为 TEST FIXTURE 的独立浏览器环境。该入口只接受全新的本地测试库，不能指向已有库。浏览器检查已确认分页请求与摘要请求分开，翻页、筛选、主题切换、事件和报告清单使用实际 FastAPI/MySQL 链路；这不构成生产规模延迟验收。

初始验证记录见 [实施验证摘要](evidence/page-loading-implementation-20261015.json)。2026-10-16 的完整回归为 614 passed、2 skipped；跳过项依赖 Redis。回归包含分页、批量预取、精简统计字段、不同时间窗口共用聚合、队列批量合并和租约隔离检查。随后默认分页的索引约束通过 40 项相关检查。前端请求缓存的 5 项 Node 检查通过。

## 回滚与待测范围

回滚需要先停止派生分析，再按服务版本/开关回退；保留业务历史、派生表和已生成报告。已启用的新接口报错或回填未完成时，前端不会自动请求旧完整报告来兜底。仅显式关闭 `ANALYTICS_ENABLED` 或对接旧版 API 时采用兼容读取路径。

生产探测中，34,156 条投影的工作台构建耗时约 117 秒；这是后台构建耗时，不是页面响应时间。依据这次实测，已增加后台预算、精简传输字段并复用时间窗口聚合。并发容量及 p95 延迟仍需专门的负载测试，不能用单次访问耗时替代。

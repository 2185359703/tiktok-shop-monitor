# 采集管理与采集监控

本文记录首版 `c821118` 的实现与发布证据。当前扩展版的操作、完整窗口统计、批次取消和持久告警见 [采集控制台扩展版](collection-console-v2.md)。下文的两万条抽样与八秒冷读是首版历史口径。

采集控制台复用现有 FastAPI、MySQL、RabbitMQ、Redis 和 Worker，不再增加一套调度系统。入口为 `#collections`（采集管理）、`#collection-monitor`（采集监控）；`#tasks` 继续提供任务筛选与执行尝试详情。

## 管理能力

- 保存单次或周期计划，明确填写当前市场的资源内部 ID，每个计划最多 100 个资源，实际数量还受调度批次预算限制。
- 店铺档案计划支持不少于一小时的周期，商品快照计划支持不少于一分钟的周期。未在服务器启用的采集器不能创建或执行计划。生产环境原有采集器配置保持不变。
- 店铺发现、商品发现、首次详情采集沿用单次任务入口；这些任务具有初始发现幂等语义，不包装成伪重复采集计划。
- 计划可以编辑、暂停、恢复和立即运行。启用后由已有 Scheduler 自动生成持久任务，窗口遗漏会合并，已有执行及补采未完成时不会持续叠加新执行。
- 全局人工暂停持久化在 MySQL，阻止领取新的来源任务，允许已有租约完成；变化检测、报表等本地任务继续执行。恢复人工开关不清除来源站点的 Redis 保护性冷却。
- 取消只作用于尚未运行的任务。失败或取消任务的补采创建关联的新任务，保留原始任务、尝试次数、错误分类和断点。重复提交同一次补采不会创建多个子任务。
- 执行记录展示本次明确关联的根任务，根任务完成不意味着所有派生任务已经完成。管理操作保存结构化审计，不记录令牌、原始请求或错误正文。

读接口沿用现有应用权限边界；管理写接口复用配置中的 API Token。前端只在当前页面内存中保存输入的令牌，刷新后需要重新连接，令牌不会写入本地存储或 URL。

## 监控口径

页面支持最近 15 分钟、1 小时、6 小时和 24 小时窗口，展示完成尝试数、成功率、平均和 P95 执行耗时、每分钟完成量、时间趋势、错误分类和最近失败任务。

成功率按执行尝试计算，失败后重试成功仍然分别记录一次失败和一次成功。窗口按尝试完成时间筛选，执行耗时包括该次业务执行。最多读取最近 20,000 次完成尝试；超过上限时明确标记截断，窗口吞吐成为下界。最多显示 200 个运行租约，超过上限也会标记。

队列数量读取已有分析进程发布的任务摘要，显示摘要来源时间。没有摘要时显示未知，不把缺失数据当作零，也不在请求中扫描全任务表重新聚合。

Worker 与 Scheduler 每 10 秒向 Redis 发布一次心跳，覆盖空闲进程。30 秒未更新标记过期，60 秒标记离线；历史注册保留 10 分钟。Redis 不可用或没有近期注册时明确显示监控未知，不能由“没有正在执行的任务”推断进程离线。心跳不参与任务租约或限速，监控故障不会中断采集。

监控响应合并并发请求并缓存 10 秒；前端每 15 秒刷新，离开页面或隐藏标签页时停止轮询。刷新失败保留上次结果并显示错误。监控会提示租约过期、摘要过期、尝试失败率过高、心跳缺失及来源冷却。调度器在线但超过 120 秒或三个调度周期没有成功执行时，会单独提示调度停滞；成功时间未知时也会明确提示。这些为页面内告警，不会向外部通讯工具发送消息。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET/PUT /api/v1/collection-management/control` | 查看或调整人工暂停 |
| `GET/POST /api/v1/collection-management/plans` | 分页计划列表与创建 |
| `GET/PATCH /api/v1/collection-management/plans/{id}` | 查看或编辑计划 |
| `POST /api/v1/collection-management/plans/{id}/run` | 按请求 UUID 幂等执行 |
| `GET /api/v1/collection-management/plans/{id}/runs` | 分页执行历史 |
| `GET /api/v1/collection-management/runs/{id}` | 根任务与执行状态 |
| `POST /api/v1/collection-management/tasks/{uuid}/cancel` | 取消未运行任务 |
| `POST /api/v1/collection-management/tasks/{uuid}/retry` | 保留历史创建补采 |
| `GET /api/v1/collection-management/audit` | 分页操作记录 |
| `GET /api/v1/collections/monitor?window_minutes=60` | 有界监控快照 |

计划、历史和审计使用倒序 `before_id` 游标，单页最多 50 条。创建计划与立即执行需要客户端生成的 `request_id`；网络重试应复用同一请求 ID。

## 发布与验证

先运行增量迁移，再更新 API、Worker 和 Scheduler，避免新代码访问尚未创建的控制表：

```powershell
python -m scripts.migrate_collection_management --execute
python -m scripts.migrate_collection_monitor_indexes --execute
npm ci --ignore-scripts
npm run build:frontend
```

012 创建五张控制、计划、执行、关联和审计表；013 在线新增执行尝试时间索引。迁移可重复执行，不清空现有任务和业务数据。`deployment/release_read_models.py --phase upgrade` 已包含上述迁移，保留原有采集并发、请求间隔和三个分析进程。

聚焦测试位于 `tests/test_collection_management.py`、`tests/test_collection_monitor.py` 和前端 collection Node 测试。浏览器辅助服务 `tests/serve_collection_console.py` 只接受本机新建的 `tiktok_shop_monitor_test_*` 数据库，使用明确标记的合成数据，不启动来源请求或消息队列。

新版本部署后不会自动为现有店铺新增计划。现有采集任务继续运行；在控制台选择需要的资源范围并保存计划后，新增计划才进入调度。

2026-10-17 本地验证：完整 Python 回归 702 项通过、2 项 Redis 集成测试因未配置独立 Redis 而跳过；前端 Node 测试 15 项通过。使用真实本机 MySQL 和 FastAPI 完成浏览器操作验证：计划创建、编辑、启停、手动派发、取消、幂等补采、根任务历史保留、全局暂停/恢复和操作审计。监控的时间窗口、暂停刷新、失败任务详情及根路径、`/shopscope/` 前缀均已验证，390px 视口无页面横向溢出，浏览器无控制台错误。测试数据为合成数据，未发起来源采集请求。

生产版本 `c821118aed1fbde182952bb9a63629cafa38c6b8` 已部署，两个 Worker 和一个 Scheduler 的真实心跳、调度成功时间及迁移状态均通过只读检查；数据基础服务容器未重建。正式入口为 `https://shopscope-monitor.top/#collections` 和 `https://shopscope-monitor.top/#collection-monitor`。未在生产环境新增测试计划或修改人工暂停状态。

此次公网页面样本中，管理接口约 466ms，默认一小时监控约 697–784ms；24 小时窗口首次读取两万条历史样本约 8 秒，随后缓存读取约 359ms。这些是单次发布后的观测，不是延迟 SLA。大窗口仍需完整时间段聚合时，应进一步增加分钟汇总；当前界面明确显示采样上限，不能当作全量统计。发布记录见 [脱敏验证记录](evidence/collection-console-release-20261017.json)。

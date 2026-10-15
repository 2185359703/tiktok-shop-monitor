# 启动与验证运行层

采集默认关闭；本地 API 使用现有 MySQL，容器栈使用独立 MySQL 卷。不要把“API 可访问”视作商品采集完成。

## 本地启动

本次已经创建项目 `.venv`。运行依赖没有安装到其他项目；验证环境继承已有系统包，额外安装的消息与指标库位于本项目虚拟环境。

新机器先创建独立环境并安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

保留现有 `tiktok-shop-monitor.env`。也支持 `.env`；优先级为进程环境变量、`.env`、`tiktok-shop-monitor.env`。不要把真实配置复制到文档或提交到版本库。

```powershell
.\.venv\Scripts\python.exe -m scripts.run_worker --check
.\.venv\Scripts\python.exe -m scripts.run_scheduler --dry-run
.\.venv\Scripts\python.exe -m scripts.run_api
```

- `--check` 不连接数据库、队列或网站，显示已实现/已启用类型。
- `--dry-run` 只查询数据库，不恢复租约，不发布消息，不访问网站。
- API 默认监听 `127.0.0.1:8000`，不自动建表、不导入种子、不启动后台采集。
- 在新空库上应先运行 `python -m app.storage.schema`，已存在的本地业务库不需要重建。

连接池通过应用 lifespan 创建和释放，导入 `app.main` 不会访问数据库。[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)

## 配置运行组件

`configs/settings.toml` 保存非敏感配置，连接和密钥只放在忽略的本地 env 文件。

| 配置 | 默认 | 用途 |
| --- | --- | --- |
| `DATABASE_URL` | 现有本地配置 | MySQL 业务库 |
| `RABBITMQ_URL` | 未配置 | Scheduler/Worker 消息连接 |
| `REDIS_URL` | 未配置 | 共享限速、暂停和通知去重 |
| `API_TOKEN` | 未配置 | 写接口关闭；配置时至少 24 字符 |
| `ENABLED_TASK_TYPES` | 空 | 无自动执行；已实现店铺、商品、详情、快照、变化检测和报告 |
| `periodic_snapshots_enabled` | false | 显式启用后生成周期快照任务 |
| `monitored_product_ids` | 空 | 周期监测仅扫描这份平台商品 ID 名单 |
| `snapshot_interval_seconds` | 3600 | UTC 逻辑窗口间隔，60-86400 秒 |
| `SETTINGS_FILE` | configs/settings.toml | 非敏感配置路径 |
| `[report].report_directory` | data/exports/reports | HTML、Markdown 和 JSON 报告导出目录 |
| `API_HOST / API_PORT` | 127.0.0.1 / 8000 | 监听地址 |
| `lease_seconds / heartbeat_seconds` | 120 / 15 | 租约与心跳周期 |
| `minimum_interval_seconds` | 1.0 | 同一市场所有 Worker 的最小请求间隔 |
| `access_pause_seconds` | 900 | 遇到访问限制的市场暂停时间 |
| `dispatch_batch_size / dispatch_scan_limit` | 100 / 1000 | 每轮通知量与扫描上限 |

用户名/密码中的特殊字符需要 URL 编码；连接字符串不要输出到日志。Redis 闸门使用原子 `SET NX PX`，每个真实请求占用额度。[Redis SET](https://redis.io/docs/latest/commands/set/)

确认基础服务和采集器后，分别启动两个进程：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_scheduler
.\.venv\Scripts\python.exe -m scripts.run_worker
```

默认空开关下 Worker 保持空闲并提供指标。已实现类型还包含 product_snapshot 和 change_detection；周期调度额外需要显式名单和开关。按[商品发现](product-discovery.md)、[详情](product-detail.md)与[历史监测](snapshot-monitoring.md)说明选择有限范围，不重复导入或重置任务。

仅处理报告可设置 `ENABLED_TASK_TYPES=report_generation`。报告不会请求网站，也不会扫描未启用的商品采集任务。报告生成仍需要 MySQL、RabbitMQ、Redis；仅 CLI 导出及 GET 报告查询只需要 MySQL，详见[报告说明](monitoring-reports.md)。

## 控制面接口

交互文档在 <http://127.0.0.1:8000/docs>。

| 方法与路径 | 用途 | 限制 |
| --- | --- | --- |
| GET /health/live | 进程存活 | 不访问数据库 |
| GET /health/ready | MySQL 可达 | 不代表 MQ/Redis 可用或处理器完成 |
| GET /api/v1/capabilities | 已实现/启用采集器 | 不连接目标网站 |
| GET /api/v1/overview | 店铺、商品、任务统计 | 区分历史店铺与有入口店铺 |
| GET /api/v1/shops | 店铺分页 | 默认仅有入口店铺；with_url=false 包含历史身份 |
| GET /api/v1/products | 商品当前信息 | 支持 shop_id；未采集时空列表 |
| GET /api/v1/products/{product_id}/snapshots | 商品历史快照 | ID 为内部主键；商品不存在返回 404 |
| GET /api/v1/events | 变化事件分页 | 尚无采集数据时空列表 |
| GET /api/v1/tasks | 任务分页 | 可按 status / task_type 过滤 |
| GET /api/v1/tasks/{task_id}/attempts | 尝试历史 | task_id 是任务 UUID |
| POST /api/v1/tasks | 创建已实现的采集/比较任务 | Token + 对应开关；快照支持带时区的 scheduled_for |
| GET /api/v1/reports/current | 当前样本报告 | format=json/html/md；读取数据库，不创建任务 |
| POST /api/v1/reports/jobs | 创建报告任务 | Token + report_generation 开关；默认当前 UTC 小时去重 |
| GET /api/v1/reports/jobs/{task_id}/files/{format} | 下载已完成任务的报告 | 仅下载配置导出目录中的对应文件 |
| GET /metrics | Prometheus 文本 | 数据库任务计数和进程指标 |

分页 `limit` 为 1-100，`after_id` 为内部 ID 游标；返回 `next_after_id` 为空时结束。ID 与金额以字符串返回，避免前端精度损失。快照中的价格上下文单独返回，不混淆优惠价和公开价。

POST /api/v1/tasks 允许 `task_type`、`resource_id` 与可选的 `scheduled_for`，不接受任意 URL、Cookie 或任务 payload。报告使用专门的 /reports/jobs 入口。资源从数据库读取。无 Token 时返回 503；错误 Token 返回 401；处理器未实现返回 501；未启用返回 409。退休种子原有任务仍保持 cancelled，不会复活。

```json
{"task_type":"shop_discovery","resource_id":"1"}
```

上例使用种子内部 ID。`product_discovery` 则使用已核验店铺的内部 ID，不能填平台 ID；重复请求返回同一初始任务，不重开成功或取消任务。分页进度通过任务查询返回的 progress 查看。

## 独立容器部署

2026-10-14 用户启动了 Docker 引擎，本次使用独立临时 RabbitMQ 容器完成真实联调。完整 Compose 栈尚未启动；配置解析通过不代表镜像构建或整体部署成功。

1. 启动 Docker。
2. 将 `.env.docker.example` 复制为 `deployment.env`，替换全部示例密码；同步对应 URL 中的密码与编码。
3. 保持 `ENABLED_TASK_TYPES` 为空，先验证基础服务。

```powershell
if (Test-Path -LiteralPath deployment.env) { throw 'deployment.env 已存在，请直接编辑，避免覆盖。' }
Copy-Item -LiteralPath .env.docker.example -Destination deployment.env
docker compose --env-file deployment.env config --quiet
docker compose --env-file deployment.env up -d --build
docker compose --env-file deployment.env ps
```

复制配置只在新文件不存在时进行；已有 deployment.env 时直接编辑，避免覆盖。容器内包含 API、Scheduler、Worker、RabbitMQ、Redis、独立 MySQL、一次性 schema-init 和 Prometheus。schema-init 使用已有模型，不删除已有表；目录/种子导入不自动执行。

Worker 将报告写入共享 `report_exports` 卷，API 将同一卷只读挂载。该卷随容器重建保留；不要在需要保留数据时执行 `docker compose down -v`。容器内的报告文件路径与宿主机导出目录不同。

如果需要让容器连接现有本机库，必须明确修改连接、依赖和网络配置；不要把示例 DATABASE_URL 当成本地业务库连接。独立容器库中没有那 100 个店铺，不应在空库查询后误判本地数据丢失。

运维入口：

- API 文档：<http://127.0.0.1:8000/docs>
- RabbitMQ 管理页：<http://127.0.0.1:15672>
- Prometheus：<http://127.0.0.1:9090>

真实 Broker 的有限故障联调和字段验证已完成。扩大 Worker 数量前仍需测量实际积压和刷新成功率。Compose 可通过 `--scale worker=2` 增加实例；Prometheus Worker 目标使用 DNS 发现。所有实例仍共享同一个请求闸门，扩容不会增加网站请求配额。该部署尚未完成多实例实测。

## 验证与证据

运行离线检查：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m app.storage.schema --check-sql migrations/001_initial_schema.sql
docker compose --env-file .env.docker.example config --quiet
```

真实 MySQL 验证需要本机专用且尚不存在的 `tiktok_shop_monitor_test_*` 数据库，通过 `MYSQL_TEST_URL` 配置，测试结束会删除该次新库，不使用 `DATABASE_URL` 的业务库。Redis 验证使用独立 `127.0.0.1:36387` 实例和 `REDIS_TEST_URL`，每项测试只删除自己的唯一前缀，不执行 FLUSHDB。

已执行的隔离验证包括：

- 多 Worker 竞争仅一次成功领取、重复通知不增加尝试；
- 成功业务与任务状态原子提交，业务校验失败完整回滚；
- 三次总尝试耗尽、检查点保留、心跳续约和过期恢复；
- 旧 Worker 提交受租约栅栏拒绝；
- 真实 Redis 跨客户端请求间隔、暂停、短期去重与令牌释放；
- FastAPI 读取真实隔离 MySQL、写接口默认关闭、错误输入脱敏；
- 既有店铺服务接入 Worker 后不重复创建尝试记录；
- 原有模型、中文注释、SQL 和种子行为回归检查。

2026-10-14 已补充真实 RabbitMQ 联调，包含持久化报告任务、重复投递、断线重投、Broker 重启、租约故障恢复和死信，详见[联调证据](queue-integration.md)。仍未执行全容器启动、多节点压测、长期持续运行或性能指标验收。

### 本次验证结果

2026-10-13：107 项检查全部通过，使用独立 MySQL 与独立 Redis 实例，无跳过；测试库已由测试框架删除。环境中存在一项第三方 Starlette / AnyIO 的弃用警告，不影响当前检查结果。

本地 API 曾在临时回环端口 18000 启动，10 个只读 HTTP 入口均返回 200，100 个种子真实 ID 与数据库入口一致。检查前后各表计数及任务状态摘要一致，100 条商品任务没有开始执行。临时验证服务结束后停止，不作为常驻部署保留。

脱敏现场证据保存在 `data/samples/runtime/architecture-check.json`，属于忽略的本地证据，不含密码、Cookie、完整响应或连接串。API 已启动时可在项目根目录复核：

```powershell
.\.venv\Scripts\python.exe -m scripts.check_runtime --api-url http://127.0.0.1:8000
```

2026-10-14 新增商品发现验证：139 项检查通过；3 个店铺各两页，120 个不同商品与 120 条详情任务真实入库。商品分页进度、失败证据和实跑命令见[商品发现说明](product-discovery.md)。原有架构证据保留，新一次只读核查单独写入 `data/samples/product-discovery/read-only-check-20261014.json`。

2026-10-14 新增详情验证：161 项检查通过；3 个商品补全、268 个 SKU 实际入库，117 条详情任务保留。GET /api/v1/products/{id} 与 /variants 已可查看属性和规格，见[详情说明](product-detail.md)。新一次只读核查在 `data/samples/product-detail/read-only-check-20261014.json`。

2026-10-14 新增历史监测：183 项检查通过；6 条商品快照、536 条 SKU 历史记录，1 条真实 stockout 事件；基线和同一窗口重复执行均新增 0 条历史记录。默认自动周期监测关闭，详见[历史监测说明](snapshot-monitoring.md)。

2026-10-14 新增服装报告：通过真实 RabbitMQ 生成业务库报告，120 个商品、268 个最新 SKU、1 条真实事件；离线 HTML 支持搜索、店铺/证据筛选和分页。历史 536 条 SKU 记录没有重复计入库存分母。报告任务再次执行复用原产物，不增加尝试或创建采集任务。

本步骤最终回归为 196 项通过，无跳过；真实 Broker 六项检查通过，报告九项 HTTP 检查及既有十项只读接口核查通过。报告下载字节与任务产物一致，检查前后业务库状态不变，100 个种子链接保持一致。临时 API、联调 Broker 和隔离 MySQL/Redis 在收尾时停止或移除，报告、证据与用户 Docker 引擎保留。

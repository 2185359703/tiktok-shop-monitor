# 异步后端运行说明

API、全部 8 类任务、RabbitMQ、Redis、MySQL、全量采集控制和分析 Worker 已接入 asyncio。主 Worker 默认 8 个在途任务，评论专用入口默认 4 个，分析投影默认 4 个。报告单独限制在途数量，避免大报告占满采集槽位；统计与报告继续遵守共享租约。

本次修改已完成本地验证。服务器实际运行的提交、镜像和部署时间以 `deployment/active-release.json` 为准；更新依赖并重启应用进程后，新代码和 1 秒间隔才会生效。小型服务器沿用两台 320 MiB Worker，每台配置 4 个协程槽，合计 8 个。

## 启动与配置

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m scripts.run_worker --check
.\.venv\Scripts\python.exe -m scripts.run_scheduler --dry-run

# API、调度器和 Worker 各自一个进程；沿用原有环境文件与启用任务列表。
.\.venv\Scripts\python.exe -m scripts.run_api
.\.venv\Scripts\python.exe -m scripts.run_scheduler
.\.venv\Scripts\python.exe -m scripts.run_worker
```

专用评论命令 `scripts.run_review_worker`、全量控制命令 `scripts.run_collection`、分析命令 `scripts.run_analytics` 也使用异步数据库与协程循环。原有 `--once`、`--check`、`--status`、`--dry-run`、评论入队与单任务重放参数继续可用。`run_worker` 现在能消费 `product_reviews`，无需为评论再开一个常驻进程；需要单独分配评论资源时才使用专用入口。

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `worker_concurrency` | 8 | 全部任务合计在途上限 |
| `review_concurrency` | 4 | 评论专用入口并发 |
| `analytics_concurrency` | 4 | 分析投影协程和共享执行槽数量 |
| `DB_POOL_SIZE` | 10 | 未显式设置环境变量时的异步连接池大小 |
| `DB_MAX_OVERFLOW` | 10 | 临时额外连接上限 |
| `DB_POOL_TIMEOUT` | 15 秒 | 等待可用连接的超时 |

已有环境文件中的连接池值优先；例如原配置 `DB_POOL_SIZE=5` 会继续生效。现有 `mysql+pymysql://` 数据库 URL 无需改写，运行时自动采用 `mysql+aiomysql`。也接受显式 `mysql+aiomysql://`。离线建表、迁移、诊断和历史单次采集工具保留同步兼容入口。

`minimum_interval_seconds=1` 是同一市场所有采集进程共享的请求间隔。协程可重叠网络等待、SQL 和解析，但不会越过该间隔。因此下述数据库并发基准不等于 TikTok 采集速度提升倍数。

## 实现与事务边界

- `app/storage/async_database.py`：SQLAlchemy AsyncEngine + aiomysql；每个请求/事务独立 Session。已有 ORM 查询和校验函数通过 `AsyncSession.run_sync` 复用，驱动读写仍原生让出事件循环，不放进数据库线程池。
- `app/api/async_router.py`：路由注册为 coroutine，保留参数、返回字段和写接口鉴权。包含 Session 的请求在同一异步事务适配范围内执行。显式注入旧同步 engine 的测试/嵌入调用才使用兼容线程路径。
- `app/crawlers/async_http.py`、`async_public.py`：HTTPX AsyncClient 或 curl_cffi AsyncSession；流式响应有体积上限，逐跳校验公开 URL，保留商品和店铺身份校验。HTTP 200 业务失败仍作为失败处理。
- `app/workers/async_handlers.py`、`async_runtime.py`：店铺发现、店铺档案、商品发现、详情、快照、评论、变化检测、报告全部以 async prepare 执行。心跳与请求等待并行；业务结果和任务成功状态仍在同一 SQL 租约栅栏事务中提交。
- `app/queue/async_broker.py`、`async_redis_gate.py`：aio-pika 与 redis.asyncio；沿用 queue/exchange 代际、DLX、请求暂停和发布去重键。消息只有提交成功后 ACK；连接代际变化后不复用旧 delivery tag。
- MQ 使用 RabbitMQ 4.x 支持的每消费者预取，实际执行总数由 Worker Semaphore 控制；等待执行槽的通知尚未取得 SQL 租约。报告单独使用预取为 1 的通道，不占满公开采集通知的队列。
- `app/services/async_scheduler.py`：原有轮转预算、图片补采分组、水位背压和租约恢复均保留，等待数据库与 MQ 时不阻塞。
- `app/services/async_analytics.py`：分析任务并发领取独立执行槽，重统计继续使用共享 heavy lease；续租由协程完成。
- `app/reports/cache.py`：新增 AsyncReportCache，相同查询共用构建任务。取消一个 HTTP 请求不会取消其他请求正在等待的构建，避免同步 Future.result 阻塞整个事件循环。

计算、解析和 SQL 表达式构造没有机械改成空壳 async。较大的文本解析、报告序列化和文件写入通过 `asyncio.to_thread` 让出主循环；在线数据库、HTTP、MQ 和 Redis 使用原生异步驱动。旧同步模块用于离线工具和兼容测试，常驻入口已经切换。

停止时先取消队列订阅，在途任务继续续租、提交和 ACK；超过排空预算的任务取消，由原有租约恢复机制接管。失去租约时取消准备过程，旧执行禁止写入新结果。

## 实测

2026-10-15 北京时间 23:11，本地实际 MySQL 与现有商品数据：

| 检查 | 结果 |
| --- | --- |
| 8 个各等待 100ms 的只读 MySQL 查询，串行执行 | 877.6ms |
| 同样 8 个查询，并发执行 | 108.8ms，等待重叠约 8.06 倍 |
| 8 个实际 API 并发请求 | 全部 HTTP 200，整批 521.4ms |
| 实际商品公开页异步请求 | HTTP 200，解析 130 个 SKU，响应约 594KB |
| 并发领取同一 SQL 任务 | 只执行一次、只有一次尝试和提交 |
| 业务校验失败 | 业务写入回滚，失败尝试独立记录 |
| 查询取消 | 连接释放，后续查询成功 |
| 队列排空 | 停止领取后，当前任务完成并 ACK |

真实 API 覆盖健康检查、总览、商品、店铺、分析商品列表、工作台、当前报告和 v2 bootstrap。首次冷请求测试中最大事件循环延迟约 118ms，仍包含 Python 构造查询/响应的 CPU 开销；这不是生产延迟承诺。

可重跑只读基准：

```powershell
.\.venv\Scripts\python.exe -m scripts.benchmark_async_backend
```

结果保存在 `output/backend-async/benchmark.json`。脚本不创建采集任务、不修改业务数据，也不访问 TikTok。

发布前全量 MySQL 隔离回归为 598 通过、2 跳过，前端查询缓存检查 5 项通过。此前证据排除测试的标记与商家描述中的脚本标记重复，造成误报；已使用独立标记核查评论、AI 信息和仓库字段确实未写入证据。异步专项覆盖路由、驱动、取消、重复领取、回滚、调度背压、报告预取隔离和共享执行控制，并验证了异步详情处理器写入商品/SKU后继续生成分析投影的链路。

当前环境没有可用的本地 RabbitMQ，Docker daemon 未运行；MQ 路由、发布确认参数、报告预取隔离和提交后 ACK 已做自动化验证，尚未完成真实 Broker 联调。部署前应运行现有队列连通验证，并优雅停止旧 Worker 后切换新进程。

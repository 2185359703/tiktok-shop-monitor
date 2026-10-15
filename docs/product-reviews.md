# 商品评论协程 Worker

商品详情新增“评论”页签，展示已保存的评论正文、评分、购买规格、国家、图片、已购和激励标记。评论独立存储，不改写商品价格、评分总数或商家介绍。来源时间缺失保存为 NULL。

当前实测边界：2026-10-15 对本地商品 81（平台 ID `1729437431865054197`）完成真实异步请求、SQL 租约落库和 API 读取，保存 5 条有正文的首屏评论。来源显示 10,713 条全球评价；第二页接口返回业务错误，任务进入 `retry_waiting`。这不代表全量采集成功。浏览器之外的新匿名 HTTP 会话重复取得了同样的首屏结构。当前未部署到远程服务器，也未启动常驻 Worker。

## 运行

项目环境安装 `requirements.txt`，已有 MySQL、Redis 和 RabbitMQ 配置仍从本地环境文件读取。评论 Worker 新增 `aio-pika`；HTTP 使用已有 `curl_cffi.requests.AsyncSession`，也可配置为 `httpx.AsyncClient`。

```powershell
# 默认只打印 SQL；--check 只检查数据库；--execute 执行增量迁移。
.\.venv\Scripts\python.exe -m scripts.migrate_product_reviews
.\.venv\Scripts\python.exe -m scripts.migrate_product_reviews --execute
.\.venv\Scripts\python.exe -m scripts.run_review_worker --check

# 只影响当前终端。现有生产配置应在原列表中追加 product_reviews。
$env:ENABLED_TASK_TYPES = 'product_reviews'
.\.venv\Scripts\python.exe -m scripts.run_review_worker --enqueue-product-id 81 --max-pages 50

# 在配置了 RabbitMQ、Redis 的终端分别运行现有调度器与评论 Worker。
.\.venv\Scripts\python.exe -m scripts.run_scheduler
.\.venv\Scripts\python.exe -m scripts.run_review_worker
```

`--enqueue-product-id` 接受商品内部主键，可多次指定，创建任务后退出。同一商品、小时窗口、解析版本和页数预算重复请求返回已有任务，不重开成功或失败任务。未完成页重试保留原页码；队列通知由现有 Scheduler 发布。页面 GET 和页签切换只读数据库，不自动向 TikTok 发请求。

`--once` 最多处理一条 MQ 消息。没有本地 RabbitMQ 时，可用 `--task-id` 加已创建的任务 UUID 执行一次 SQL 通知重放，仍要求同一 Redis 请求闸门。该模式不自动续跑整条任务链、不代替常驻调度器。

本地已执行 007 增量迁移：创建 `product_reviews`，扩展任务类型 CHECK。迁移没有修改旧业务数据；重复执行为无操作。升级服务器前先执行相同迁移，并更新其 Worker/API 代码。

## 协程与恢复设计

1. Scheduler 将 `product_reviews` 任务 UUID 发布至独立队列；普通同步 Worker 跳过此类型。
2. `AsyncReviewWorker` 使用 Semaphore 限定在途任务，AMQP prefetch 使用相同上限。默认并发 4。
3. 每个任务通过已有 MySQL 行锁领取唯一租约；独立协程持续续租。HTTP、AMQP、Redis 等待不会阻塞事件循环。现有 PyMySQL 的短事务通过 `asyncio.to_thread` 执行，每次独立 Session。
4. 一页评论、下一页任务和当前任务成功状态在同一租约栅栏事务提交，提交后 ACK。失败通过原有重试策略回退，最多 3 次；耗尽进入 dead letter。
5. 不同商品可以并发；同一采集链上一页提交后才生成下一页。失去租约立即取消请求，禁止提交；退出先停止领取，等待在途任务提交。强制停止由现有 reaper 恢复过期租约。

请求间隔继续共享 `{queue_prefix}:{platform}:{region}:request_gate` 与暂停键，Broker 代际不会拆分限流。默认所有采集器合计至少间隔 1 秒，并发 4 不等于每秒请求 4 次。HTTP 会话按任务隔离；只使用本次匿名访问获得的服务端 Cookie，不复制浏览器登录状态。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `review_concurrency` | 4 | 在途任务上限，1–32 |
| `review_max_pages` | 50 | 单次商品链页数预算，1–500 |
| `review_http_backend` | `curl_cffi` | 原生异步 HTTP 后端，也支持 `httpx` |
| `detail_proxy_url` | 环境配置 | 沿用已配置的商品出站代理 |

## 数据与分页契约

首屏来自公开商品文档 `__MODERN_ROUTER_DATA__` 中 `product_info.component_data.review_info`。分页接口为 `POST https://shop.tiktok.com/api/shop/pdp_desktop/get_product_reviews`，请求使用 `product_id`、从 1 开始的 `page_start`、`page_size=5`、推荐排序 `sort_rule=1`、全部星级过滤 `filter_type=1/filter_value=6` 和 `component_name=pdp_left_reviews`。首屏与分页排序保持一致；不能从推荐首屏直接跳到“最新排序”的第二页。

读取文档时校验市场、商品 ID、店铺归属；每条评论再次核对商品 ID。页面结构或预览数量变化会失败，不猜测游标。`HTTP 200` 必须同时满足成功业务码与数据结构；业务错误、验证页、缺失列表都不能归为零评论。重复页摘要终止续页。达到页数预算标记 `page_budget`，`complete=false`。

`product_reviews` 按 `(product_id, platform_review_id)` 去重。后续相同内容只更新时间，内容变化增加独立版本；旧观察不能覆盖新评论。不删除本次没出现的历史评论。来源时间按 epoch 毫秒精确转 UTC，缺失为 NULL；全局评价数和具体评论国家分别保留。只保存公开展示昵称，不保存用户账号 ID、头像、主页或原始请求。

## API

`GET /api/v1/products/{product_id}/reviews?limit=20&after_id=0` 返回评论列表、`next_after_id`、`collected_count`、`source_total`、`collection_complete`、状态和最近任务。所有 ID 及大计数按字符串返回。`next_after_id=null` 仅表示数据库当前已保存的评论读完；全量完成必须单独看 `collection_complete`。未迁移返回 `pending_schema`，未采集返回 `not_captured`，有部分数据且等待重试返回 `partial_pending`，失败耗尽返回 `partial_failed`。

`POST /api/v1/products/{product_id}/reviews/collect` 接收 `{"max_pages":50}`，复用现有 Bearer 写权限，返回 202 和任务 UUID。需要启用 `product_reviews` 并执行迁移。通用 `/tasks` 接口引导使用这个专用入口。

## 验证与证据

范围为本工作区的评论采集实现、匿名公开页面与本地数据库。无账号登录、评论发布或远程部署。

| Evidence | 观察 | Finding 与实现路径 |
| --- | --- | --- |
| E1 | Camoufox 清洁文档返回 200，公开首屏包含 5 条评论及 `has_more=true`；Chrome 配置的调试端口不可用 | F1：首屏可解析。评论独立于原商品评分计数，见 `parse_review_document` |
| E2 | 页面静态调用代码指向 `get_product_reviews`，推荐排序为 1，页码字段为 `page_start`；未取得完整动态 initiator 栈 | F2：实现相同请求形状；不宣称签名恢复或匿名分页已打通 |
| E3 | 新异步 HTTP 客户端连续两次直接请求，均得到 5 条评论 | F1：首屏不依赖浏览器会话，见 `AsyncReviewClient` |
| E4 | 本地 SQL 租约任务首屏成功，第二页失败进入重试；API 返回 5 条和 `collection_complete=false` | F3：失败页不会冲掉已保存数据，见 `persist_review_page` 与 `AsyncReviewWorker` |

调用路径 P1：公开文档（E1/E3）→ 评论解析（F1）→ 租约事务落库（E4/F3）→ 下一页请求（E2/F2）→ API 部分采集状态（E4/F3）。当前残余问题是来源拒绝匿名分页；代码不会复制动态签名或把首屏误报为全量。

本次本地结果和可重跑脚本在忽略目录 `js_reverse_cache/tasks/product-reviews-20261015/`；`live-summary.json` 记录任务与覆盖情况，`reviews.json` 是实际评论数据。任务仍保留正常重试状态，没有修改重试次数或清除旧任务。隔离 MySQL 测试覆盖去重、时序、事务、分页、恢复和迁移；单元检查覆盖并发、取消、业务错误和客户端身份校验；前端验证覆盖转义、部分采集标记与继续加载。

验证结果：评论、租约运行时、历史迁移兼容性、前端静态路由和队列命名等 92 项相关回归通过；补充“空页或短页不能提前宣布采完”的校验后，30 项核心检查通过。真实浏览器已验证打开评论页签、显示 5 条、刷新后保留正确的等待重试状态，控制台无错误；1440×900 与 390×844 截图在 `output/playwright/product-reviews-desktop.png` 和 `product-reviews-mobile.png`。

最后一次全量回归为 561 通过、2 失败、2 跳过。剩余失败涉及本次开始前已有的详情证据保存行为（`test_evidence_does_not_save_reviews_aigc_shipping_or_warehouse`），以及工作期间并行加入的四张分析表尚未同步当前 schema 快照和表数断言（`test_current_schema_matches_registered_models`）。没有覆盖或回退这些其他工作。当前没有本地 RabbitMQ 配置，Docker daemon 未运行，因此 MQ 拓扑和 ACK 路径仅完成自动化检查；真实采集使用已有 Redis 和 SQL 租约入口验证。

协议验收清单单独保存为 `analysis/product_reviews_proof_manifest.json`，保留已有的全项目 proof manifest。验收范围是可重复的首屏采集；匿名分页仍被来源拒绝，完整评论采集的验收未通过。

验收清单 SHA-256：`398e0516eb39b0149d2c55c37d639dcb196efcb359283e392788d13a70700631`。临时浏览器和本地验证服务已关闭；中断检查留下的空测试库已清理，真实评论数据保留在项目数据库中。

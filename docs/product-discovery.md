# 商品发现与匿名分页

商品发现已接入运行层，并完成少量真实实跑。它保存商品基础信息并创建详情任务，不代表详情、SKU、历史快照或价格变化监测已经完成。

自动采集开关仍为空。本次没有启动 100 店批量抓取；数据库新增内容来自显式指定的三个已核验店铺。

## 本次真实结果

2026-10-14（北京时间；证据内部使用 UTC）：

| 店铺 | 成功页面 | 新增商品 | 详情任务 | 全店遍历 |
| --- | --- | --- | --- | --- |
| ANRABESS | 2 | 40 | 40 | 未完成，有下一页 |
| Dokotoo | 2 | 40 | 40 | 未完成，有下一页 |
| CHOUYATOU | 2 | 40 | 40 | 未完成，有下一页 |
| 合计 | 6 | 120 | 120 | 保留 3 条下一页任务 |

本次使用真实 MySQL、独立 Redis 和现有 Worker 生命周期；通过本地 SQL 通知执行，未使用 RabbitMQ Broker。每次分页使用新的普通匿名 HTTP 客户端，没有复制浏览器 Cookie、会话查询参数、指纹或签名。

证据位置：

- 首次来源核实：`data/samples/product-discovery/source-probe.json`；对应[来源分析](product-source-analysis.md)。
- 六页实跑：`data/samples/product-discovery/pilot-20261013.json`。文件名使用 UTC 日期，实际本地日期为 2026-10-14。
- 每页公开字段投影：`data/samples/runtime/<task_uuid>/<response_hash>.json`。
- 失败摘要：同一任务目录下的 `failure-*.json`，不保存完整 HTML、响应正文、Cookie 或连接串。

发现阶段产生 120 个商品、120 条详情任务、6 条成功商品页任务。后续详情小批量已完成 3 条，剩余 117 条，见[详情与 SKU 补全](product-detail.md)。商品发现待执行总数仍为 100，其中 97 条是未开始的初始任务，3 条是已采店铺的第三页；不能仅凭 pending 总数判断 100 个店铺都没有执行过。原始种子与退休历史仍保留。

## 小批量运行

项目根目录下，默认命令只读数据库、展示计划，不访问网站：

```powershell
.\.venv\Scripts\python.exe -m scripts.discover_products --max-shops 3 --pages-per-shop 2
```

显式实跑需要 `DATABASE_URL` 和 `REDIS_URL`，不要求 RabbitMQ。连接仍读取原有本地 env 文件，不在代码中写密码。选择当前种子中的平台店铺 ID：

```powershell
.\.venv\Scripts\python.exe -m scripts.discover_products --shop-id 7495434645551745353 --shop-id 7495467852373330933 --shop-id 7495552971774921097 --max-shops 3 --pages-per-shop 2 --execute
```

注意：这些店铺已经处理过两页，再执行将从待处理的第三页继续，而不是清空任务、重新抓首屏或覆盖历史。`--pages-per-shop` 控制这次本地执行量，后续分页任务仍会保留。`--output` 可指定本地报告路径，默认生成新的唯一文件名。

本次验证用的 Redis 实例属于临时验证环境，收尾后停止；常规使用需配置自己的项目 Redis 服务。普通 Worker / Scheduler 仍需要配置 RabbitMQ 与 Redis，并显式启用 `product_discovery`。

## 只取样、不写数据库

```powershell
.\.venv\Scripts\python.exe -m scripts.probe_product_source --shop-url https://shop.tiktok.com/us/store/anrabess/7495434645551745353
```

该命令最多读取首屏和一页续页，串行间隔至少 2 秒，响应不超过 8 MiB；使用与生产处理器相同的严格解析器。结果保存在新生成的本地证据目录，不写业务表。

## 写入规则

1. 首屏仅接受身份匹配的美国站 `shop_info`，及 `store_list_product` 来源的 `feed_list`。
2. 每条商品的 seller_id 必须等于目标店铺，canonical_url 内商品 ID 必须匹配；其他商家的推荐商品不会被误归属。
3. 价格使用 Decimal；From 起价的 price_max 保留 NULL，库存为 unknown。没有依据时不填 SKU、颜色、尺码、类目、库存或描述。
4. 商品按平台 + 市场 + 平台商品 ID Upsert；更新过时的来源不会回退新数据。列表只更新未补全详情的公开列表字段，不覆盖已有有效详情、内容哈希或库存。
5. 同一事务写入本页商品、每个商品的幂等详情任务、下一页任务及页进度。旧 Worker 租约失效时不能提交这些写入。
6. 后续页保留根 UUID、原始逻辑窗口、原生公开游标和累计已见商品 ID；不保存凭据，也不猜测游标算法。
7. 详情任务在同一根发现窗口中去重；已取消、运行或完成的详情任务不会被重置。

列表数据变化时未补全商品的 data_version 增加；MySQL Upsert 先比较旧字段再赋值，避免左到右赋值顺序使版本漏增。完整详情商品的字段、解析器和版本保持不变。

## 停止与完成语义

每页 `succeeded` 只表示本页事务成功，API 任务查询的 progress 区分以下情况：

| progress | 含义 | 下一步 |
| --- | --- | --- |
| has_more=true、complete=false、page_saved | 本页成功，来源仍有下一页 | 已创建后续任务 |
| has_more=false、complete=true、source_exhausted | 来源声明本次遍历结束 | 不再创建分页任务 |
| has_more=true、complete=false、page_budget | 达到本根任务页数预算 | 明确为部分结果，不宣称全量 |

默认根任务预算为 50 页，可通过 `product_discovery_max_pages` 调整。重试不改变预算和逻辑窗口。重复游标、无新商品的续页、has_more=true 的空页、外部重定向、非零业务码和字段漂移会失败；不会为了凑量循环抓取或把错误响应标记成功。

401/403/429 或 Security Check 会写失败摘要并暂停市场共享闸门；网络和可重试 5xx 使用任务层的有限退避。每次任务只做一次网络尝试，避免内外重试叠乘。

## 查询与下一步

启动 API 后可查看：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/products?limit=20'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/tasks?task_type=product_discovery&status=pending'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/tasks?task_type=product_detail&status=pending'
```

商品接口返回币种、价格上下文、起价、原价、折扣、评分、评价数及确证销量；ID 与金额按字符串保留。详情处理器已接入并补全三个商品的 268 个 SKU，快照与变化事件仍未启用。

验证结果：139 项检查通过，使用独立 MySQL 与 Redis，无跳过。覆盖来源结构、店铺归属、价格/币种、游标推进、重复页面、幂等任务、版本更新、旧来源保护、详情数据保留、事务回滚和脱敏失败摘要。存在一项环境中第三方 Starlette / AnyIO 的弃用警告，不影响检查结果。

已启动临时本地 API 验证 10 个只读入口；两次商品分页查询共返回 120 个不同 ID，价格与 ID 为字符串，列表指标可读，自动采集仍关闭。查询前后数据库状态一致，见 `data/samples/product-discovery/read-only-check-20261014.json`。临时 API、独立测试 MySQL 与 Redis 在收尾时停止，真实业务 MySQL 和采集结果保留。

Evidence → Finding → Path：三个真实店铺的首屏与匿名续页 + 120 个真实商品及 120 条幂等详情任务 + 回滚/归属/游标/缺失字段验证 → 商品发现链路可运行，但仍是部分店铺分页结果 → 下一步用少量已发现商品核实详情、颜色尺码、材质和库存，再接入详情与 SKU 补全。

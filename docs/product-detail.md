# 商品详情与 SKU 补全

详情处理器已接入公开 PDP、严格解析、SKU Upsert、租约事务和查询接口。后续[历史快照与变化检测](snapshot-monitoring.md)也已接入；自动采集和常驻周期监测仍默认关闭。

## 实跑结果

2026-10-14（北京时间，证据时间使用 UTC），仅执行三个已发现商品的详情任务：

| 店铺 | 平台商品 ID | 商品内部 ID | 已入库 SKU |
| --- | --- | --- | --- |
| ANRABESS | 1729415858004005193 | 41 | 90 |
| Dokotoo | 1729437431865054197 | 81 | 130 |
| CHOUYATOU | 1729443883935109513 | 1 | 48 |
| 合计 | 三个不同商品 | — | 268 |

详情阶段完成后有 120 个不同商品、3 个已补全详情和 117 条待处理任务；后续历史步骤已补建 6 条商品快照、536 条 SKU 观察和 1 条真实缺货事件，见[历史说明](snapshot-monitoring.md)。店铺与商品发现未再批量运行。

采用本地 SQL Worker 实跑、真实 MySQL、独立 Redis 共享请求闸门；未使用真实 RabbitMQ Broker。每次任务使用普通匿名 HTTP 客户端，不复制浏览器凭据，不读取购买者或评论人信息。

原始公开字段分析见[详情来源核实](product-detail-source-analysis.md)。本次成功报告是 `data/samples/product-detail/pilot-20261014.json`，每个任务的脱敏详情投影在 `data/samples/runtime/<task_uuid>/`。旧的三份 `*-source.json` 是前置结构样例；最终入库依据为 Worker 保存的完整公开字段投影。

## 哪些字段已补全

- 商品标题、商家文本描述、原始类目路径、颜色/尺码汇总。
- 每个 SKU 的平台 ID、完整已披露规格组合、单价、原价和公开可售状态。
- 商品展示价区间、原价下限/上限（不可见部分为 NULL）、展示折扣。
- 当前市场的 product_info.review 聚合评分/评价数，以及页面明确披露的销量。
- 商家规格和结构化描述中的材质、版型、季节、风格等；没有可靠唯一值时仍为 NULL。

SKU 身份来自真实 sku_id，不按标题拼凑颜色或尺码。颜色里的原始拼写（如 Apricort）保持不变，不擅自纠正。库存由可售数量与公开按钮标识相互印证；没有数量或标识不一致时为 unknown。公开 stock 可能有显示上限，不能视为商家仓库的真实库存总量。

本次有 5 处属性声明差异，保留在 attribute_conflicts 中；其中包含材质和长度的不同声明、缺省说明或不同字段层次，不代表已经证实商家错误。三份样例的材质标准字段采取保守 NULL，原始值保留，后续规范化可进一步区分占位声明和语义相容的材质类别/成分。不会把页面 aigc_info 的摘要当商家事实。

销量说明明确指向全球累计、排除退货的商品销量，而非美国站日销量。metric_scopes 保留这一口径；不能据此推算近期销量或美国站成交额。

## 小批量执行

默认命令只读数据库展示可执行计划，不访问网站、不写入商品：

```powershell
.\.venv\Scripts\python.exe -m scripts.complete_product_details --max-products 3
```

实际执行需要本地 DATABASE_URL、REDIS_URL，使用 `--execute` 明确选择范围：

```powershell
.\.venv\Scripts\python.exe -m scripts.complete_product_details --max-products 3 --execute
```

可通过重复的 `--product-id` 指定平台商品 ID；它不是内部主键。已成功、已取消、已耗尽或未到重试时间的任务不会再次领取。上述三个商品本次已处理，再指定它们不会重置任务，也不会自动重新创建任务。

共享限速使用现有 Redis 闸门，默认同一市场至少 2 秒间隔，401/403/429 或安全验证页面触发市场暂停。失败摘要只保存状态、类型、响应哈希和大小，不保存完整响应、Cookie、签名或连接串。

独立验证 Redis 已在收尾时停止，正常使用需配置自己的项目 Redis。若使用通用 Worker / Scheduler，还需要 RabbitMQ 并显式启用 `product_detail`；默认开关仍为空。

只取样、不写数据库的入口：

```powershell
.\.venv\Scripts\python.exe -m scripts.probe_product_detail --product-url https://shop.tiktok.com/us/pdp/open-front-cardigan-by-anrabess-lightweight-casual-sweater-coat-2025/1729415858004005193 --expected-shop-id 7495434645551745353
```

该命令使用同一个生产解析器，最多访问一个 PDP，保留新的投影文件；不会修改现有业务数据。

## 一致性与版本

详情任务提交前后核验租约；商品、SKU 和任务成功状态在同一事务中提交。商品、店铺和 URL 身份不一致时全部回滚，不能将 SKU 移到其他商品。

SKU 使用商品内部主键 + 稳定平台 SKU ID/哈希键去重，已有内部 ID 与身份键保留。旧观察不覆盖更新的详情；重复响应不会重复插入 SKU，也不会因采集时间不同增加内容版本。

product.content_hash / data_version 跟踪稳定详情内容，包括标题、描述、属性、颜色尺码和 SKU 规格。价格、库存、销量和评分等动态指标不用于制造 content_changed；SKU 状态另有 variant_payload_hash，后续历史快照/事件模块负责动态变化分析。

未出现在新响应里的旧 SKU 不删除，也不自动当作下架。raw_attributes.observed_sku_ids 记录本次看见的 SKU；查询接口的 seen_in_latest_detail 可区分当前观察与历史保留项。没有新证据的既有非冲突属性可以保留，并在 retained_attributes 中标识；冲突属性标准字段保留 NULL。

## 查询接口

```powershell
.\.venv\Scripts\python.exe -m scripts.run_api
```

然后查询本次样例：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/products/41'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/products/41/variants?limit=100'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/tasks?task_type=product_detail&status=pending'
```

接口中的 product_id 是内部主键，CLI 的 `--product-id` 是平台 ID，两者不可混用。SKU 列表通过 after_id / next_after_id 分页，ID 与价格返回字符串，不将大型平台 ID 转成浮点数字。

POST /api/v1/tasks 已支持详情初始补采，需 Token 和显式启用处理器；已有任意状态的同类任务返回现有任务，不重开历史任务。

## 验证边界与下一步

161 项检查全部通过，使用独立 MySQL 与 Redis，无跳过。覆盖归属与币种、规格引用、重复 SKU、未知库存、材料声明差异、商家文本与 AI/评论隔离、版本、旧观察保护、SKU 历史保留和整体回滚。环境中存在一项第三方 Starlette / AnyIO 弃用警告，不影响结果。

临时 API 已验证 10 个只读入口，以及三个商品详情和跨页共 268 个 SKU；自动采集开关为空，查询前后数据一致。证据见 `data/samples/product-detail/read-only-check-20261014.json`。临时 API、独立验证 MySQL / Redis 停止后，业务 MySQL 与 268 个 SKU 保留。

未验证全体商品、长期连续运行、真实 RabbitMQ 联调和多节点性能；不把三个样例外推为整个市场的覆盖或准确率。

Evidence → Finding → Path：三份真实 PDP、三个成功任务、268 个幂等 SKU 与事务/口径验证 → 详情和 SKU 链路可以运行，缺失与冲突仍可追踪 → 下一步建立商品/SKU 历史快照和价格、库存变化事件，继续保留观察上下文。

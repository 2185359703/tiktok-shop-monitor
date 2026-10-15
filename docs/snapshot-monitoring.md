# 历史快照与变化检测

商品与 SKU 历史快照、价格/库存变化检测和周期任务生成已接入。默认自动采集与周期监测开关仍关闭；本次只对已经补全的三个商品建立基线并重新观察一次。

## 真实运行结果

2026-10-14（北京时间）：

| 商品 | 商品快照 | SKU 历史记录 | 最新实际比较 |
| --- | --- | --- | --- |
| CHOUYATOU / 1729443883935109513 | 2 | 96 | 未检测到变化 |
| ANRABESS / 1729415858004005193 | 2 | 180 | 1 个 SKU 从有货变为缺货 |
| Dokotoo / 1729437431865054197 | 2 | 260 | 未检测到变化 |
| 合计 | 6 | 536 | 1 条 stockout 事件 |

已有 120 个商品和 268 个当前 SKU 保留。历史表保存两次不同时间的实际观察，不按内容相同就丢弃第二次快照。剩余 117 条详情任务与其他店铺任务没有在这一步批量执行。

三条基线来自上一步已成功详情任务保存的真实投影，使用原来的实际观察时间、任务窗口和响应摘要。随后重新访问三个公开 PDP，保存新的三条快照，执行六个变化检测任务。没有修改真实商品价格或库存来制造演示事件。

真实事件：ANRABESS 的 Gray / Large，平台 SKU `1729415858006954313`，旧公开可售数量为 5、状态 in_stock，新数量为 0、状态 out_of_stock；价格两次均为 81.89 USD。对应前快照 2、后快照 5、内部 SKU 93。这里反映公开可售状态，不宣称商家仓库库存被核验。

本次未检测到价格变化；降价、涨价、补货和折扣等分支通过隔离数据库中的明确测试样例验证，模拟样例不写入业务库。

## 证据位置

- `data/samples/snapshots/backfill-20261014.json`：3 条旧详情证据的真实基线补录。
- `data/samples/snapshots/pilot-20261014.json`：3 条新快照与 6 个变化检测的结果。
- `data/samples/snapshots/backfill-replay-20261014.json`：重复补录新增 0 条商品/SKU 快照。
- `data/samples/snapshots/replay-20261014.json`：重复执行同一窗口新增 0 条快照和事件，也没有再次请求网站。
- `data/samples/snapshots/read-only-check-20261014.json`：接口查询期间数据库状态一致，100 条种子入口仍匹配。
- `data/samples/runtime/<task_uuid>/`：每个任务的脱敏公开响应投影。

这些文件是忽略的本地证据，不包含 Cookie、签名、登录凭据或原始 HTML。快照只保存白名单内的商家商品事实，评论人、评论正文、AI 摘要和图片请求参数不进入比较链路。

## 单次快照采集

项目根目录执行下列命令只读展示计划，不访问网站、不创建任务：

```powershell
.\.venv\Scripts\python.exe -m scripts.capture_snapshots --max-products 3
```

显式执行需要配置现有 MySQL 与项目 Redis，默认取当前 UTC 小时窗口：

```powershell
.\.venv\Scripts\python.exe -m scripts.capture_snapshots --product-id 1729415858004005193 --product-id 1729437431865054197 --product-id 1729443883935109513 --max-products 3 --execute
```

`--product-id` 是平台商品 ID，可以重复指定。`--scheduled-for` 支持带时区的 ISO 逻辑窗口；手动实跑不接受未来窗口。重复执行同一商品、窗口和价格上下文保留第一条有效观察，不覆盖历史，也不重开已成功/已取消的任务。

同一次调用最多重采选中的商品，随后处理这些商品的有限数量待执行比较任务。比较读取数据库，不访问 TikTok；即使网站共享请求已暂停，比较仍可执行。大量历史比较积压应由普通变化检测 Worker 消化，不能把单次脚本当无限队列清理器。

本次单次实跑使用现有 SQL 租约 Worker 与独立 Redis，未使用 RabbitMQ Broker。临时验证 Redis 在收尾后停止；日常使用需要配置自己的项目 Redis 服务。

## 用已有真实证据建立基线

```powershell
.\.venv\Scripts\python.exe -m scripts.backfill_detail_snapshots --max-products 3
```

默认只展示候选，加 `--execute` 才补录。只能读取项目 runtime 目录内的成功详情证据；文件解析版本、商品身份、响应摘要和捕获时间必须与成功尝试相符。使用原时间，不把旧数据伪装成刚刚采集。

基线补录不重写当前商品与 SKU。已有更晚快照时拒绝倒序插入，避免旧观察改变已经生成事件的前后关系；因此首次部署先补基线再开始新采集。相同窗口的重复补录是幂等操作。

## 周期任务配置

周期功能需要同时满足三个条件：显式启用快照和变化检测处理器、打开周期开关、提供有限的商品名单。

在本地 env 文件中设置 `ENABLED_TASK_TYPES=product_snapshot,change_detection`，在 `configs/settings.toml` 的 runtime 段设置：

```toml
snapshot_interval_seconds = 3600
periodic_snapshots_enabled = true
monitored_product_ids = ["1729415858004005193", "1729437431865054197", "1729443883935109513"]
```

配置属于启用示例，当前项目实际仍为 false 和空名单。不要重复声明已有 TOML 字段，应编辑已有值。

Scheduler 只扫描显式名单中的已补全公开标价商品，按 UTC 窗口生成幂等快照任务，不追补停机期间每个错过的时间点。快照成功后保存 next_capture_at；超过剩余次数的失败窗口保留 dead，不清零伪装成功。不同窗口是不同观察，重试保持 scheduled_for 不变。

周期 Scheduler / Worker 仍需真实 RabbitMQ 与 Redis。连接服务并确认配置后，分别运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_scheduler
.\.venv\Scripts\python.exe -m scripts.run_worker
```

本次没有启动常驻周期进程、没有创建桌面自动化任务，也没有声称完成真实 Broker 联调或长期监测。

## 事务与历史规则

快照处理器重新使用已核实的 PDP / SKU 解析器，在事务外读取数据，再在租约栅栏下同时提交当前状态、商品快照、当次观察到的 SKU 快照及幂等变化检测任务。HTTP 失败、403、429 或安全验证页不会写“缺货/下架快照”。

- 商品快照唯一键为商品内部 ID + 逻辑窗口 + 价格上下文。
- SKU 快照只包含当次真实看见的 SKU，不把保留在当前表里的旧 SKU 拼进新观察。
- 第一次有效窗口写入后即不可覆盖；同一内容在新的窗口仍保留新的历史记录。
- 同一商品用行锁串行提交快照，新观察的 captured_at 必须晚于已存在的最新观察。
- 比较使用实际 captured_at 顺序，scheduled_for 是重试与幂等窗口，不冒充真实采集时间。
- 变化检测只比较相邻有效观察；不会改写已经存在的前后事件关系。
- 金额和大型平台 ID 在 JSON 与接口中均保留字符串。

## 比较规则

| 情形 | 行为 |
| --- | --- |
| 首条快照 | 只建立基线，不伪造“新上架”事件 |
| 同币种、上下文、解析版本的完整价格字段变化 | price_changed，商品级和同一 SKU 可分别出现 |
| NULL 价格变成已知价格 | 视为补全，不当涨跌价 |
| 明确有货/部分有货 → 明确全部缺货 | stockout |
| 明确缺货 → 明确有货/部分有货 | restocked |
| unknown 参与库存转换 | 不生成缺货/补货事件，不跨越未知中间观察比较 |
| 已知折扣 0 → 正数 / 正数 → 0 | discount_started / discount_ended；缺失折扣不比较 |
| 已观察规格集合变化 | variant_changed；不将未出现的 SKU 直接宣称下架 |
| 前后都明确的商家内容变化 | content_changed；字段变成未知或从未知补全不制造内容变化 |
| 币种、上下文或解析版本不一致 | 不比较，comparison_skipped=true，与“确认无变化”区分 |
| 坏格式快照或身份不一致 | 检测任务失败，不伪造有效结果 |

事件幂等键包含规则版本、商品/SKU、前后快照和事件类型。同一对观察重复检测不会再产生一份相同事件，旧事件保留原 detected_at。每条事件包含 previous_snapshot_id、snapshot_id、old_value、new_value，可以查回具体证据。

没有核实的页面失效或缺失响应不会生成 product_removed；销量、评价增长也不转换为未经验证的美国站近期成交量。

## 查询与指标

启动 API 后可查看本次 ANRABESS 历史与真实缺货事件：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/products/41/snapshots?limit=100'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/products/41/snapshots/5/variants?limit=100'
Invoke-RestMethod 'http://127.0.0.1:8000/api/v1/events?product_id=41&event_type=stockout'
```

路径使用内部 ID，不是平台 ID。历史 SKU 与事件接口使用 after_id / next_after_id 分页；事件可按商品和事件类型过滤。任务 progress 返回快照写入数、SKU 历史写入数、前后快照 ID、基线/跳过比较标识和事件计数，不暴露完整任务 payload。

POST /api/v1/tasks 支持 product_snapshot 和 change_detection。快照可提供带时区的 scheduled_for，变化检测选择该商品当前最新快照；写接口仍需要 Token 与对应处理器开关。

Worker 指标增加 snapshots_written_total、variant_snapshots_written_total、change_events_written_total，只统计该进程实际提交的新记录；数据库查询可核对持久化总数。指标从 Worker 重启后重新计数，不等同于全库累计值。

## 验证与下一步

183 项检查通过，使用隔离 MySQL 与 Redis，无跳过；覆盖窗口幂等、历史不覆盖、价格与未知值、缺补货、折扣、规格变化、版本漂移、周期白名单、暂停时比较和事务回滚。环境中有一项第三方 Starlette / AnyIO 弃用警告，不影响本次结果。

已完成真实基线补录、三个商品重新采集、同一窗口重放，以及只读 API 核查。历史 API 可返回 6 条商品快照及跨页 536 条 SKU 观察，真实 stockout 的公开数量从 5 到 0 与前后快照一致。查询期间数据库状态保持不变。

价格/补货/折扣/未知值/结构漂移等分支使用隔离测试库，真实业务库只保留实际观察到的 1 条缺货事件。测试库由框架删除；临时 API、隔离 MySQL / Redis 在收尾后停止，用户业务 MySQL 与历史数据保留。

上述快照阶段尚未验证 30 天持续运行、全部店铺覆盖、容器整体运行或多节点压测。后续报告阶段已完成真实 RabbitMQ 断线重投、Broker 重启、故障租约恢复与死信联调，见[队列联调](queue-integration.md)。

Evidence → Finding → Path：真实旧详情证据 + 三次新 PDP 观察 + 1 条可追溯缺货事件 + 重放新增 0 条 → 历史快照与变化检测链路可运行，结果保持来源语义 → 已在后续步骤完成[服装监测报告](monitoring-reports.md)和真实队列故障验证，下一步安排有限范围的持续运行与数据新鲜度验收。

# Google VPS 部署与全量采集

本文记录一次 Google VPS 部署与采集运行，实际服务器地址和本机连接配置不随公开仓库发布。部署脚本使用 SSH 别名 `google-vps`，项目目录为 `/opt/tiktok-shop-monitor`；复用脚本时，请在自己的 SSH 配置中将该别名指向已授权的服务器。

## 服务器与数据位置

2026-10-14，用户授权把启动磁盘从 10 GB 扩到 30 GB。使用 Edge 中有项目权限的 Google Cloud 账号修改启动盘，再在线扩展 `/dev/sda1` 的 ext4 文件系统。系统显示约 29 GiB 根文件系统，扩容后约 20 GiB 可用；原有 Sub2API、PostgreSQL、Redis 均保持健康。

| 内容 | 位置 |
| --- | --- |
| 应用与部署文件 | `/opt/tiktok-shop-monitor` |
| 私有连接与 Token | `/opt/tiktok-shop-monitor/deployment.env`，权限 600 |
| 部署配置 | `deployment/compose.server.yml` |
| 服务器采集配置 | `configs/server.toml` |
| 业务数据库 | 独立 MySQL 8.0.43 的 `tiktok_shop_monitor` |
| 业务、MQ、Redis、报告、任务进度、证据 | `tiktok-shop-monitor_*` 项目数据卷 |

服务器数据库为本次独立初始化，不覆盖本机业务 MySQL，不复制本机数据库密码。真实配置不进入源码归档、镜像或报告。

## 查看系统

Windows 已建立回环 SSH 转发，本次访问入口：

- 前端及 API：<http://127.0.0.1:18080/>。
- API 文档：<http://127.0.0.1:18080/docs>。
- 全量批次状态：<http://127.0.0.1:18080/api/v1/collections/current>。
- 当前样本报告：<http://127.0.0.1:18080/api/v1/reports/current?format=html>。
- Prometheus：<http://127.0.0.1:18090/>。
- RabbitMQ 管理页：<http://127.0.0.1:18072/>，凭据只在服务器私有配置中。

这些回环地址通过本机 SSH 连接访问服务器。转发断开后，在本项目根目录运行 `deployment/open_tunnel.ps1` 重新建立；已有转发端口占用时脚本会停止，避免覆盖其他进程。直连大文件传输缓慢时，本机已有回环代理 `127.0.0.1:7897` 可用 `deployment/open_tunnel.ps1 -ProxyPort 7897`。项目桥接仅向已确认 VPS 的 SSH 22 端口传递加密字节，认证与 known_hosts 校验仍由 OpenSSH 完成；不读取密钥或修改全局 SSH 配置。服务器 API 和管理入口均只绑定其回环地址；原有 80 端口的 Sub2API 服务保持原用途。

## 服务与恢复

服务器上执行：

```bash
cd /opt/tiktok-shop-monitor
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env ps
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env logs --tail 30 collection scheduler worker
```

服务包含 MySQL、RabbitMQ、Redis、一次性 schema-init、API、Scheduler、两个 Worker、全量批次控制进程、Prometheus 和详情出站转发。长期服务自动重启；批次控制进程完成并生成报告后正常退出。schema-init 的退出码 0 是建表完成，不是故障。

启动或恢复现有部署：

```bash
cd /opt/tiktok-shop-monitor
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env up -d --no-build --scale worker=2
```

更新已审核代码后构建并应用：

```bash
cd /opt/tiktok-shop-monitor
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env build api
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env up -d --no-build --scale worker=2
```

不要删除命名数据卷来“重新开始”。进程重启复用同一批次、种子哈希和 MySQL 任务；已有成功页、详情、快照与失败尝试均保留。

## 全量范围与完成标准

批次 `981b6945-eb51-480f-aad6-ad300efc10b5` 的固定范围为当前 100 条已核验美国站服装店铺入口。控制程序先导入这份名单，持续跟踪店铺身份、商品分页、详情、SKU、基线快照和本地比较，队列处理结束后自动创建报告任务。

全量完成必须同时满足：100 条种子有记录、所有目标店铺的来源分页明确结束、所有已发现商品详情完成、对应基线齐全、没有活动或失败任务。队列为空、每页成功或有少量商品，都不能单独证明全量。

状态字段：

| 字段 | 语义 |
| --- | --- |
| `target_shops` / `resolved_shops` | 目标数 / 已核实身份数 |
| `source_complete_shops` | 来源明确 has_more=false 的店铺数 |
| `products` / `details_completed` | 已发现商品 / 成功详情商品 |
| `variants` / `baseline_products` | 当前 SKU 数 / 有基线的商品数 |
| `active_tasks` / `failed_tasks` | 待执行、执行中或重试数 / 终止失败数 |
| `status=running` | 本批仍在执行 |
| `status=report_pending` | 采集任务已结束，报告等待处理 |
| `status=completed` | 完成标准满足且报告成功 |
| `status=partial` | 存在未完成来源或失败任务，保留已有数据和报告 |

每家店铺最多 500 个公开分页，达到预算仍有下一页会明确记为部分结果。颜色、尺码、公开单价、可售状态和商家属性来自详情；缺失字段保留 NULL/unknown。商品首次详情请求同事务保存基线，不为建立基线重复访问网站。周期快照仍关闭，本次运行的是有限名单的初始化全量批次。

## 请求兼容与限速

店铺身份和匿名商品分页继续使用 HTTPX。部分官方 PDP 链接只有 `/us/pdp/商品ID`，解析器现已支持该格式，同时保留商品 ID、店铺归属与市场校验。

PDP 使用固定版本 `curl_cffi==0.15.0` 的 Chrome 请求配置，保留该配置的默认请求头。详情出站路径为容器 → 本项目 `detail-egress` 转发 → 服务器既有 `127.0.0.1:17879` 出站通道。转发只监听 Docker 私有桥地址 `172.17.0.1:17880`，没有公网代理端口；未修改原出站通道配置。

采集不依赖浏览器自动化、登录账号、手工 Cookie 或验证码点击。该通道的可用性属于运行依赖；通道停止、出口信誉变化或站点规则变化时，任务会保留真实失败和重试状态。

所有真实请求，包括重定向，共享 Redis 的 2 秒最小间隔。单个 HTTP 200 验证页在服务器配置下只冷却当前资源；实际 401、403、429 继续暂停市场共享请求。默认总尝试次数为 3，不把受限响应写成空商品、缺货或下架。

已核实的兼容修复可以显式重试指定失败，保留旧尝试与逻辑窗口。追加预算只对已耗尽任务生效，最多追加 3 次：

```bash
cd /opt/tiktok-shop-monitor
sudo docker compose -f deployment/compose.server.yml --env-file deployment.env exec -T api python -m scripts.retry_compatibility_failures --error-code product_url_identity_mismatch
```

上例只展示任务。仅确认对应修复已验证后加 `--execute`；若旧预算确已耗尽，可显式加 `--extra-attempts 3`。这不是定时重开所有 dead 任务的机制。

## 验证证据

本地隔离 MySQL/Redis 与部署镜像检查均已执行。初次部署镜像 203 项通过；2026-10-15 的统计、规格、金额、报表、流式导出、调度和队列代际修复后，部署镜像 255 项通过，无跳过。镜像真实结果保存在服务器 `data/samples/deployment/image-tests.log`。测试创建独立的 `tiktok_shop_monitor_test_server_*` 库与临时 Redis，结束删除自己的测试库和临时容器，不使用业务表测试。

PDP 的原始 HTTPX 对照返回安全验证页；通过出站通道的三个独立匿名会话均返回匹配的商品、店铺、USD 币种及 36 个 SKU。最终 `ProductDetailClient` 也已单独重复验证，而不是只检查底层库的 HTTP 200。过程保留在项目 `js_reverse_cache/tasks/deployment-transport-20261014/`，不保存 Cookie、凭据或完整响应。

现场证据与截图保存在本地 `data/samples/deployment/`；其中 `disk-30gb.png` 为云端磁盘容量，`image-tests.log` 为镜像检查记录。采集阶段的最新数量以状态接口的 updated_at 为准，本说明不把早期进度数字作为最终结果。

`server-check.json` 已核验 10 表、193 个字段的中文注释、9 个只读 HTTP 入口以及四个 Prometheus 抓取目标。前端是“设计前端页面”任务完成的现有工作台，本次整体部署保留并同步了该实现。

实跑中有商家风格说明超过标准列长度。该说明完整保留在原始 specifications，标准字段留 NULL 并写入 normalization_issues；不会截短后冒充完整值，也不会因为这个可选字段丢弃价格和 SKU。来源身份、市场、金额及 SKU 归属的强校验继续执行。

## 2026-10-15 的运行修复

统计旧 SQL 的 OR + CAST 子查询在实际数据量下执行超过 47 分钟。改为互斥的任务类型范围查询和显式商品关联，不读取任务 JSON；取消的仅是已匹配本库、本用户特征的旧只读统计查询，没有删除任务。修复后的完整范围统计实测约 1.59 秒。

公开规格样本中，定义标签 `Size(General size)` 与 SKU 标签 `Size` 对应相同属性 ID、值 ID 和原始取值。仅增加这个已验证的标签等价关系，ID、取值、店铺和币种仍严格核验。金额仅允许格式正确的美国式千分位，例如 `9,880.00`；畸形分组、货币符号、负数和超过两位的小数继续拒绝。来源展示的高价不会被擅自改成低价。此前失败的规格与格式任务保留原尝试，重新排队使用剩余预算。

三个新的独立匿名详情会话均成功解析同一已失败商品，核对商品、店铺、USD 和 30 个 SKU；不复制 Cookie 或浏览器状态。两个分页取样中，一个原业务错误游标重新返回 20 个商品，因此恢复这类任务的剩余尝试；另一个返回空列表但没有明确 has_more，继续记为未确认结束，不猜测全量完成。

在 26,690 个商品、约 49 万个 SKU 的真实数据上，新报表构建实测约 53.4 秒、进程峰值 137 MiB。该数值只覆盖只读构建，不代表文件导出、HTTP 并发或 10 万商品的容量。报表现按 250 个商品分页读取并在 SQL 中汇总 SKU；事件只投影必要历史字段。实时报告按 App 隔离、同上下文单飞计算，成功编码后缓存 60 秒，保留真实生成时间；详见[报告口径与接口](monitoring-reports.md)。

根据用户授权，两个子 agent 分别审查队列与报表设计，主流程完成集成和实跑。发现详情队列有 545,241 条 ready 通知，而实际活动 SQL 任务约 11,430，说明通知显著膨胀；这不等于有同样数量的独立商品待采。`basic_get` 模式的消费者计数为 0 是正常现象。

调度改成每种任务独立游标、轮转预算和 passive ready 水位检查。公开采集类型水位 64，本地比较/报告水位 16；队列超过水位时不再新增该类型通知，但不限制消费、租约恢复和其他类型发布。只运行一个 scheduler。恢复时一次真实 tick 发布了 32 条通知，同时将详情类型标为背压；没有清空旧队列、增加 Worker 数或抬高 RabbitMQ 内存限制。

为了让新的 SQL 待办及时取得执行机会，RabbitMQ 使用 `broker_generation=g20261015` 的独立 queue、exchange、DLX、DLQ；Redis 的 `queue_prefix`、请求闸门、暂停和发布去重键保持原前缀 `tiktok_shop:tiktok_shop:US`。切换顺序为先停旧 scheduler、优雅停两个旧 Worker、确认退出后启动新代际。MySQL 的任务状态、尝试、游标和全量控制状态均继续使用原数据；未完成的旧租约由正常 reaper 恢复。

北京时间 15:02 与 15:05 的两次实际核查中，新详情 ready 分别为 61、59，变化检测为 6、7，旧详情队列始终为 541,025，未消费或清空。旧队列仅保留为停用通知存档，不计入当前待办，也不向其发布新消息。回滚时同样先停当前 scheduler/Worker，再移除或恢复代际配置；业务 SQL 无需回滚。当前代际只运行两个 Worker，原有 Sub2API 及数据库仍健康。

报表构建与导出已移到 Worker 的 prepare 阶段，显式使用只读会话，主线程持续处理 MQ 心跳和续租。apply 仅在租约栅栏下登记结果。隔离测试验证了报表准备时间超过初始租约长度仍能续租并成功提交；真实新任务 `7ef50b06-b849-442f-876f-ca81c0d144ad` 于北京时间 14:48:51 开始、14:51:34 完成，耗时约 163 秒，尝试 1 次成功，证明超过 120 秒的初始租约后仍能续期并登记产物。长时间留下的 Worker、调度器和出站转发是用户要求的持续采集服务，不是遗留诊断进程。

完整编码验证曾在 256 MiB API 容器触发内核 cgroup OOM，已保存真实限制，未把只读构建的 137 MiB 当作完整容量。修复为逐块编码、逐格式写文件和分块 HTTP 发送，保持原 JSON 字节摘要及 HTML 转义。随后并发 JSON/Markdown 请求返回相同生成时间，冷请求约 84.8 秒、热缓存约 1.23 秒，三种下载逐字节核对成功；当轮无 OOM 或重启。因文件页缓存及发送仍接近原限额，在主机约 1.9 GiB 可用内存的实测条件下把 API 配额调整为 384 MiB，没有修改云主机规格或费用。

阶段报告任务 `b841031e-5ce9-476c-9275-0bea25c8bd3a` 的 JSON、HTML、Markdown 已下载至本地 `data/exports/server/us-clothing-20261015-interim.*`；该报告观察截止为北京时间 13:47:39，包含 26,690 个发现商品、14,891 个历史详情商品、495,557 个观察 SKU。JSON SHA-256 为 `f2a037fd6b0e9eb0634ecae5feb432ff65077fba46a25b99935c26f41856a895`，三种本地文件均与登记产物摘要匹配。它是阶段报告，不是批次终态报告。

新的流式阶段报告 `7ef50b06-b849-442f-876f-ca81c0d144ad` 也已下载并核对三个格式的完整摘要，文件为 `data/exports/server/us-clothing-2026-10-15-5627a3a2d093ff5b.*`。观察截止为北京时间 14:48:51，包含 43,209 个发现商品、15,631 个历史详情商品、521,578 个观察 SKU。JSON SHA-256 为 `5627a3a2d093ff5b1647c31af176680bcbe5d670dcf878bcc0e23946b6df162f`。完整下载核验记录在 `data/samples/deployment/streamed-report-delivery-check.json`。

切换后对因旧运行故障耗尽的 5 个租约任务分别追加一次尝试，保留全部旧尝试和检查点；没有重开受限资源的全部耗尽任务。另一次有界分页取样在第 63 页重新返回 20 个合法新增商品，续传参数仍有变化，因此只恢复 `pagination_not_progressing` 来源任务的原有剩余预算，不宣称历史错误来自参数不变，也没有放宽结束判定。没有明确布尔结束标志的来源继续记为未完成。这些手动恢复使用服务器上的审核脚本经标准输入执行，不要求重建业务库。

部署镜像为 `sha256:2f092b86a0aed6ed2cd69b6eb81a70f8f6e2379f3ba384367c008ed012e6a073`。`deployment/Dockerfile.recovery` 用于在既有服务器镜像上应用已审核修复；常规全项目构建仍使用根目录 `Dockerfile`。证明与代码摘要见 `analysis/proof_manifest.json`；现场数量使用批次状态文件的更新时间，不能用上述阶段报告代替后续全量覆盖验收。

证明文件的写入后 SHA-256：`8a806dcba09d19645e293dc7545a7638639dc4c1d46c75a3118747ee575cf621`。它记录浏览器依赖边界、实际镜像与验证数量、匿名重放、阶段产物、队列代际和剩余限制，不包含原始凭据或页面响应。

Evidence → Finding → Path：云端 30 GB 与服务器文件系统实测 + 完整容器栈 + 隔离镜像回归 + 三次最终详情客户端验证 + 持久化采集进度 → 系统已部署且采集链路有真实数据产出 → 持续处理固定名单至终态，核对每店分页与失败原因，再导出最终报告；终态之前保持“采集中”的表述。

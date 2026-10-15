# 店铺档案与经营指标采集

2026-10-15：将店铺头像、名称、蓝 V、官方店铺标识、粉丝数、累计已售和视频数列为独立采集对象，保存当前值与每日历史观察。

状态：已实现独立解析、当前档案与历史快照、幂等任务、每日调度、查询、导出及店铺界面。本地业务库已完成增量迁移，CHOUYATOU 已通过真实采集、入库、API 与浏览器展示闭环。服务器部署和常驻周期 Worker 尚未启动；下文历史核查与本地实跑不代表服务器当前数据。

## 1. 实现前的缺项核查

| 截图信息 | 实现前状态 | 2026-10-15 实现前核查 |
| --- | --- | --- |
| 店名 CHOUYATOU、店铺 ID、链接 | 已解析并入库 | 存在 ID `7495552971774921097` 的店铺 |
| 店铺 Logo | 未持久化 | 公开页有 `shop_logo` |
| 蓝 V | 未持久化 | 公开页有 `store_brand_show.label_type_str = blue_v` |
| 官方店铺 | 未持久化 | 公开页有 `ShopIdentityLabelKey = official` 和 `OFFICIAL SHOP` |
| 粉丝数 34.8K+ | `shops.follower_count` 已预留，采集未接入 | 本地库 102 家店铺此字段全部为空 |
| 已售 498.2K | 没有店铺级字段 | 商品级 `sales_count` 不能代替店铺累计已售 |
| 视频数 1391 | 没有店铺级字段 | 与商品数、抓到的视频列表长度分开 |
| 平台在售商品数 | 已在商品发现首屏解析 | 本地库 3 家非空，CHOUYATOU 历史值为 206 |
| 店铺评分 | `shops.rating` 已预留，采集未接入 | 本地库 102 家店铺全部为空 |

2026-10-15 16:55（Asia/Shanghai）执行了只读 SQL 核查。CHOUYATOU 的 `last_seen_at` 为 `2026-10-13 16:00:35.507795 UTC`，不能把这一历史观察当作今天的完整店铺档案。

原链路缺项依据（下面所列文件现已按后文接入扩展）：

- [身份解析](../app/crawlers/public_shop.py)的 `ShopCandidate` 只保留身份、入口和证据路径；[身份写入](../app/services/shop_discovery.py)不写粉丝、销量或视频数。
- [商品发现解析](../app/crawlers/product_list.py)的 `parse_store_products` 已定位 `shopInfo`，目前只读取店铺身份及 `on_sell_product_count`；[商品发现写入](../app/services/product_discovery.py)更新 `product_count`。
- [店铺模型](../app/models/shop.py)没有 Logo、认证、销量和视频数字段；[店铺接口](../app/api/routes.py)也未返回这些指标。
- [任务枚举](../app/models/enums.py)和 [Worker](../app/workers/handlers.py)没有独立的店铺档案刷新任务。

## 2. 已验证的来源与字段映射

实际读取入口：[CHOUYATOU 美国站店铺](https://shop.tiktok.com/us/store/chouyatou/7495552971774921097)。2026-10-15 16:57:33（Asia/Shanghai）返回 HTTP 200，店铺 ID、名称和 US 市场一致。

页面 `__MODERN_ROUTER_DATA__` 中，选择唯一店铺路由的 `page_config.components_map`，按 `component_type = shop_info` 定位，再读取 `component_data.shopInfo`。本次组件索引为 `3`；实现不能硬编码这个索引。具体业务路径、响应摘要、白名单投影和数据库核查记录见[核验证据](evidence/shop-profile-20261015.json)。投影不包含完整 HTML 或会话信息。

以下是同一次公开请求的真实观察值，不是截图 OCR 的估计结果：

| 业务字段 | `shopInfo` 来源 | 观察值 | 计划存储 |
| --- | --- | --- | --- |
| 店铺名称 | `shop_name` | CHOUYATOU | `canonical_name` |
| 店铺 Logo | `shop_logo.url_list` | 两个图片地址，300 × 300 | `logo_url`，图片来源元数据 |
| 蓝 V | `store_brand_show.label_type_str` | `blue_v` | `verification_type` |
| 官方店铺标识 | `shop_identity_label.ShopIdentityLabelKey` / `identity_label_text` | `official` / `OFFICIAL SHOP` | `identity_label_key` / `identity_label_text` |
| 粉丝数 | `followers_count` / `format_followers_count` | `34872` / `34.8K+` | `follower_count` / `follower_count_text` |
| 店铺累计已售 | `sold_count` / `format_sold_count` | `498223` / `498.2K` | `sales_count` / `sales_count_text` |
| 来源标注的全球累计已售 | `global_sold_count` / `format_global_sold_count` | `498223` / `498.2K` | `global_sales_count` / `global_sales_count_text` |
| 视频数 | `video_count` / `format_video_count` | `1391` / `1391` | `video_count` / `video_count_text` |
| 平台在售商品数 | `on_sell_product_count` / `display_on_sell_product_count` | `205` / `205` | `product_count` / `product_count_text` |
| 店铺评分 | `shop_rating` | `4.5` | `rating` |
| 店铺评价数 | `review_count` | `36124` | `review_count` |

蓝 V 与官方店铺标识分别保存，不合并成一个“认证”布尔值；`official_creator_uid` 的存在不能证明官方店铺，`enable_follow`、`follow_status` 也不是粉丝数。

这里的 `sold_count` 与 `global_sold_count` 本次相同，但仍分列保存。`region = US` 说明店铺入口市场，尚不能单凭它断言 `sold_count` 仅统计美国订单。`video_count` 先定义为“店铺页展示的视频数量”，尚不宣称它等于该账号发布量或所有达人带货视频量。

本次浏览器已确认 CHOUYATOU 的 Logo 地址能加载。当前直接展示通过公共图片域名校验的来源地址，尚无本地图片缓存或长期可用性保证。设计阶段证据文件中的图片 URL 已移除查询参数，不可将其当作已验证可用的缓存地址。

## 3. 数据存储

### 3.1 `shops` 保存当前档案

保留既有 `(platform, region, platform_shop_id)` 唯一身份、内部 ID 和商品关联。既有 `follower_count`、`product_count`、`rating` 已接入真实来源，并增加下列可空字段：

| 字段 | 类型 | 写入约定 |
| --- | --- | --- |
| `logo_url` | TEXT | 经过公共图片来源校验的地址；资源缓存另行保存来源与抓取状态 |
| `verification_type` | VARCHAR(32) | 已观察的 `blue_v` 等标识，不从店名或图标颜色推断 |
| `identity_label_key` / `identity_label_text` | VARCHAR(64) / VARCHAR(255) | 标识代码与原始显示文案，例如 `official` / `OFFICIAL SHOP` |
| `follower_count_text` | VARCHAR(128) | 页面原始展示字符串 |
| `sales_count` / `global_sales_count` / `video_count` / `review_count` | BIGINT UNSIGNED | 仅接受来源给出的精确非负整数；未提供时 NULL |
| `sales_count_text` / `global_sales_count_text` / `video_count_text` / `product_count_text` | VARCHAR(128) | 保留缩写、加号及平台原始文案 |
| `profile_captured_at` | DATETIME(6) | 当前档案成功观察时间，UTC |
| `profile_source_url` | TEXT | 去跟踪参数后的公开店铺入口 |
| `profile_parser_version` | VARCHAR(64) | 字段映射版本，当前为 `shop_profile_ssr_1` |
| `profile_response_hash` | CHAR(64) | 原响应 SHA-256 |
| `profile_evidence_path` | TEXT | 白名单业务证据位置 |
| `profile_field_state` | JSON | 各字段状态、来源路径及指标口径 |
| `profile_observation_status` | VARCHAR(16) | `complete` / `partial`，不是 HTTP 请求状态 |

`profile_captured_at` 与 `last_seen_at` 分开。商品分页也会更新 `last_seen_at`，不能据此认定粉丝和销量已经刷新。产品发现链路仍可独立更新 `product_count`，新增 `product_count_captured_at` 记录它的实际观察时间；不因店铺其他字段刷新而改变这个时间。

### 3.2 `shop_profile_snapshots` 保存不可变观察

新表保存本次档案的全部身份文案、Logo、标识、计数及展示文本，同时包含：

- `id`、`shop_id`、`platform`、`region`，用复合外键校验店铺市场归属。
- `task_id`（内部任务主键）、`scheduled_for`（UTC 采集窗口）、`captured_at`（真实读取时间）。
- `source_url`、`response_hash`、`parser_version`、`evidence_path`、`field_state`。
- `observation_status`：`complete` 或 `partial`；HTTP 失败和身份不匹配仅记录任务尝试，不制造业务快照。

约束：`UNIQUE(task_id)`；普通周期任务同时以 `(platform, region, shop_id, scheduled_for)` 保证窗口幂等。增加 `(shop_id, captured_at, id)` 时间查询索引。人工补采使用新的明确窗口；对失败任务重试仍使用原窗口。

不同日期即使数值没有变化，也分别保存观察；同一任务重复消息不产生重复快照。快照至少支持 30 天趋势，默认不自动删除历史，保留策略应单独配置。

### 3.3 数字、缺失值和新旧观察规则

1. 数字字段允许 JSON 整数或全数字字符串，拒绝布尔值、浮点数、负数和超出 BIGINT UNSIGNED 的数值。精确 `0` 是有效数据。
2. `34.8K+` 不能直接写成精确 `34800`；只有来源同时提供 `followers_count = "34872"` 时才保存 34872。仅有缩写时数值为 NULL，文本原样保留。
3. `field_state` 区分 `exact`、`display_only`、`not_provided`、`invalid`；标识使用 `observed`、`not_displayed`、`invalid`。没有出现蓝 V 或官方标识不自动写成“未认证”。
4. 成功获取且确认身份的响应成为完整的一次观察。该响应缺少的指标在当前档案中写 NULL、在 `field_state` 标明缺失，旧值保留在历史快照，避免把旧值伪装成本次数据。请求失败则保留上一次成功档案，并通过任务状态和时间展示过期。
5. 用专属 `profile_captured_at` 判断档案新旧；迟到任务可以保存历史，但不能覆盖更新的当前档案。`product_count` 按其独立观察时间更新，避免旧档案覆盖较新的商品首屏结果。
6. 数值与展示文本、市场与指标来源都随同一次观察保存。店铺累计销量不从已采商品销量求和，不用相邻快照差值宣称成交订单数、近期净销量或 GMV。

## 4. 采集和调度

新增 `shop_profile` 任务，资源类型为 `shop`，`resource_id` 使用内部 `shops.id`；载荷包含平台店铺 ID、已验证店铺 URL、市场、采集窗口和载荷版本。

1. 店铺身份确认后，同时准备初始档案任务与商品发现任务。两条链路独立执行，商品分页失败不阻止读取店铺头部。
2. 从已核验店铺名单建立首次补采任务。采集频率默认每 24 小时一次，可按监测配置调整，使用 UTC 窗口加错峰时间；不自动扩大名单。
3. HTTP 请求复用现有客户端、共享限速和暂停机制。验证店铺路由、`seller_id`、市场及唯一 `shop_info` 组件，只采目标店铺的头部信息，不从推荐商品卖家信息拼出整店指标。
4. 在事务外请求、解析和保存白名单证据；提交时复用现有租约检查，在一个事务中写入档案快照、更新当前值、记录尝试及完成任务。提交后才确认消息。
5. 当前通过独立档案任务请求店铺页。商品首屏复用是后续优化，尚未接入；分页续页或身份投影不会被当成档案结果。
6. 请求失败、验证页、身份错配、字段类型异常分别记录原因。HTTP 200 也要检查业务结构；可重试错误按已有预算退避，结构变化进入人工诊断，绝不填零完成任务。

幂等键包含 `platform + region + internal_shop_id + shop_profile + scheduled_for`。Redis 用于共享限速与通知去重，持久化任务和 MySQL 唯一约束保证最终幂等。不会按整个 HTML 哈希去重掉后续日期的观察。

对字段缺失但身份有效的响应，可写 `partial` 快照并完成该次“观察任务”；批次仍须单独展示字段覆盖情况，不能把任务成功数当成全部字段采齐数。图像下载结果与结构化档案成功状态分开。

## 5. 查询、展示和完整性统计

已实现以下查询接口：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/shops` | 保留现有字段，增加 Logo、标识、三项主要指标及档案观察时间 |
| `GET /api/v1/shops/{shop_id}` | 查询完整店铺档案、原始展示文本、字段状态、来源及最近任务结果 |
| `GET /api/v1/shops/{shop_id}/snapshots` | 分页查询历史店铺指标，支持时间区间与数量上限 |
| `GET /api/v1/shop-profiles/coverage` | 当前市场有公开入口店铺的实时字段覆盖率及任务状态 |

历史查询参数为 `limit`（1–100）、`after_id`、`captured_from` 和 `captured_to`。时间参数必须带时区，服务端转换为 UTC，区间为左闭右开。大整数计数和身份 ID 通过 JSON 字符串返回，避免浏览器丢失精度。接口不会返回本地证据路径或原始错误文本。

继续使用现有任务创建入口，扩展其 `shop_profile` 类型校验；API 请求只创建任务，不同步访问网站。接口与商品查询一样按 `platform/region` 限定范围，内部 ID 和平台 ID 均返回字符串。

店铺详情头部呈现 Logo、店名、蓝 V、官方店铺标签，以及粉丝、累计已售、视频三项指标。优先用本次来源展示文本；只有精确数值时再格式化。详细信息展示精确值和 UTC 转换后的本地采集时间。缺失显示“未获取”，失败后保留旧值时显示“上次采集于……”。

列表增加粉丝、累计已售、视频、档案更新时间；导出同时包含精确数值、原始文本、字段状态、来源和时间。认证标签只展示本次明确观察的标识；不向用户制造已认证结论。

批次进度在 `shop_profiles` 中独立提供 `target_shops`、`profile_observed_shops`、`profile_complete_shops`、`profile_partial_shops`、`profile_failed_shops`、`profile_pending_shops`、`profile_stale_shops`，以及各字段非空/有效覆盖率。已保存的观察、最近任务状态和过期状态是不同维度，不相加；旧档案存在但最新任务失败时，同时计入 observed 和 failed。原有商品批次 `full_coverage` 保持商品口径，店铺档案完整度读取 `shop_profiles.full_coverage`。

截图核心字段的完整度包括名称、Logo 来源、粉丝、已售、视频以及已检查的两类标识状态；标识明确未展示可算“已检查”，但不能算“有认证”。数字只有显示文本时标明 `display_only`，并另报精确值覆盖率。商品采集完成与店铺档案完成分别报告。

趋势仅比较同店铺、同市场、同指标来源口径的有效精确值。缩写文字变化只展示原始观察；没有连续观察时显示缺口，不插值补造历史。累计已售下降可以记录为来源修正，不能自动解释为退货量。

## 6. 实施范围与验收

| 改动范围 | 交付要求 |
| --- | --- |
| `app/crawlers/shop_profile.py` | 独立档案解析、来源身份校验、字段状态和安全证据投影 |
| `app/models/shop.py`、新增快照模型 | 当前档案字段、不可变快照、约束、中文注释和索引 |
| 独立数据库迁移 | 扩展字段、建快照表、扩展任务类型约束；历史记录保持 NULL，不用截图填历史 |
| `app/services/shop_profile.py` | 原子写入、旧观察保护、首轮补采及周期窗口幂等 |
| 适配器、任务枚举、Worker 注册、调度器、配置 | 接入 `shop_profile`；先迁移，再上线兼容代码，最后启用任务与周期调度 |
| 店铺查询、前端、导出、批次统计 | 同时呈现数据、采集时间、缺失/过期状态及字段覆盖率 |

实施验收应覆盖：

- CHOUYATOU 真实公开页面读取、解析、入库、API 查询和页面展示闭环；数值以运行当时来源为准，不硬编码本次样本。
- 补充至少一个无官方标识店铺和一个缺失/缩写指标样本，检查 NULL、零和 `display_only` 的差异。
- 重复消息只保存一次快照；下一个时间窗口即使值不变仍新增快照；旧任务不覆盖新的当前值。
- 403、429、HTTP 200 验证页、店铺 ID/市场错配、组件缺失和坏字段不会生成“零粉丝、零销量”的假记录。
- 字段完整率、精确值覆盖率、请求成功率和图片下载成功率分别统计。
- 既有商品发现、详情、图片、快照和历史关系保持可用；所有已核验店铺的档案结果或明确失败原因可查询。

上述能力现已接入，验证边界与实跑记录见下节。Evidence → Finding → Path：原有库空值与真实 `shopInfo` 投影 → 明确缺项和可用字段 → 独立档案任务、当前值与历史快照、真实入库及浏览器展示完成闭环。

## 7. 运行方式

在项目根目录使用现有 Python 环境。连接从本地 env 读取；一次性采集使用 MySQL 租约 Worker 和 Redis 共享闸门，不需要 RabbitMQ，也不自动启动其他商品任务。

```powershell
# 先检查/迁移；可重复执行，已完成后 executed_statements 为 0。
.\.venv\Scripts\python.exe -X utf8 -m scripts.migrate_shop_profiles --check
.\.venv\Scripts\python.exe -X utf8 -m scripts.migrate_shop_profiles --execute

# 默认只输出计划；加 --execute 才请求并入库。
.\.venv\Scripts\python.exe -X utf8 -m scripts.collect_shop_profiles --shop-id 7495552971774921097
.\.venv\Scripts\python.exe -X utf8 -m scripts.collect_shop_profiles --shop-id 7495552971774921097 --execute

# 首次补采现有 100 家已核验店铺。
.\.venv\Scripts\python.exe -X utf8 -m scripts.collect_shop_profiles --seed-file data/seed/shops_us_clothing_verified.csv --max-shops 100 --execute
```

默认窗口为当前 UTC 日；同日再次执行复用已有任务，已成功任务不再请求或新增快照。手动新观察可使用 `--scheduled-for` 指定新的带时区时间。重试未到 `available_at` 或共享暂停期间的任务保持待处理，不重置已成功/失败耗尽任务。

已提供 [100 家店铺周期配置](../configs/shop-profiles.toml)，按每天一次、最多 300 秒错峰运行，独立于商品采集。常驻运行还需在当前环境配置 RabbitMQ。分别启动以下两个进程；本次交付未将它们注册为服务或部署到服务器：

```powershell
$env:SETTINGS_FILE = 'configs/shop-profiles.toml'
.\.venv\Scripts\python.exe -X utf8 -m scripts.run_scheduler
```

```powershell
$env:SETTINGS_FILE = 'configs/shop-profiles.toml'
.\.venv\Scripts\python.exe -X utf8 -m scripts.run_worker
```

默认 `configs/settings.toml` 仍不自动运行采集。服务器配置已注册 `shop_profile`，部署前须先执行迁移；周期刷新需使用明确名单配置。

## 8. 本地验证边界

2026-10-15 的本地业务库增量迁移执行了 4 条 DDL，迁移前后均为 102 家店铺、120 个商品、268 个 SKU、361 条原有任务，未删除或覆盖这些记录。随后 CHOUYATOU 于 17:31:47（Asia/Shanghai）完成真实采集，保存 34,872 粉丝、498,223 累计已售、1,391 视频、蓝 V、官方标识和首条完整快照。

真实 API 的店铺详情、历史查询均返回 200。浏览器已核对 1440px 桌面和 390px 手机布局、真实 CDN Logo、指标、标识和历史记录，无 JavaScript 错误或抽屉横向溢出。截图位于任务本地 `output/shop-profile-live-desktop.png`、`output/shop-profile-live-history.png`、`output/shop-profile-live-mobile.png`。

2026-10-15 17:33–17:42（Asia/Shanghai）补采已核验名单：100/100 成功且完整，失败、待处理均为 0；99 次新请求加上同窗口已完成的 CHOUYATOU 观察。100 家均取得 Logo 地址、精确粉丝数、精确累计已售、精确视频数、评分和评价数，其中 28 家明确展示蓝 V 和官方店铺标识。未展示标识的店铺保存 `not_displayed`，不推断未认证。

17:44 重跑同一名单和窗口，任务、快照和执行尝试均保持 100 条，新请求为 0。[实施核验证据](evidence/shop-profile-implementation-20261015.json)记录迁移、字段覆盖、重跑和浏览器结果；详细运行报告保留在任务本地 `data/samples/shop-profile/run-b6df2b02e0b6.json` 与 `run-c75ca30404d3.json`。

隔离 MySQL 定向回归 241 项通过；最后对历史补采任务状态排序的修正及存储/API/批次覆盖率补测 60 项通过。范围覆盖解析、迁移保留数据、重复执行、新旧观察、租约/重试、名单调度、查询隔离和既有店铺/商品发现。一次全仓回归为 484 通过、28 失败、2 跳过、1 个清理错误；失败涉及本次开始前已有未提交改动的分析投影、图片补采、商品内容证据，以及商品内容外键清理失败引发的后续任务测试串扰，不能宣称全仓测试通过。没有为通过这组回归覆盖其他正在修改的功能。

单次初始采集不证明 30 天连续稳定性，日采服务尚未常驻启动。默认配置不自动调度；100 家显式日采配置已经准备好。

# 数据字典与写入约定

数据库名默认为 tiktok_shop_monitor，以连接配置中的名称为准。当前市场是美国站 US，币种是 USD。表结构的定义源是 app/models；可执行建表 SQL 位于 migrations/001_initial_schema.sql。

## 通用字段与约束

| 字段或规则 | 类型 | 约定 |
|---|---|---|
| id | BIGINT UNSIGNED | 内部自增主键，仅用于本系统关联 |
| platform | VARCHAR(32) | 默认 tiktok_shop，大小写敏感 |
| region | VARCHAR(2) | 市场代码；调用方统一使用大写，如 US |
| platform_shop_id / platform_product_id / platform_variant_id | VARCHAR(128) | 平台原始 ID，按字符串保存，保留前导零和大小写 |
| SHA-256 字段 | CHAR(64) ASCII | 小写十六进制；由调用方生成 |
| created_at | DATETIME(6) | UTC 创建时间，数据库自动赋值 |
| updated_at | DATETIME(6) | 可更新实体的最后修改时间，数据库自动更新 |
| 金额 | DECIMAL(12,2) | Python 使用 Decimal；保留 0.00，缺失使用 NULL |
| discount_rate | DECIMAL(7,4) | 折扣比例，范围 0-1；0.2000 表示降价 20% |
| rating | DECIMAL(3,2) | 范围 0-5；不可见时 NULL |
| JSON 字段 | JSON | 原始属性、规格、业务载荷；缺失值写 SQL NULL，除非明确记录 JSON null |

除 crawl_tasks.payload 外，可空 JSON 字段均启用 none_as_null。payload 必须是 JSON 对象，ORM 默认空字典，直接 SQL 插入时需要显式提供。

未观察到的销量、库存数量、评价数和评分都保存 NULL，不能写 0。公开展示的“1K+ sold”等文字保存在 sales_count_text，未经确认的区间不填充 sales_count。

周期刷新、价格统计和事件比较按相同商品、币种和 pricing_context 进行。public_listed 表示公开标价，不代表优惠券、新客价格、运费、税费或到手价。后续如观察上下文不同，使用不同标识，禁止混用。

UTCDateTime 在读库时返回带 UTC 时区的 datetime，写入带时区 datetime 时先转为 UTC；传入无时区 datetime 时视为 UTC。MySQL 连接固定 time_zone 为 +00:00。

所有外键采用 RESTRICT。店铺或商品有历史子记录时禁止物理删除，失效通过状态标记。身份 ID 与解析记录一旦用于关联，应保持稳定。

## 1. seed_shop_observations

保存 CSV 原始观察，名称线索不会直接创建正式店铺。

| 字段 | 类型 | 用途 |
|---|---|---|
| category | VARCHAR(64) | 种子观察到的服装类目 |
| seed_type | VARCHAR(32) | shop_name、shop_url 或 shop_id；默认 shop_name |
| seed_value | VARCHAR(255) | 原始名称、URL 或平台 ID |
| shop_url | TEXT，可空 | 未解析时 NULL |
| source_url | TEXT | 类目页或观察入口 |
| verification_status | VARCHAR(64) | 保留 CSV 的观察依据；默认 observed_public_category_page |
| observed_at | DATE | 观察日期 |
| row_hash | CHAR(64) | 同一条原始观察的幂等键 |
| raw_record | JSON，可空 | 原始 CSV 行，默认延迟加载 |
| resolution_status | VARCHAR(32) | pending、resolved、ambiguous、not_found、failed |
| resolution_error | TEXT，可空 | 最近一次身份解析失败原因 |

其他字段为 id、platform、region、created_at、updated_at。

- 唯一键：row_hash。
- 辅助唯一键：(id, platform, region)，供映射表校验市场归属。
- 查询索引：(region, resolution_status, category)。
- 同名并不证明同一店铺。多个候选使用 ambiguous，不建立错误映射。
- 同一名称在不同日期或来源出现属于不同观察，可以映射到同一正式店铺。

导入时 row_hash 的输入固定为 platform 和 CSV 原始 8 个字段，按列名排序、紧凑分隔符、不转义非 ASCII 字符序列化为 UTF-8 JSON 后计算 SHA-256。文本去除首尾空白，region 大写，空 shop_url 统一为 null，日期为 YYYY-MM-DD。保持名称大小写，不用显示名称替代真实店铺身份。raw_record 保留未修剪的原始行。

## 2. shops

| 字段 | 类型 | 用途 |
|---|---|---|
| platform_shop_id | VARCHAR(128) | 已确认的稳定店铺 ID，必填 |
| canonical_name | VARCHAR(255) | 当前店铺名称，允许改名和同名 |
| canonical_url | TEXT，可空 | 稳定店铺入口 |
| discovery_category | VARCHAR(64)，可空 | 初次解析来源的类目，不代表全部主营类目 |
| currency | VARCHAR(3) | 默认 USD |
| rating | DECIMAL(3,2)，可空 | 店铺评分 |
| follower_count / product_count | BIGINT UNSIGNED，可空 | 平台可见指标 |
| status | VARCHAR(32) | unknown、active、inactive |
| first_seen_at / last_seen_at | DATETIME(6) | 首次和最近成功观察时间 |

其他字段为 id、platform、region、created_at、updated_at。

- 唯一键：(platform, region, platform_shop_id)。
- 辅助唯一键：(id, platform, region)。
- 查询索引：(region, status, discovery_category)。
- product_count 是平台可见数量，可空；本系统已监测商品数应从 products 查询。
- 解析后使用 Upsert 更新名称和 URL，不修改主键或平台 ID。

## 3. shop_seed_links

| 字段 | 类型 | 用途 |
|---|---|---|
| seed_id | BIGINT UNSIGNED | seed_shop_observations.id |
| shop_id | BIGINT UNSIGNED | shops.id |
| resolved_at | DATETIME(6) | 身份解析确认时间 |
| resolution_method | VARCHAR(64) | 如 source_id 或 manual |
| evidence_url | TEXT，可空 | 身份确认依据的公开页面 |

其他字段为 id、platform、region、created_at、updated_at。

- seed_id 唯一，每条观察只确认一个店铺；一个店铺可以对应多条观察。
- 复合外键保证种子、映射和店铺的 platform、region 一致。
- 索引：(shop_id, platform, region)。
- shops、shop_seed_links 和种子 resolution_status 应在同一事务中写入。

## 4. products

商品当前状态，用于查询和后续调度。

| 字段 | 类型 | 用途 |
|---|---|---|
| platform_product_id | VARCHAR(128) | 平台商品 ID |
| shop_id | BIGINT UNSIGNED | 内部店铺主键 |
| product_url | TEXT | 商品入口 |
| title | VARCHAR(512)，可空 | 发现后可暂缺；有效详情解析应验证标题存在 |
| description | TEXT，可空 | 原始描述，默认延迟加载 |
| original_category | TEXT，可空 | 平台原始类目路径 |
| normalized_category / category_rule_version | VARCHAR(64/32)，可空 | 标准类目与清洗规则版本 |
| brand | VARCHAR(255)，可空 | 品牌 |
| gender / season / style / fit | VARCHAR(32/64/128/64)，可空 | 性别、季节、风格、版型 |
| garment_length / material / pattern | VARCHAR(64) / TEXT / VARCHAR(128)，可空 | 长度、面料、图案 |
| color_summary / size_summary | JSON，可空 | 颜色、尺码标签数组；不强行换算跨店尺码 |
| raw_attributes | JSON，可空 | 原始属性，默认延迟加载 |
| currency / pricing_context / price_text | VARCHAR(3/64/255) | 币种、价格上下文、原始价格文案 |
| price_min / price_max | DECIMAL(12,2)，可空 | 当前 SKU 价格范围；单价商品最小值等于最大值 |
| original_price_min / original_price_max | DECIMAL(12,2)，可空 | 原价范围 |
| discount_rate | DECIMAL(7,4)，可空 | 原价与现价可以明确配对时才计算 |
| stock_status | VARCHAR(32) | unknown、in_stock、out_of_stock、partial |
| availability_status | VARCHAR(32) | unknown、available、unavailable、removed |
| sales_count / sales_count_text | BIGINT UNSIGNED / VARCHAR(128)，可空 | 精确销量和原始展示文案 |
| rating / review_count | DECIMAL(3,2) / BIGINT UNSIGNED，可空 | 评分和评价数量 |
| first_seen_at / last_seen_at / last_success_at | DATETIME(6) | 观察与有效详情完成时间，last_success_at 可空 |
| next_capture_at | DATETIME(6)，可空 | 下一次计划采集时间 |
| content_hash | CHAR(64)，可空 | 稳定业务内容摘要，排除采集时间和随机字段 |
| parser_version | VARCHAR(32)，可空 | 详情解析器版本 |
| data_version | INT | 默认 1；服务层在业务内容变更时显式增加 |

其他字段为 id、platform、region、created_at、updated_at。

- 唯一键：(platform, region, platform_product_id)。
- shop_id 的复合外键保证商品市场与店铺市场一致。
- 索引：(shop_id, availability_status)、(region, normalized_category, price_min)、(availability_status, next_capture_at)。
- CHECK 保证非负价格、价格区间顺序、有效评分和折扣比例。
- 请求失败或空正文不会证明商品下架；应记录 crawl_attempts，保留已有当前值。
- 使用过期采集结果更新当前表前，服务层应比较 last_success_at，避免旧任务覆盖新数据。

## 5. product_variants

| 字段 | 类型 | 用途 |
|---|---|---|
| product_id | BIGINT UNSIGNED | products.id |
| variant_key | CHAR(64) | 系统规格身份；必填 |
| platform_variant_id | VARCHAR(128)，可空 | 原始 SKU ID，可见时保存 |
| color / size / material | VARCHAR(128/64/255)，可空 | 原始或标准化标签 |
| options | JSON，可空 | 完整属性组合，支持颜色、尺码之外的规格 |
| currency | VARCHAR(3) | 默认 USD |
| price / original_price | DECIMAL(12,2)，可空 | 该规格的明确价格 |
| stock_status / available_quantity | VARCHAR(32) / BIGINT UNSIGNED，可空数量 | 库存状态和可见数量 |
| variant_payload_hash | CHAR(64)，可空 | 稳定规格内容摘要 |

其他字段为 id、created_at、updated_at。

- 唯一键：(product_id, variant_key)、(product_id, platform_variant_id)。
- 辅助唯一键：(id, product_id)，供历史记录校验所属商品。
- 有 SKU ID 时以该 ID 生成 variant_key；没有时根据排序后的规格键值生成 SHA-256，不使用列表位置。
- 缺失 SKU ID 可以保留多个记录；重复规格组合受 variant_key 约束。
- 已分配 variant_key 的记录后续补到平台 ID 时，在匹配确认后补充 platform_variant_id，保留原 variant_key，避免制造新的规格身份。

## 6. product_snapshots

每个逻辑采集窗口保存一次有效观察。相同价格在下一窗口仍然要保存，形成完整时间序列。

| 字段 | 类型 | 用途 |
|---|---|---|
| product_id | BIGINT UNSIGNED | products.id |
| task_id | BIGINT UNSIGNED，可空 | crawl_tasks.id；手动采集可空 |
| scheduled_for | DATETIME(6) | 调度窗口标记；同一任务重试时保持不变 |
| captured_at | DATETIME(6) | 实际观察时间 |
| currency / pricing_context / price_text | VARCHAR(3/64/255) | 观察价格上下文 |
| price_min / price_max / original_price_min / original_price_max | DECIMAL(12,2)，可空 | 本次观察的价格范围 |
| discount_rate | DECIMAL(7,4)，可空 | 已确认折扣比例 |
| stock_status / availability_status | VARCHAR(32) | 本次业务状态 |
| sales_count / sales_count_text | BIGINT UNSIGNED / VARCHAR(128)，可空 | 本次销量指标 |
| rating / review_count / variant_count | DECIMAL(3,2) / BIGINT UNSIGNED / INT，可空 | 本次统计 |
| content_hash | CHAR(64)，可空 | 本次稳定内容摘要 |
| parser_version | VARCHAR(32) | 必填，说明本次解析规则 |
| source_status / source_url | INT / TEXT，可空 | HTTP 状态及公开来源 |
| raw_payload | JSON，可空 | 脱敏后的复现材料，默认延迟加载 |

其他字段为 id、created_at。

- 唯一键：(product_id, scheduled_for, pricing_context)。
- 辅助唯一键：(id, product_id)。
- 历史查询索引：(product_id, captured_at)；归档索引：(captured_at)。
- 同一次成功事务里保存商品快照、SKU 快照和事件。重投时读取已有快照，避免重复创建。
- 快照不以 content_hash 去重；否则长期价格未变的商品会丢失观察历史。
- 快照采用追加写入；MySQL 账户权限及服务层应避免更新或删除历史记录。

## 7. variant_snapshots

| 字段 | 类型 | 用途 |
|---|---|---|
| product_snapshot_id | BIGINT UNSIGNED | 本次商品快照 |
| product_id / variant_id | BIGINT UNSIGNED | 内部商品和 SKU 主键 |
| captured_at | DATETIME(6) | 实际观察时间 |
| currency | VARCHAR(3) | 默认 USD |
| price / original_price | DECIMAL(12,2)，可空 | SKU 本次价格 |
| stock_status / available_quantity | VARCHAR(32) / BIGINT UNSIGNED，可空数量 | SKU 本次库存 |
| variant_payload_hash | CHAR(64)，可空 | SKU 本次内容摘要 |

其他字段为 id、created_at。

- 唯一键：(product_snapshot_id, variant_id)。
- 两个复合外键保证商品快照和 SKU 属于同一商品，禁止把其他商品的 M 码关联进来。
- 历史索引：(variant_id, captured_at)。
- 价格变化和缺货事件优先比较同一 SKU，商品最低价变化不等同于某一个 SKU 降价。

## 8. change_events

| 字段 | 类型 | 用途 |
|---|---|---|
| product_id / variant_id | BIGINT UNSIGNED，variant_id 可空 | 所属商品；有明确 SKU 时指定规格 |
| snapshot_id / previous_snapshot_id | BIGINT UNSIGNED，前一快照可空 | 当前和前一次观察证据 |
| event_type | VARCHAR(32) | 变化类型 |
| old_value / new_value | JSON，可空 | 前后值；金额以字符串写入 JSON |
| detected_at | DATETIME(6) | 发现时间 |
| event_hash | CHAR(64) | 事件幂等键 |

其他字段为 id、created_at。

event_type 允许 product_new、price_changed、discount_started、discount_ended、stockout、restocked、variant_changed、content_changed、product_removed。

- event_hash 唯一；建议输入商品主键、SKU 主键、事件类型、前后快照 ID 和标准化前后值，保证同一对快照重跑不重复，A→B→A→B 的后续变化仍能记录。
- 复合外键保证前后快照和 SKU 均属于事件商品。
- 查询索引：(product_id, detected_at)、(event_type, detected_at)。
- unknown 库存不能当成 out_of_stock；NULL 价格不能解释为免费；挑战页或超时不能触发 product_removed。

## 9. crawl_tasks

| 字段 | 类型 | 用途 |
|---|---|---|
| task_id | CHAR(36) ASCII | 业务 UUID；ORM 自动生成 |
| idempotency_key | CHAR(64)，可空 | 同一资源、任务类型、逻辑调度窗口和上下文的 SHA-256 |
| task_type / resource_type / resource_id | VARCHAR(32/32/128) | 任务类别、资源类型和资源标识 |
| payload | JSON | 业务参数，ORM 默认空对象 |
| checkpoint | JSON，可空 | 持久化分页游标或进度；Redis 只用作运行时缓存 |
| priority | INT | 0-9，默认 0；调度器约定值越大越优先 |
| status | VARCHAR(32) | 默认 pending |
| attempt_count / max_attempts | INT | 已执行次数和最多尝试次数；默认 0/3 |
| scheduled_for / available_at | DATETIME(6) | 原始调度窗口和允许再次执行时间 |
| started_at / finished_at | DATETIME(6)，可空 | 本次执行时间 |
| lease_expires_at / lease_token | DATETIME(6) / CHAR(36)，可空 | Worker 租约和本次执行令牌 |
| worker_id / last_error | VARCHAR(128) / TEXT，可空 | 最近执行者和错误 |

其他字段为 id、platform、region、created_at、updated_at。

- task_id、非空 idempotency_key 分别唯一。
- 类型允许 shop_discovery、product_discovery、product_detail、product_snapshot、change_detection、report_generation。
- 资源类型允许 seed、shop、product、variant、report。
- seed 任务 resource_id 使用种子内部 id 的字符串；其他类型的资源标识由服务层保持一致，不混用内部主键和平台 ID。
- 状态允许 pending、running、retry_waiting、succeeded、abandoned、dead、cancelled。
- 索引：(status, available_at, priority)、(platform, region, resource_type, resource_id)、(status, lease_expires_at)。

执行约定：

1. 通过条件更新抢占可执行任务，更新 lease_token 和 lease_expires_at。
2. 相同事务里增加 attempt_count，并创建新的 crawl_attempts。
3. 提交成功数据和任务终态后再 ACK 消息。
4. 瞬时失败更新 retry_waiting 和 available_at；耗尽次数进入 dead。
5. Worker 退出后发现租约到期，将 abandoned 任务转为 retry_waiting。
6. 晚到 Worker 只能使用仍匹配的 lease_token 更新状态，防止覆盖新的执行。

这些字段支撑后续 Worker 实现，模型不会自动执行调度、ACK 或重试。消息发布失败时需要调度器重扫数据库中未发布/未开始的 pending 任务，不能依赖数据库提交和 RabbitMQ 发布天然原子。

## 10. crawl_attempts

| 字段 | 类型 | 用途 |
|---|---|---|
| task_id | BIGINT UNSIGNED | crawl_tasks.id，内部主键，不是业务 UUID |
| attempt_no | INT | 从 1 起计数 |
| worker_id | VARCHAR(128)，可空 | 执行实例 |
| request_url / request_method | TEXT / VARCHAR(16) | 脱敏 URL；方法默认 GET |
| response_status / response_bytes / latency_ms | INT / BIGINT UNSIGNED / INT，可空 | HTTP 状态、响应字节数和毫秒耗时 |
| succeeded | BOOLEAN，可空 | 尚未完成时 NULL |
| error_type / error_message | VARCHAR(64) / TEXT，可空 | 可分类错误 |
| evidence_path / response_hash | TEXT / CHAR(64)，可空 | 脱敏证据文件与 SHA-256 |
| started_at / finished_at | DATETIME(6)，结束时间可空 | 执行起止时间 |

其他字段为 id、created_at。

- 唯一键：(task_id, attempt_no)。
- 索引：(finished_at, error_type)。
- 每条记录对应一次 Worker 执行尝试。若一次任务访问多个分页接口，请将各页拆成子任务，或后续扩展请求日志表；不要让一个响应状态冒充全部分页的结果。
- 不在 request_url、payload、checkpoint、错误信息或证据文件里保存 Cookie、Authorization、密码或完整签名。

## 字段与原总规划的调整

当前实现针对已存在的种子和服装数据特点做了以下补充：

- 增加种子观察及身份映射，保留原始名称线索。
- 平台 ID 和数据库主键明确分开，支持按市场去重。
- 商品单一 price 改为 price_min/price_max，SKU 单价保存于规格表。
- 增加 SKU 快照，使同一颜色、尺码的价格与库存可追溯。
- 未采集到的统计保持 NULL；未知业务状态不触发误报。
- 检查点和租约持久化到任务表，为恢复执行提供记录。

种子 CSV 幂等导入与公开来源解析现已实现，命令和限制见[种子导入与店铺解析](seed-discovery.md)。初始身份任务的幂等键使用任务类型、种子 row_hash 和固定的 initial_identity_resolution 上下文，不使用每次导入的当前时间。商品发现初始任务使用平台、市场、真实店铺 ID 和 initial_product_discovery 上下文；后续周期任务另按调度窗口生成幂等键。

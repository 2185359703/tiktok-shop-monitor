# 种子导入与公开店铺身份解析

本流程把名称线索转为带公开来源证据的真实店铺身份，并准备后续商品发现任务。

前置条件：Python 3.11+、已创建的 MySQL 表以及项目根目录的数据库配置。真实连接文件不应提交到版本库。所有命令均从项目根目录执行。

来源类目页和推荐页不是完整店铺目录。没有匹配到名称，不能据此认定店铺关闭或不存在。当前只解析美国站公开购物页面，不登录、不下单，也不复现带签名的接口。

2026-10-13 补链后，用户允许替换未核实的 22 个原店名，当前 100 条记录均有已核验店铺入口。原始 100 条观察保留在数据库和归档 CSV 中，新增 22 条替换观察。当前主 CSV 是新的观察；重新导入时会新增部分对应观察，不会覆盖原始记录。核验链接与当前任务已同步，无需再次导入才能使用。

## 1. 校验并导入种子

```powershell
python -m scripts.seed_shops --dry-run
python -m scripts.seed_shops
```

自定义文件：

```powershell
python -m scripts.seed_shops --file data/seed/shops_us_clothing.csv
```

CSV 必须包含且仅包含以下 8 列，顺序可以不同：

```text
region,category,seed_type,seed_value,shop_url,source_url,verification_status,observed_at
```

- UTF-8 和 UTF-8 BOM 均可读取。日期必须是 YYYY-MM-DD。
- region 标准化为大写，所有文本去首尾空白，空 shop_url 转为 NULL，店名保留大小写。
- region、类目、种子类型和值、来源 URL、观察依据和日期必填。字段数量、长度或枚举不合法时，整份文件校验失败，数据库不写入。
- 原始行保存在 raw_record；标准化后的 8 个字段加 platform，排序为紧凑 UTF-8 JSON 后计算 row_hash。
- 同一标准化观察在文件中或数据库中重复，不产生重复行。同名不同日期、类目或来源是不同观察，并不直接创建不同正式店铺。
- 每个待解析观察创建一个初始 shop_discovery 任务，resource_id 和 payload.seed_id 均使用内部种子主键的字符串。它不是平台店铺 ID。
- 重复导入保留既有任务 UUID、状态、重试次数、调度时间、检查点和错误；不会重新打开已完成、失败或取消任务。缺少初始任务的 pending 种子可以补建任务。

输出统计的 unique_rows 是文件内去重后的观察数，不是确认店铺数。

## 2. 访问真实公开来源

```powershell
python -m scripts.discover_shops --max-sources 6 --interval 2
```

命令读取数据库中美国站 pending 种子，按 source_url 合并请求。HTTP 下载发生在数据库事务外，不在等待网站时持有行锁。

| 参数 | 默认值 | 含义 |
|---|---|---|
| --max-sources | 6 | 本次最多读取的不同公开来源页数量 |
| --interval | 2 | 请求之间的间隔秒数，命令不接受小于 2 的值 |
| --evidence-dir | data/samples/shop-discovery | 脱敏证据目录 |

只允许美国站 `shop.tiktok.com/us/k/`、`/c/`、`/store/`、`/pdp/` 和公开 `www.tiktok.com/shop/store/` 入口。URL 跟踪参数不保存也不请求，跨域或非购物路由的重定向拒绝跟随。

HTTP 超时、连接失败和 5xx 最多尝试 3 次，使用有限退避。401、403、429 不自动重试，并停止后续来源访问。响应最多读取 8 MiB；200 但没有预期 SSR 业务组件时记录结构错误，不把它当作空店铺目录。

解析入口是 HTML 的 `script#__MODERN_ROUTER_DATA__`，只读取 `loaderData` 中页面的 `page_config.components_map` 业务组件。真实页面中组件可以是数组；兼容同字段下的对象表示，不依赖固定组件位置。

店铺身份来自同一业务对象的 `seller_id` 和 `shop_name`，以及存在时的 `shop_link`。商品列表中的 seller_info 也能提供这些字段。店铺 ID 按字符串保存，大整数无损转换，拒绝浮点 ID。相同 ID 在商品和店铺模块中重复出现时合并。

## 3. 确认身份和衔接任务

名字匹配只对 Unicode 兼容字符、连续空白和大小写做标准化，不使用相似度、模糊匹配或自行拼接店铺 URL。shop_id 类型按 ID 匹配；shop_url 类型按经过校验的店铺 URL 中的 ID 匹配。

| 结果 | 种子状态 | 初始任务状态 | 写入行为 |
|---|---|---|---|
| 唯一真实 ID | resolved | succeeded | 正式店铺、来源映射、成功尝试记录和初始商品发现任务一起提交 |
| 同名多个 ID 或映射冲突 | ambiguous | dead | 不猜测身份，保存人工确认提示和失败尝试记录 |
| 本次来源未覆盖 | pending | pending | 不建立店铺；保存来源检查点，等待其他入口 |
| 来源访问失败 | 保持原状态 | 保持原状态 | 控制台汇总错误；不伪造已执行的逐种子尝试 |
| 任务已运行或已处理 | 保持原状态 | 保持原状态 | 不覆盖 Worker 的执行或租约状态 |

shops 使用 `(platform, region, platform_shop_id)` 去重。同一店铺的不同观察建立多个 shop_seed_links，但只创建一个初始 product_discovery 任务。没有直接观察到店铺 URL 时保留 NULL，不从店名构造猜测链接；下一步应通过商品或店铺入口补充它。

本命令是来源页批量身份确认，不是通用分布式 Worker。一页 HTTP 响应可以验证多条种子，成功任务的 crawl_attempts 引用同一页证据；尝试记录条数不能当作网络请求次数。未覆盖的种子不计为一次已执行任务，不能靠高频重复抓推荐页来补齐。

## 4. 检查落库结果

在连接到项目库的数据库工具执行：

```sql
SELECT resolution_status, COUNT(*) AS observation_count
FROM seed_shop_observations
GROUP BY resolution_status;

SELECT task_type, status, COUNT(*) AS task_count
FROM crawl_tasks
GROUP BY task_type, status;

SELECT id, platform_shop_id, canonical_name, canonical_url
FROM shops
ORDER BY id;
```

再次运行种子导入，应显示 new_observations 和 new_discovery_tasks 都为 0，已有解析状态不会被重置。

证据 JSON 是白名单投影，包含来源 URL、HTTP 状态、时间、响应字节数、响应 SHA-256 和店铺候选。它不包含完整 HTML、Cookie、会话凭据、接口签名、埋点或完整 router layout。response_hash 是原 HTML 响应的摘要，不是投影文件本身的摘要；不能用投影文件重新计算原 HTML 哈希。

## 5. 后续采集任务

product_discovery 已持久化，但尚没有执行商品分页的 Worker。下一步应从确认店铺中选择少量样本，验证公开商品列表、分页条件和详情字段，再接入商品幂等落库、SKU、价格快照和变化事件。

需要人工补入口的种子应提供来自公开页面的真实店铺链接或 ID，并保留新来源证据；不要直接手工把 pending 改成 resolved。

已有具体店铺候选链接时，可运行 `python -m scripts.verify_seed_links --sync-database`。该命令按已有 ID 或明确名称核对公开店铺页，允许使用经核验的店铺入口补充原种子身份，同时保留原始观察、row_hash 和未运行任务的其他状态。不修改运行中 Worker 的载荷或租约。

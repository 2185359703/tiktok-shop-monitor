# 商品列表公开来源核实

2026-10-13，使用已核验的 ANRABESS 美国站店铺，核实公开首屏、浏览器“查看更多”及普通 HTTP 匿名分页。没有复制浏览器 Cookie、会话标识或签名参数，没有访问登录接口。

## 已观察的请求链

1. GET 店铺公开入口：`https://shop.tiktok.com/us/store/anrabess/7495434645551745353`。
2. 从 `__MODERN_ROUTER_DATA__.loaderData.* /page.page_config.components_map` 的 `shop_info` 取得店铺身份，从 `feed_list` 取得首屏商品。
3. 首屏有 20 条商品，`has_more=true`；商品组件的数据来源类型为 `store_list_product`。
4. 浏览器点击“查看更多”发起 POST `/api/shop/brandy_desktop/store/product_list`，另取 20 条。再次点击后页面共 60 条，分页游标继续变化。
5. 普通 HTTP 客户端向同一固定分页地址发送公开组件信息及原生分页参数，不带浏览器请求中的会话查询参数，实测 HTTP 200、业务 code=0、20 条商品。

匿名取样证据：`data/samples/product-discovery/source-probe.json`。文件只保存公开商品样例、字段名、状态和摘要，不保存完整 HTML、Cookie 或页面安全配置。

## 分页协议

请求 JSON 含两个序列化字符串：`component_info`、`load_more_params`。

- 组件类型 `feed_list`，名称 `feed_list_store_list_product`；数据来源 `store_list_product`，count=20，clamp=false。
- 下一页使用响应提供的 `search_params`，这是不解析、不猜测的公开分页游标。
- 浏览器实测 offset 始终为 0，seller_id 为目标店铺 ID，search_word 为店铺名称，exclude_product_ids 为已展示商品 ID 的累计集合。
- 响应必须满足整数 code=0、data.products 为数组、has_more 为布尔值。
- has_more=true 时必须有有效且推进的下一页游标；重复游标、无新商品或空页不能被标记为全店采集完成。

后续实现采用每页一个持久化任务：商品写入与下一页任务创建在同一事务提交。页面处理成功不等于整个店铺遍历结束；只有来源返回 has_more=false 才记录 complete=true。

## 字段映射与校验

| 真实字段 | 用途 | 规则 |
| --- | --- | --- |
| product_id | 平台商品 ID | 字符串保留，不转浮点数 |
| seller_info.seller_id | 商品归属 | 必须等于任务目标店铺，不混入其他商家的推荐商品 |
| seo_url.canonical_url | 详情入口 | 美国站 PDP，URL 内商品 ID 必须与 product_id 一致 |
| title | 商品标题 | 必填，保留原文，不能靠标题猜 SKU 规格 |
| product_price_info.currency_name | 币种 | 本项目固定 USD，不混入其他币种 |
| sale_price_decimal / origin_price_decimal | 公开展示价格 | 使用 Decimal；空原价保留 NULL |
| price_prefix | 起价语义 | From 表示起价，不代表所有 SKU 同价，不能直接填相同最大价 |
| discount_decimal | 展示折扣 | 仅保存 0-1 范围内的确证值，不自行估算 |
| rate_info.score / review_count | 评分和评价数 | 范围与整数校验；不可见保留 NULL |
| sold_info.sold_count | 公开销量 | 仅保存精确整数；区间/缩写保留原始文本，不估算 |
| sku_info | 列表中的 SKU 价格片段 | 不含足够颜色/尺码规格，不能当作完整 SKU 详情 |

ANRABESS 样例首个商品展示 From 27.99 USD；列表内 SKU 价格样例不一致。商品发现阶段只确证起价，最大价保留 NULL，SKU 规格与完整价格范围留给详情处理器。

## 边界与工程接入

遇到 401/403/429、安全验证页面、非成功业务码或结构漂移即记录失败，不把它当成零商品、下架或遍历完成。所有实际 GET/POST，包括重定向，都需经过请求间隔与运行层共享闸门。

Evidence → Finding → Path：真实店铺首屏、浏览器两次原生分页、匿名 HTTP 分页成功 → 可以实现公开商品发现，不需要浏览器凭据 → 编写严格列表解析器、每页幂等事务、后续详情任务和有界分页，再在少量店铺上实跑核验。

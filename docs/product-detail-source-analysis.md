# 商品详情与 SKU 公开来源

2026-10-14，已在 ANRABESS、Dokotoo、CHOUYATOU 各核实一个已发现商品，普通匿名 HTTP 均返回可解析的公开详情。没有复制浏览器 Cookie、指纹、签名或登录状态，没有采集评论正文和购买者信息。

## 数据入口

GET 已发现的美国站 PDP，读取 `__MODERN_ROUTER_DATA__` 中 `product_info` 组件的 `component_data.product_info`。商品 ID、商品内 seller_id、组件 shop_info.seller_id 与任务/URL 三方一致才接受结果；shop_info.region 必须为 US。

三个前置投影位于 `data/samples/product-detail/*-source.json`。已观察的公开 SKU 数为 ANRABESS 90、Dokotoo 130、CHOUYATOU 48；这只是三个商品的样例，不是 120 个商品都已补全。

## 核实字段与语义

| 来源 | 用途 | 约束 |
| --- | --- | --- |
| product_info.product_id / seller_id | 商品与店铺身份 | 字符串保留，禁止浮点 ID |
| shop_info.region | 市场 | US，不推断买家的具体配送地区 |
| sale_props / sku_sale_props | 颜色、尺码与其他规格 | 属性/属性值 ID 和标签交叉校验，不从标题猜规格 |
| skus[].sku_id | 平台 SKU ID | 每个商品下稳定唯一，不按颜色名拼出虚构 SKU |
| skus[].price | 单个 SKU 的公开价与原价 | Decimal / USD；缺失原价保留 NULL |
| price.min_sku_price / max_sku_price | 商品公开价格区间 | 使用确证字段；无最大价的起价不冒充单价 |
| stock + pdp_button_area_id | 当前公开可售状态 | 正数 + default 为有货，0 + sold_out 为缺货；冲突或缺失为 unknown |
| specifications | 商家规格与服装属性 | 保留重复声明，冲突不任意选值 |
| desc_blocks | 商家商品描述 | 仅文本，不执行 HTML，不抓取图片/评论 |
| product_info.review | 当前市场的聚合评分/评价 | 不混用 global review_info 的数量，不保存评论人信息 |

公开 stock 是页面披露的可售数量，可能有显示上限，不是经核验的商家仓库库存。数字 status 的完整枚举未被核实，因此不能单独据此猜库存。

ANRABESS 同一商品的两个 Material 声明分别指向 PBT 和 Polyester。两份原文会保留在 raw_attributes，material 标准字段保留 NULL 并记录冲突；不使用 aigc_info 摘要替代商家事实。

## 接入规则

每个详情任务在事务外读取与解析，在租约栅栏下将商品详情、SKU Upsert、内容哈希和任务结果一起提交。重复处理不增加重复 SKU，旧观察不回退新价格与规格。没有出现在本次响应的旧 SKU 不直接删除或判定下架，保留历史并由后续快照/状态策略处理。

当前步骤只补全商品与 SKU；周期快照和变化事件处理器不在本步骤中启用。遇到访问限制、身份冲突、缺失关键 SKU 字段或结构变化时保留失败摘要，不把它当作商品下架。

Evidence → Finding → Path：三个真实 PDP 的公开投影与颜色/尺码、价格、库存字段 → 可以实现不需浏览器凭据的详情与 SKU 补全，但需保留缺失和冲突 → 严格解析、幂等事务与小批量实跑，再接入历史快照和变化检测。

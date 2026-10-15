# TikTok Shop 美国站公开来源验证

验证日期：2026-10-13。范围：公开美国站服装来源页和一个实际店铺页面，没有登录、下单、账号操作或签名接口复现。

下文记录最初来源采集批次。后续补链和用户批准替换后，当前种子为 100 个已核验入口，状态与证据见[种子链接核验](seed-link-verification.md)，不要把下文的 21 个初始店铺数当作最新数量。

## 浏览器与 HTTP 验证

浏览器访问[女装来源页](https://shop.tiktok.com/us/k/clothing-for-women)，页面能够展示店铺和商品。一次实际观察中，CHOUYATOU 的链接包含字符串 ID `7495552971774921097`。

继续访问公开[CHOUYATOU 店铺入口](https://www.tiktok.com/shop/store/chouyatou/7495552971774921097)，跳转到[美国站店铺页](https://shop.tiktok.com/us/store/chouyatou/7495552971774921097)。页面内嵌数据中的 seller_id、shop_name、shop_link 和 region 与入口一致。

店铺页面还提供 products 列表、product_id、title、product_price_info、seller_info、seo_url、sku_info 和 has_more。has_more 为 true，说明首屏不是完整商品目录；后续采集需要独立验证分页，不能把首屏数量当作店铺商品总数。

Python 普通 HTTP GET 同样能读取类目页 HTML，无需先复现浏览器中的带签名 XHR 请求。采集器使用有标识的 User-Agent、串行访问、至少两秒间隔和有限重试，没有使用登录凭据或指纹绕过。

## 本次实际来源采集

运行命令：

```powershell
python -m scripts.seed_shops
python -m scripts.discover_shops --max-sources 6
```

| 来源类目 | HTTP 状态 | 去重店铺候选数 |
|---|---|---|
| activewear | 200 | 22 |
| clothing-for-men | 200 | 21 |
| clothing-for-women | 200 | 23 |
| lingerie | 200 | 13 |
| sportswear-shop | 200 | 26 |
| women-dresses | 200 | 23 |

这里的候选数只代表一次公开页面快照，不是类目总店铺数。不同来源中的同一店铺可能重复，不能把候选数相加作为去重后的业务规模。

数据库核对结果：

- 100 条原始种子观察。
- 21 条观察解析成功，形成 21 个真实店铺和 21 条来源映射。
- 79 条观察本次未在原来源页匹配到，仍为 pending，而非 not_found。
- 21 个 shop_discovery 任务成功，79 个仍待解析。
- 自动创建 21 个 pending 的 product_discovery 任务；尚未执行商品采集。
- 21 条成功尝试记录引用六份来源证据，不能视为 21 次网络请求。
- 其中 4 个已确认店铺具有直接观察到的公开入口；其余店铺的 canonical_url 保持 NULL，不拼接猜测地址。

重复运行种子导入，新增观察和新增店铺解析任务均为 0，解析状态保持不变。

## 证据、结论与后续路径

证据投影保存在 `data/samples/shop-discovery/`，crawl_attempts.evidence_path 与任务检查点保存对应路径。文件包含公开店铺 ID、名称、链接、业务数据路径和响应摘要，不保存完整 router layout、Cookie 或接口签名。

由本次证据可得：公开 SSR 页面能够完成一部分真实店铺身份确认；种子名称本身不足以作为唯一身份；推荐内容会变化，来源页不能保证覆盖全部种子。无需先建设高并发或分布式队列来证明这一数据链路。

后续路径：优先选取已有真实入口的店铺，获取少量公开商品列表与详情样本，确认价格是普通标价还是特定优惠上下文，再验证商品 ID、SKU 规格和分页条件。商品正式采集结果、历史快照和变化事件的数量必须以实际落库为准。

本文件记录单次验证结果，不保证未来页面结构或可访问性不变。

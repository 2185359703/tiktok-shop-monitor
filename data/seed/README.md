# 美国站服装种子店铺

文件：`shops_us_clothing.csv`

截至 2026-10-13，主名单包含 100 个已核验的美国站服装店铺，100 条记录都有店铺链接，对应 100 个不同的真实店铺 ID。保留原名单中已核验的 78 个店铺，按用户允许为另外 22 条补选了同类店铺。

| 文件 | 条数 | 使用方式 |
|---|---|---|
| [shops_us_clothing.csv](shops_us_clothing.csv) | 100 | 完整当前名单，每条都有已核验链接 |
| [shops_us_clothing_verified.csv](shops_us_clothing_verified.csv) | 100 | 可直接使用的采集入口，与完整当前名单一致 |
| [shops_us_clothing_pending.csv](shops_us_clothing_pending.csv) | 0 | 当前无待确认项，只保留表头，不用于导入 |
| [原始名称观察](archive/shops_us_clothing_names_20260925.csv) | 100 | 2026-09-25 的原始名单，全部 shop_url 为空，保留追溯 |
| [被替换的原店名](archive/shops_us_clothing_replaced_names_20261013.csv) | 22 | 原有未核实链接的名称，不再作为当前采集目标 |

逐条替换关系见 [shop_replacements_20261013.json](shop_replacements_20261013.json)。按原有分组配齐数量：女装 25、连衣裙 21、男装 21、运动服 17、运动类服装 12、内衣 4。分组是本项目的监测分类，不表示店铺只经营该类商品。

覆盖：

- 女装
- 女装连衣裙
- 男装
- 运动服
- 运动类服装
- 内衣

这些店名来自公开类目页的店铺信息，来源页包括 [女装](https://shop.tiktok.com/us/k/clothing-for-women)、[女装连衣裙](https://shop.tiktok.com/us/k/women-dresses)、[男装](https://shop.tiktok.com/us/k/clothing-for-men)、[运动服](https://shop.tiktok.com/us/k/activewear)、[运动类服装](https://shop.tiktok.com/us/k/sportswear-shop) 和 [内衣](https://shop.tiktok.com/us/k/lingerie)。

## 字段说明

- `region`：市场区域，当前为 `US`。
- `category`：服装标准类目。
- `seed_type`：种子类型，当前为 `shop_name`。
- `seed_value`：公开类目页观察到的店铺名称。
- `shop_url`：已核验的公开店铺入口。当前 100 条均已填写，链接包含真实平台店铺 ID，不是按店名直接填出来的地址。
- `source_url`：当前名单使用本次实际访问的店铺页面；原始类目来源均保存在归档名单中。
- `verification_status`：verified_public_shop_page 表示店铺页面名称/身份已核验；observed_public_category_page 仍只是类目页名称线索。
- `observed_at`：当前名单均为 2026-10-13 的核验观察，归档保留 2026-09-25 的原始记录。

核验依据和原始 row_hash 的对应关系见 `data/samples/seed-links/verification.json`，其中记录真实 ID、核验时间与脱敏证据路径。候选 JSON 只供继续核验，不等同于可直接使用的名单。

## 后续处理

店铺发现 Worker 读取 `seed_value` 和 `source_url`，解析并补充：

```text
shop_id
shop_url
shop_name_normalized
region
discovery_category
first_seen_at
```

解析成功后再写入 `shops` 表，并以 `(platform, region, platform_shop_id)` 作为唯一键。名称不是身份，原始 CSV 不回写覆盖。

本次已把核验链接同步到现有数据库，保留原始 100 条观察及其 row_hash，并为 22 个替换店铺新增观察。当前 100 个采集目标都有可用入口及待执行商品任务，原始历史记录不删除。无需为了使用这些链接再重复导入新名单。

继续核验候选链接（不改 CSV；同一天已核验结果可复用）：

```powershell
python -m scripts.verify_seed_links
python -m scripts.verify_seed_links --sync-database
```

新名单的链接、来源和日期发生变化，因此它是新的观察数据。将它交给 seed_shops 导入时会保留既有原始观察并新增对应观察；这不是新增同等数量的正式店铺，shops 仍按真实 ID 去重。不要把 row_hash 手工改为旧值。

部分原店名的商品页面触发安全验证，本次没有绕过。用户选择替换后，原店名保留在归档和替换对照中，相关未执行任务已取消，不影响当前 100 个目标。详见[核验报告](../../docs/seed-link-verification.md)和[导入约定](../../docs/seed-discovery.md)。

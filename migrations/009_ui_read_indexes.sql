-- Online covering indexes for UI reads and background candidate scans.
ALTER TABLE `product_read_models` ADD INDEX `ix_read_products_catalog` (`schema_version`, `basis`, `platform`, `region`, `currency`, `pricing_context`, `eligible`, `observed_at`, `product_id`, `shop_id`, `price_min`, `stock_status`, `sku_count`, `rating`, `review_count`, `title`), ALGORITHM=INPLACE, LOCK=NONE;
ALTER TABLE `products` ADD INDEX `ix_products_recent_detail` (`platform`, `region`, `last_success_at`, `id`, `title`), ALGORITHM=INPLACE, LOCK=NONE;

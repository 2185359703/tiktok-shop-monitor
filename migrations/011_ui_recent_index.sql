-- Online scoped ordering index for bounded product pages.
ALTER TABLE `product_read_models` ADD INDEX `ix_read_products_recent_scope` (`schema_version`, `basis`, `platform`, `region`, `currency`, `pricing_context`, `eligible`, `observed_at`, `product_id`, `shop_id`), ALGORITHM=INPLACE, LOCK=NONE;

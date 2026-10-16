-- Rebuildable UI read models. Apply with scripts.migrate_analytics.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS analytics_refresh_requests (
	schema_version INTEGER NOT NULL COMMENT '派生结构版本',
	kind VARCHAR(32) NOT NULL COMMENT '刷新种类或控制行',
	key_hash VARCHAR(64) NOT NULL COMMENT '规范化参数SHA256',
	parameters JSON NOT NULL COMMENT '作用域与有界参数',
	requested_revision BIGINT UNSIGNED NOT NULL COMMENT '最新请求修订',
	completed_revision BIGINT UNSIGNED NOT NULL COMMENT '成功处理修订',
	claimed_revision BIGINT UNSIGNED NOT NULL COMMENT '本次领取修订',
	is_pending BOOL NOT NULL COMMENT '到期扫描索引标记；修订号仍为处理依据',
	pending_since DATETIME(6) COMMENT '最早未处理请求时间',
	available_at DATETIME(6) NOT NULL COMMENT '可再次领取时间',
	last_requested_at DATETIME(6) NOT NULL COMMENT '最近访问或请求时间',
	lease_token VARCHAR(36) COMMENT '领取所有权令牌',
	lease_until DATETIME(6) COMMENT '数据库时间下的租约期限',
	attempts INTEGER NOT NULL COMMENT '连续失败次数',
	last_error_code VARCHAR(80) COMMENT '脱敏失败类型',
	CONSTRAINT pk_analytics_refresh_requests PRIMARY KEY (schema_version, kind, key_hash),
	CONSTRAINT ck_analytics_refresh_requests_revision_order CHECK (requested_revision >= completed_revision)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='按资源合并的统计刷新需求及租约控制';

CREATE INDEX ix_analytics_due ON analytics_refresh_requests (kind, available_at, lease_until);

CREATE INDEX ix_analytics_pending ON analytics_refresh_requests (schema_version, is_pending, available_at, key_hash);

CREATE TABLE IF NOT EXISTS analytics_snapshots (
	schema_version INTEGER NOT NULL COMMENT '派生结构版本',
	key_hash VARCHAR(64) NOT NULL COMMENT '规范化查询键',
	kind VARCHAR(32) NOT NULL COMMENT '统计主题',
	parameters JSON NOT NULL COMMENT '市场、口径与过滤条件',
	payload JSON NOT NULL COMMENT '有限摘要，不含商品全集',
	generation BIGINT UNSIGNED NOT NULL COMMENT '发布修订',
	snapshot_at DATETIME(6) NOT NULL COMMENT '派生读取视图时间',
	generated_at DATETIME(6) NOT NULL COMMENT '本结果生成时间',
	fresh_until DATETIME(6) NOT NULL COMMENT '新鲜期截止',
	stale_until DATETIME(6) NOT NULL COMMENT '允许显示旧结果的截止时间',
	CONSTRAINT pk_analytics_snapshots PRIMARY KEY (schema_version, key_hash)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='原子发布的小型图表统计；重建期间保留上一成功结果';

CREATE TABLE IF NOT EXISTS event_read_models (
	schema_version INTEGER NOT NULL COMMENT '派生结构版本',
	event_id BIGINT UNSIGNED NOT NULL COMMENT '源事件主键',
	platform VARCHAR(32) NOT NULL COMMENT '来源平台',
	region VARCHAR(2) NOT NULL COMMENT '市场',
	currency VARCHAR(3) NOT NULL COMMENT '快照币种',
	pricing_context VARCHAR(64) NOT NULL COMMENT '快照报价上下文',
	product_id BIGINT UNSIGNED NOT NULL COMMENT '商品主键',
	shop_id BIGINT UNSIGNED NOT NULL COMMENT '店铺主键',
	event_type VARCHAR(32) NOT NULL COMMENT '事件类型',
	title VARCHAR(512) COMMENT '事件发生时的商品标题',
	observed_at DATETIME(6) NOT NULL COMMENT '后快照观察时间',
	published_sequence BIGINT UNSIGNED NOT NULL COMMENT '同事务发布序号；分页水位',
	payload JSON NOT NULL COMMENT '历史规格和前后值；保留源快照引用',
	CONSTRAINT pk_event_read_models PRIMARY KEY (schema_version, event_id)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='从不可变快照提取的历史事件分页投影';

CREATE INDEX ix_read_events_time ON event_read_models (platform, region, observed_at, event_id);

CREATE INDEX ix_read_events_type ON event_read_models (platform, region, event_type, observed_at, event_id);

CREATE TABLE IF NOT EXISTS product_read_models (
	schema_version INTEGER NOT NULL COMMENT '派生结构版本',
	basis VARCHAR(32) NOT NULL COMMENT '统计口径',
	product_id BIGINT UNSIGNED NOT NULL COMMENT '源商品主键',
	platform VARCHAR(32) NOT NULL COMMENT '来源平台',
	region VARCHAR(2) NOT NULL COMMENT '市场',
	currency VARCHAR(3) NOT NULL COMMENT '币种',
	pricing_context VARCHAR(64) NOT NULL COMMENT '报价上下文',
	shop_id BIGINT UNSIGNED NOT NULL COMMENT '源店铺主键',
	platform_product_id VARCHAR(128) NOT NULL COMMENT '平台商品标识',
	title VARCHAR(512) COMMENT '该口径商品标题',
	category TEXT NOT NULL COMMENT '该口径有效类目',
	brand VARCHAR(255) COMMENT '公开品牌',
	price_min NUMERIC(12, 2) COMMENT '公开起价',
	stock_status VARCHAR(32) NOT NULL COMMENT '库存状态；未知不置零',
	sku_count INTEGER COMMENT '当前观察规格数量；未采详情为空',
	rating NUMERIC(3, 2) COMMENT '公开评分',
	review_count BIGINT UNSIGNED COMMENT '公开评价数',
	observed_at DATETIME(6) COMMENT '源观察时间',
	eligible BOOL NOT NULL COMMENT '是否满足本口径来源条件',
	payload JSON NOT NULL COMMENT '不含源大JSON和正文的列表字段',
	facts JSON NOT NULL COMMENT '仅后台读取的规格计数与历史质量',
	applied_revision BIGINT UNSIGNED NOT NULL COMMENT '已发布刷新修订号',
	projected_at DATETIME(6) NOT NULL COMMENT '投影生成时间',
	CONSTRAINT pk_product_read_models PRIMARY KEY (schema_version, basis, product_id),
	CONSTRAINT ck_product_read_models_basis CHECK (basis IN ('current','latest_valid_snapshot'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='可重建的商品分页与统计投影；区分当前观察和历史快照口径';

CREATE INDEX ix_read_products_catalog ON product_read_models (schema_version, basis, platform, region, currency, pricing_context, eligible, observed_at, product_id, shop_id, price_min, stock_status, sku_count, rating, review_count, title);

CREATE INDEX ix_read_products_price ON product_read_models (platform, region, basis, price_min, product_id);

CREATE INDEX ix_read_products_recent ON product_read_models (platform, region, basis, observed_at, product_id);

CREATE INDEX ix_read_products_shop ON product_read_models (platform, region, basis, shop_id, product_id);

CREATE INDEX ix_read_products_sku ON product_read_models (platform, region, basis, sku_count, product_id);

CREATE INDEX ix_read_products_stock ON product_read_models (platform, region, basis, stock_status, product_id);

CREATE INDEX ix_read_products_title ON product_read_models (platform, region, basis, title, product_id);

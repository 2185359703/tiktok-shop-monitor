-- Add human-reviewed product and exact SKU mappings to the selected existing database.

-- Requires MySQL >= 8.0.16. No catalog identifiers are merged or changed.

SET time_zone = '+00:00';

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS product_matches (
	left_product_id BIGINT UNSIGNED NOT NULL COMMENT '较小的商品内部主键', 
	right_product_id BIGINT UNSIGNED NOT NULL COMMENT '较大的商品内部主键', 
	status VARCHAR(32) NOT NULL COMMENT '关系状态：confirmed=人工确认，rejected=已撤销或排除', 
	source VARCHAR(32) NOT NULL COMMENT '关系来源；manual_confirmation 表示用户明确确认', 
	confidence NUMERIC(5, 4) COMMENT '规则建议分数，范围0至1；不是同款概率，也不是人工结论', 
	rule_version VARCHAR(32) NOT NULL COMMENT '生成建议证据的规则版本', 
	evidence JSON NOT NULL COMMENT '确认时的公开标题、品牌、类目和规格证据', 
	note VARCHAR(1000) COMMENT '用户填写的匹配复核备注', 
	reviewed_at DATETIME(6) NOT NULL COMMENT '最后人工确认或撤销的时间（UTC）', 
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT, 
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop', 
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写', 
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6), 
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6), 
	CONSTRAINT pk_product_matches PRIMARY KEY (id), 
	CONSTRAINT uq_product_matches_pair UNIQUE (left_product_id, right_product_id), 
	CONSTRAINT uq_product_matches_scope UNIQUE (id, left_product_id, right_product_id), 
	CONSTRAINT ck_product_matches_ordered_pair CHECK (left_product_id < right_product_id), 
	CONSTRAINT ck_product_matches_confidence CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)), 
	CONSTRAINT ck_product_matches_status CHECK (status IN ('confirmed', 'rejected')), 
	CONSTRAINT fk_product_matches_left_product_id_products FOREIGN KEY(left_product_id) REFERENCES products (id) ON DELETE RESTRICT, 
	CONSTRAINT fk_product_matches_right_product_id_products FOREIGN KEY(right_product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人工确认或撤销的商品同款关系；有序商品对避免重复';

CREATE INDEX ix_product_matches_market_status ON product_matches (platform, region, status);

CREATE TABLE IF NOT EXISTS sku_matches (
	product_match_id BIGINT UNSIGNED NOT NULL COMMENT '已持久化同款商品关系的内部主键', 
	left_product_id BIGINT UNSIGNED NOT NULL COMMENT '关系左侧商品内部主键，用于校验规格归属', 
	right_product_id BIGINT UNSIGNED NOT NULL COMMENT '关系右侧商品内部主键，用于校验规格归属', 
	left_variant_id BIGINT UNSIGNED NOT NULL COMMENT '左側商品的规格内部主键', 
	right_variant_id BIGINT UNSIGNED NOT NULL COMMENT '右侧商品的规格内部主键', 
	status VARCHAR(32) NOT NULL COMMENT 'SKU映射状态：confirmed=人工确认，rejected=已撤销', 
	source VARCHAR(32) NOT NULL COMMENT '映射来源，manual_confirmation 表示用户明确确认', 
	rule_version VARCHAR(32) NOT NULL COMMENT '完整规格校验规则版本', 
	evidence JSON NOT NULL COMMENT '确认时双方规格属性和价格口径证据', 
	note VARCHAR(1000) COMMENT '用户填写的SKU映射备注', 
	reviewed_at DATETIME(6) NOT NULL COMMENT '最后确认或撤销时间（UTC）', 
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT, 
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6), 
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6), 
	CONSTRAINT pk_sku_matches PRIMARY KEY (id), 
	CONSTRAINT fk_sku_matches_product_pair FOREIGN KEY(product_match_id, left_product_id, right_product_id) REFERENCES product_matches (id, left_product_id, right_product_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_sku_matches_left_variant FOREIGN KEY(left_variant_id, left_product_id) REFERENCES product_variants (id, product_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_sku_matches_right_variant FOREIGN KEY(right_variant_id, right_product_id) REFERENCES product_variants (id, product_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_sku_matches_pair UNIQUE (product_match_id, left_variant_id, right_variant_id), 
	CONSTRAINT ck_sku_matches_ordered_product_pair CHECK (left_product_id < right_product_id), 
	CONSTRAINT ck_sku_matches_distinct_variants CHECK (left_variant_id <> right_variant_id), 
	CONSTRAINT ck_sku_matches_status CHECK (status IN ('confirmed', 'rejected'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='已人工复核的精确SKU映射；商品和规格归属由复合外键约束';

CREATE INDEX ix_sku_matches_status ON sku_matches (product_match_id, status);

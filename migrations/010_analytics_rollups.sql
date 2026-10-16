-- Incremental product summaries. Apply with scripts.migrate_analytics_rollups.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS analytics_product_rollups (
	schema_version INTEGER NOT NULL COMMENT '分片统计结构版本',
	basis VARCHAR(32) NOT NULL COMMENT '当前观察或最新有效历史快照口径',
	shard_id BIGINT UNSIGNED NOT NULL COMMENT '商品主键除以128的分片编号',
	members JSON NOT NULL COMMENT '有界商品贡献；按修订替换以保证幂等',
	summary JSON NOT NULL COMMENT '按市场及报价上下文隔离的精确统计摘要',
	CONSTRAINT pk_analytics_product_rollups PRIMARY KEY (schema_version, basis, shard_id)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='每128个商品一片的增量统计；与商品投影同事务发布';

CREATE TABLE IF NOT EXISTS product_read_attributes (
	schema_version INTEGER NOT NULL COMMENT '商品投影结构版本',
	basis VARCHAR(32) NOT NULL COMMENT '商品观察口径',
	product_id BIGINT UNSIGNED NOT NULL COMMENT '源商品主键',
	attribute VARCHAR(16) NOT NULL COMMENT '颜色尺码及服装属性维度',
	ordinal INTEGER NOT NULL COMMENT '该商品该维度的标签序号',
	value TEXT NOT NULL COMMENT '去除首尾空格的来源标签；不合并同义词',
	CONSTRAINT pk_product_read_attributes PRIMARY KEY (schema_version, basis, product_id, attribute, ordinal)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品属性筛选索引；保留原始标签的SQL比较语义';

CREATE INDEX ix_read_attribute_value ON product_read_attributes (schema_version, basis, attribute, value(128), product_id);

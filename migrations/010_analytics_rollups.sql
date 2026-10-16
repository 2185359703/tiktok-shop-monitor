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

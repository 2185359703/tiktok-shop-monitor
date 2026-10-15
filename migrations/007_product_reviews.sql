-- Add public reviews and allow their independent coroutine task.
-- Repeated migration: python -m scripts.migrate_product_reviews --execute
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS product_reviews (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '商品内部主键，市场与店铺沿商品关联',
	platform_review_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '来源评论ID，与商品共同去重',
	platform_variant_id VARCHAR(128) COLLATE utf8mb4_bin COMMENT '来源SKU标识；缺失保留空值',
	rating INTEGER COMMENT '评论评分，1至5；缺失为空',
	text TEXT COMMENT '评论纯文本正文；无正文评分允许为空',
	reviewer_name VARCHAR(255) COMMENT '公开展示昵称；不采集用户ID或个人主页',
	reviewed_at DATETIME(6) COMMENT '来源评论时间（UTC），未提供时为空',
	sku_specification TEXT COMMENT '评论显示的购买规格原文',
	country VARCHAR(16) COMMENT '评论来源国家；不可由店铺市场推断',
	verified_purchase BOOL COMMENT '来源已验证购买标记；未提供为空',
	incentivized BOOL COMMENT '来源激励评论标记；未提供为空',
	images JSON NOT NULL COMMENT '公开买家图片链接列表；不下载媒体',
	content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '白名单评论字段摘要，用于识别内容更新',
	content_version INTEGER NOT NULL COMMENT '评论内容版本；内容变化时增加',
	first_seen_at DATETIME(6) NOT NULL COMMENT '首次观察时间（UTC）',
	last_seen_at DATETIME(6) NOT NULL COMMENT '最近成功观察时间（UTC）',
	source_url TEXT NOT NULL COMMENT '去除跟踪参数的公开商品来源链接',
	source_kind VARCHAR(16) NOT NULL COMMENT '评论来源：document首屏或api分页',
	source_version VARCHAR(64) NOT NULL COMMENT '评论解析器版本',
	response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '来源响应摘要；不保存原始响应',
	task_id BIGINT UNSIGNED NOT NULL COMMENT '最近成功更新评论的任务内部主键',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_product_reviews PRIMARY KEY (id),
	CONSTRAINT uq_product_reviews_identity UNIQUE (product_id, platform_review_id),
	CONSTRAINT ck_product_reviews_rating CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
	CONSTRAINT ck_product_reviews_content_version CHECK (content_version >= 1),
	CONSTRAINT fk_product_reviews_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT,
	CONSTRAINT fk_product_reviews_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品公开评论正文、评分与来源；不包含账户标识或会话凭据';

CREATE INDEX ix_product_reviews_product_page ON product_reviews (product_id, id);

ALTER TABLE crawl_tasks DROP CHECK `ck_crawl_tasks_task_type`, ADD CONSTRAINT `ck_crawl_tasks_task_type` CHECK (task_type IN ('shop_discovery', 'shop_profile', 'product_discovery', 'product_detail', 'product_reviews', 'product_snapshot', 'change_detection', 'report_generation'));

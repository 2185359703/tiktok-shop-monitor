-- Additive merchant content storage. Existing tables and data are unchanged.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS product_contents (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '商品内部主键，每个商品保留一份最新介绍',
	description_text MEDIUMTEXT COMMENT '商家介绍纯文本投影，不含评论与推断内容',
	sections JSON NOT NULL COMMENT '商品参数与介绍的有序安全节点JSON',
	completeness VARCHAR(16) NOT NULL COMMENT '内容覆盖：missing缺失、empty显式空、partial部分、complete完整',
	unsupported_blocks JSON NOT NULL COMMENT '未支持节点或缺失结构的原因代码数组',
	source_shape JSON NOT NULL COMMENT '白名单来源结构、章节覆盖与各章节及最新观察的证据',
	source_version VARCHAR(64) NOT NULL COMMENT '介绍解析版本，独立于商品与图片解析版本',
	content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '介绍节点、文本与覆盖状态的SHA256摘要',
	content_version INTEGER NOT NULL COMMENT '独立介绍版本，仅内容摘要变化时增加',
	source_url TEXT NOT NULL COMMENT '实际观察介绍的公开商品详情链接',
	response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '本次公开详情响应的SHA256证据摘要',
	observed_at DATETIME(6) NOT NULL COMMENT '当前保留资料最早的章节观察时间（UTC），分章节与最新观察见source_shape',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_product_contents PRIMARY KEY (id),
	CONSTRAINT uq_product_contents_product UNIQUE (product_id),
	CONSTRAINT ck_product_contents_completeness CHECK (completeness IN ('missing', 'empty', 'partial', 'complete')),
	CONSTRAINT ck_product_contents_content_version CHECK (content_version >= 1),
	CONSTRAINT fk_product_contents_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品公开介绍的安全有序节点、参数与独立采集证据';

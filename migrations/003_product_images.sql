-- Optional additive migration: no existing table is altered.
-- Apply only during an approved release after the active collection finishes.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS product_images (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '商品内部主键', 
	image_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '来源照片 URI 的 SHA256 业务去重键', 
	image_url TEXT NOT NULL COMMENT '平台公开图片 HTTP(S) 链接', 
	thumbnail_url TEXT COMMENT '平台公开缩略图链接，缺失时为空', 
	source_url TEXT NOT NULL COMMENT '观察图片的公开商品或店铺页面', 
	source_kind VARCHAR(16) NOT NULL COMMENT 'detail=详情相册，listing=店铺列表封面', 
	source_version VARCHAR(64) NOT NULL COMMENT '图片解析版本，独立于价格和 SKU 哈希', 
	`role` VARCHAR(16) NOT NULL COMMENT 'cover=封面，gallery=相册图片', 
	position INTEGER NOT NULL COMMENT '公开相册顺序，从零开始', 
	is_current BOOL NOT NULL COMMENT '是否在最新完整且非空的公开相册中出现' DEFAULT 1, 
	observed_at DATETIME(6) NOT NULL COMMENT '图片最近观察时间（UTC）', 
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT, 
	CONSTRAINT pk_product_images PRIMARY KEY (id), 
	CONSTRAINT uq_product_images_identity UNIQUE (product_id, image_key), 
	CONSTRAINT ck_product_images_role CHECK (role IN ('cover', 'gallery')), 
	CONSTRAINT ck_product_images_source_kind CHECK (source_kind IN ('detail', 'listing')), 
	CONSTRAINT ck_product_images_position CHECK (position >= 0 AND position < 100), 
	CONSTRAINT fk_product_images_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='公开商品原图与缩略图链接、来源及最新相册顺序';

CREATE INDEX ix_product_images_cover ON product_images (product_id, is_current, position);

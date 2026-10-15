-- Shop profile extension; apply once to the existing database.
-- For repeatable execution use python -m scripts.migrate_shop_profiles --execute.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

ALTER TABLE shops
    ADD COLUMN logo_url TEXT COMMENT '本次观察到的公开店铺头像地址；未获取时为空',
    ADD COLUMN verification_type VARCHAR(32) COMMENT '来源明确展示的认证类型，例如 blue_v；空不代表未认证',
    ADD COLUMN identity_label_key VARCHAR(64) COMMENT '来源店铺身份标签代码，例如 official',
    ADD COLUMN identity_label_text VARCHAR(255) COMMENT '来源店铺身份标签展示文案，例如 OFFICIAL SHOP',
    ADD COLUMN follower_count_text VARCHAR(128) COMMENT '本次公开粉丝数原始展示文案，保留缩写和加号',
    ADD COLUMN sales_count BIGINT UNSIGNED COMMENT '本次公开店铺累计已售精确值；不能视为指定时期销量',
    ADD COLUMN sales_count_text VARCHAR(128) COMMENT '本次公开店铺累计已售原始展示文案',
    ADD COLUMN global_sales_count BIGINT UNSIGNED COMMENT '本次来源标注的全球累计已售精确值；未获取时为空',
    ADD COLUMN global_sales_count_text VARCHAR(128) COMMENT '本次来源标注的全球累计已售原始展示文案',
    ADD COLUMN video_count BIGINT UNSIGNED COMMENT '本次店铺页展示的视频数量；不推断视频归属口径',
    ADD COLUMN video_count_text VARCHAR(128) COMMENT '本次店铺页展示的视频数原始文案',
    ADD COLUMN product_count_text VARCHAR(128) COMMENT '平台展示的在售商品数原始文案；与商品数观察时间一致',
    ADD COLUMN review_count BIGINT UNSIGNED COMMENT '本次公开店铺评价数量；未获取时为空',
    ADD COLUMN product_count_captured_at DATETIME(6) COMMENT '当前平台商品数的独立实际观察时间（UTC）',
    ADD COLUMN profile_captured_at DATETIME(6) COMMENT '当前店铺档案实际观察时间（UTC），独立于商品发现时间',
    ADD COLUMN profile_source_url TEXT COMMENT '当前档案的公开来源链接，移除跟踪与签名参数',
    ADD COLUMN profile_parser_version VARCHAR(64) COMMENT '当前店铺档案解析器版本',
    ADD COLUMN profile_response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '当前店铺档案来源响应的SHA-256摘要',
    ADD COLUMN profile_evidence_path TEXT COMMENT '当前档案白名单业务证据文件路径',
    ADD COLUMN profile_field_state JSON COMMENT '当前档案各字段状态、来源路径和指标口径',
    ADD COLUMN profile_observation_status VARCHAR(16) COMMENT '当前档案观察完整度：complete=完整，partial=部分字段缺失或无效',
    ADD CONSTRAINT `ck_shops_profile_observation_status` CHECK (profile_observation_status IS NULL OR profile_observation_status IN ('complete', 'partial'));

ALTER TABLE crawl_tasks DROP CHECK `ck_crawl_tasks_task_type`, ADD CONSTRAINT `ck_crawl_tasks_task_type` CHECK (task_type IN ('shop_discovery', 'shop_profile', 'product_discovery', 'product_detail', 'product_snapshot', 'change_detection', 'report_generation')), MODIFY COLUMN task_type VARCHAR(32) NOT NULL COMMENT '任务类型：shop_discovery=店铺发现，shop_profile=店铺档案，product_discovery=商品发现，product_detail=详情，product_snapshot=快照，change_detection=变化检测，report_generation=报表';

CREATE TABLE IF NOT EXISTS shop_profile_snapshots (
	shop_id BIGINT UNSIGNED NOT NULL COMMENT '所属店铺内部主键，和平台及市场共同校验归属',
	task_id BIGINT UNSIGNED NOT NULL COMMENT '档案采集任务内部主键；同一任务只能写入一次',
	platform_shop_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '本次确认的平台稳定店铺ID，按字符串保存',
	canonical_name VARCHAR(255) NOT NULL COMMENT '本次观察到的店铺名称',
	canonical_url TEXT NOT NULL COMMENT '本次观察到的标准化店铺公开入口',
	scheduled_for DATETIME(6) NOT NULL COMMENT '逻辑采集窗口时间（UTC）；任务重试保持不变',
	captured_at DATETIME(6) NOT NULL COMMENT '本次实际读取店铺档案的时间（UTC）',
	logo_url TEXT COMMENT '本次公开店铺头像地址；未获取时为空',
	verification_type VARCHAR(32) COMMENT '本次来源明确展示的认证类型，例如 blue_v',
	identity_label_key VARCHAR(64) COMMENT '本次来源店铺身份标签代码，例如 official',
	identity_label_text VARCHAR(255) COMMENT '本次来源店铺身份标签展示文案',
	follower_count BIGINT UNSIGNED COMMENT '本次公开店铺粉丝数精确值；未提供精确值时为空',
	follower_count_text VARCHAR(128) COMMENT '本次公开粉丝数原始展示文案，保留缩写和加号',
	sales_count BIGINT UNSIGNED COMMENT '本次公开店铺累计已售精确值；不能视为指定时期销量',
	sales_count_text VARCHAR(128) COMMENT '本次公开店铺累计已售原始展示文案',
	global_sales_count BIGINT UNSIGNED COMMENT '本次来源标注的全球累计已售精确值',
	global_sales_count_text VARCHAR(128) COMMENT '本次来源标注的全球累计已售原始展示文案',
	video_count BIGINT UNSIGNED COMMENT '本次店铺页展示的视频数量；不推断视频归属口径',
	video_count_text VARCHAR(128) COMMENT '本次店铺页视频数原始展示文案',
	product_count BIGINT UNSIGNED COMMENT '本次平台展示的在售商品数，区别于本系统已采集数量',
	product_count_text VARCHAR(128) COMMENT '本次在售商品数原始展示文案',
	rating NUMERIC(3, 2) COMMENT '本次公开店铺评分，范围0-5；未获取时为空',
	review_count BIGINT UNSIGNED COMMENT '本次公开店铺评价数量；未获取时为空',
	source_url TEXT NOT NULL COMMENT '本次公开来源链接，移除跟踪与签名参数',
	response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '本次来源响应的SHA-256摘要',
	parser_version VARCHAR(64) NOT NULL COMMENT '生成本次店铺档案的解析器版本',
	evidence_path TEXT COMMENT '本次白名单业务证据文件路径',
	field_state JSON NOT NULL COMMENT '本次各字段状态、来源路径及指标口径，不包含原始页面或凭据',
	observation_status VARCHAR(16) NOT NULL COMMENT '本次观察完整度：complete=完整，partial=部分字段缺失或无效',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_shop_profile_snapshots PRIMARY KEY (id),
	CONSTRAINT uq_shop_profile_snapshots_task UNIQUE (task_id),
	CONSTRAINT uq_shop_profile_snapshots_window UNIQUE (platform, region, shop_id, scheduled_for),
	CONSTRAINT fk_shop_profile_snapshots_shop_scope FOREIGN KEY(shop_id, platform, region) REFERENCES shops (id, platform, region) ON DELETE RESTRICT,
	CONSTRAINT ck_shop_profile_snapshots_rating CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
	CONSTRAINT ck_shop_profile_snapshots_observation_status CHECK (observation_status IN ('complete', 'partial')),
	CONSTRAINT fk_shop_profile_snapshots_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='按逻辑采集窗口保存的店铺档案与经营指标不可变观察';

CREATE INDEX ix_shop_profile_snapshots_history ON shop_profile_snapshots (shop_id, captured_at, id);

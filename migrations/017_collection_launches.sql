-- Explicit collection launches and durable task membership.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS collection_launches (
	request_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '市场与提交UUID生成的唯一幂等键',
	request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '规范化采集定义摘要；同一请求不能改变范围',
	name VARCHAR(80) NOT NULL COMMENT '用户可识别的批次名称',
	task_type VARCHAR(32) NOT NULL COMMENT '本批次最初派发的来源任务类型',
	requested_count INTEGER NOT NULL COMMENT '明确提交且去重后的资源或店铺链接数量',
	created_task_count INTEGER NOT NULL COMMENT '本次新建的根任务数量' DEFAULT '0',
	reused_task_count INTEGER NOT NULL COMMENT '复用已存在根任务的数量；不表示新增来源请求' DEFAULT '0',
	max_pages INTEGER COMMENT '每个资源的分页上限；仅商品发现与评论适用',
	cancel_requested_at DATETIME(6) COMMENT '持久取消请求时间；阻止独占后续任务开始',
	cancel_completed_at DATETIME(6) COMMENT '当前关联的活动任务全部结束时间；保留共享任务的实际执行历史',
	cancel_pending BOOL NOT NULL COMMENT '是否仍需有界清理；共享任务活动时保留检查资格' DEFAULT '0',
	cancel_checked_at DATETIME(6) COMMENT '最近清理检查时间；用于公平轮转而非始终重复最早的批次',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_collection_launches PRIMARY KEY (id),
	CONSTRAINT uq_collection_launches_request UNIQUE (request_key),
	CONSTRAINT ck_collection_launches_requested_count CHECK (requested_count BETWEEN 1 AND 100),
	CONSTRAINT ck_collection_launches_task_type CHECK (task_type IN ('shop_discovery', 'product_discovery', 'product_detail', 'product_reviews'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='人工明确范围的一次性采集批次；保存幂等结果与真实任务链路';

CREATE INDEX ix_collection_launches_cancellation ON collection_launches (platform, region, cancel_pending, cancel_checked_at, id);

CREATE INDEX ix_collection_launches_market ON collection_launches (platform, region, id);

CREATE TABLE IF NOT EXISTS collection_launch_tasks (
	launch_id BIGINT UNSIGNED NOT NULL COMMENT '所属人工采集批次内部ID',
	task_id BIGINT UNSIGNED NOT NULL COMMENT '准确关联的任务内部ID',
	member_role VARCHAR(16) NOT NULL COMMENT 'root为原始根任务，descendant为后续任务，retry为补采任务',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_collection_launch_tasks PRIMARY KEY (id),
	CONSTRAINT uq_collection_launch_tasks_member UNIQUE (launch_id, task_id),
	CONSTRAINT ck_collection_launch_tasks_member_role CHECK (member_role IN ('root', 'descendant', 'retry')),
	CONSTRAINT fk_collection_launch_tasks_launch_id_collection_launches FOREIGN KEY(launch_id) REFERENCES collection_launches (id) ON DELETE RESTRICT,
	CONSTRAINT fk_collection_launch_tasks_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='一次性采集的根任务、后续分页处理及保留历史的重试成员';

CREATE INDEX ix_collection_launch_tasks_task ON collection_launch_tasks (task_id);

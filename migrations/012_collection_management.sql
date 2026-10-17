-- Durable collection management. Apply with scripts.migrate_collection_management.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS collection_controls (
	paused BOOL NOT NULL COMMENT '暂停领取新的来源任务；已有租约允许完成' DEFAULT '0',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_collection_controls PRIMARY KEY (id),
	CONSTRAINT uq_collection_controls_market UNIQUE (platform, region)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='持久化的人工采集暂停开关；独立于来源访问保护冷却';

CREATE TABLE IF NOT EXISTS collection_operations (
	action VARCHAR(32) NOT NULL COMMENT '固定操作分类',
	target_type VARCHAR(16) NOT NULL COMMENT 'market、plan或task',
	target_id VARCHAR(128) NOT NULL COMMENT '目标数据库ID、任务UUID或市场代码',
	details JSON NOT NULL COMMENT '白名单结果字段；不保存密钥、原始请求、错误文本或任意备注',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_collection_operations PRIMARY KEY (id)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='采集控制操作审计；仅固定动作码和非敏感结构化结果';

CREATE INDEX ix_collection_operations_market ON collection_operations (platform, region, id);

CREATE TABLE IF NOT EXISTS collection_plans (
	request_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '创建请求的市场隔离幂等键',
	request_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '初次计划定义摘要，拒绝复用请求ID改变定义',
	name VARCHAR(80) NOT NULL COMMENT '计划显示名称',
	task_type VARCHAR(32) NOT NULL COMMENT 'shop_profile或product_snapshot',
	resource_ids JSON NOT NULL COMMENT '最多100个同市场数据库资源ID字符串',
	schedule_mode VARCHAR(16) NOT NULL COMMENT 'once=单次，interval=周期',
	interval_seconds INTEGER COMMENT '周期秒数；单次计划为空',
	enabled BOOL NOT NULL COMMENT '是否允许调度器自动生成本计划的新执行' DEFAULT '1',
	next_run_at DATETIME(6) COMMENT '下一逻辑执行时间；遗漏周期合并为一次',
	last_run_at DATETIME(6) COMMENT '最近生成执行记录的时间',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_collection_plans PRIMARY KEY (id),
	CONSTRAINT uq_collection_plans_request UNIQUE (request_key),
	CONSTRAINT ck_collection_plans_task_type CHECK (task_type IN ('shop_profile', 'product_snapshot')),
	CONSTRAINT ck_collection_plans_schedule_mode CHECK (schedule_mode IN ('once', 'interval')),
	CONSTRAINT ck_collection_plans_interval CHECK ((schedule_mode = 'once' AND interval_seconds IS NULL) OR (schedule_mode = 'interval' AND interval_seconds BETWEEN 60 AND 604800))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='显式资源名单的单次或周期采集计划；不隐式扩大采集范围';

CREATE INDEX ix_collection_plans_due ON collection_plans (platform, region, enabled, next_run_at, id);

CREATE TABLE IF NOT EXISTS collection_runs (
	plan_id BIGINT UNSIGNED NOT NULL COMMENT '所属采集计划',
	run_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '计划加逻辑窗口或手动请求ID的幂等键',
	trigger_kind VARCHAR(16) NOT NULL COMMENT 'scheduled或manual',
	scheduled_for DATETIME(6) NOT NULL COMMENT '本次任务共同的逻辑观察窗口',
	requested_count INTEGER NOT NULL COMMENT '本次固定范围内的根资源数',
	error_code VARCHAR(64) COMMENT '生成任务失败的固定错误分类；不保存原始异常或请求',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_collection_runs PRIMARY KEY (id),
	CONSTRAINT uq_collection_runs_key UNIQUE (run_key),
	CONSTRAINT ck_collection_runs_trigger_kind CHECK (trigger_kind IN ('scheduled', 'manual')),
	CONSTRAINT ck_collection_runs_requested_count CHECK (requested_count BETWEEN 1 AND 100),
	CONSTRAINT fk_collection_runs_plan_id_collection_plans FOREIGN KEY(plan_id) REFERENCES collection_plans (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='计划每次执行的固定逻辑窗口；状态从关联根任务推导';

CREATE INDEX ix_collection_runs_plan ON collection_runs (plan_id, id);

CREATE TABLE IF NOT EXISTS collection_run_tasks (
	run_id BIGINT UNSIGNED NOT NULL COMMENT '计划执行记录',
	task_id BIGINT UNSIGNED NOT NULL COMMENT '关联的采集根任务内部ID',
	member_role VARCHAR(16) NOT NULL COMMENT 'root=本次原始根任务，retry=保留原结果的后续重试；二者都参与防重叠检查' DEFAULT 'root',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_collection_run_tasks PRIMARY KEY (id),
	CONSTRAINT uq_collection_run_tasks_member UNIQUE (run_id, task_id),
	CONSTRAINT ck_collection_run_tasks_member_role CHECK (member_role IN ('root', 'retry')),
	CONSTRAINT fk_collection_run_tasks_run_id_collection_runs FOREIGN KEY(run_id) REFERENCES collection_runs (id) ON DELETE RESTRICT,
	CONSTRAINT fk_collection_run_tasks_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='计划执行与根任务的精确关联；不将下游任务冒充完整覆盖';

CREATE INDEX ix_collection_run_tasks_task ON collection_run_tasks (task_id);

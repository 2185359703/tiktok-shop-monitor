-- Generated from app.models by python -m app.storage.schema --print-sql

-- Requires MySQL >= 8.0.16. All datetime values use UTC.

CREATE DATABASE IF NOT EXISTS tiktok_shop_monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE tiktok_shop_monitor;

SET time_zone = '+00:00';

SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';

CREATE TABLE IF NOT EXISTS analytics_product_rollups (
	schema_version INTEGER NOT NULL COMMENT '分片统计结构版本',
	basis VARCHAR(32) NOT NULL COMMENT '当前观察或最新有效历史快照口径',
	shard_id BIGINT UNSIGNED NOT NULL COMMENT '商品主键除以128的分片编号',
	members JSON NOT NULL COMMENT '有界商品贡献；按修订替换以保证幂等',
	summary JSON NOT NULL COMMENT '按市场及报价上下文隔离的精确统计摘要',
	CONSTRAINT pk_analytics_product_rollups PRIMARY KEY (schema_version, basis, shard_id)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='每128个商品一片的增量统计；与商品投影同事务发布';

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

CREATE TABLE IF NOT EXISTS collection_alert_evaluations (
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '来源平台',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场',
	last_observed_at DATETIME(6) COMMENT '最近接受的监控观测时间UTC；防迟到评估覆盖',
	last_evaluated_at DATETIME(6) COMMENT '最近成功持久评估时间UTC',
	sources JSON NOT NULL COMMENT '各来源可用性，避免未知被解释为恢复',
	CONSTRAINT pk_collection_alert_evaluations PRIMARY KEY (platform, region)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='市场级评估串行锁与新鲜度；独立后台运行，不依赖页面访问';

CREATE TABLE IF NOT EXISTS collection_alerts (
	code VARCHAR(64) NOT NULL COMMENT '固定告警规则代码',
	severity VARCHAR(16) NOT NULL COMMENT 'info、warning或critical',
	message VARCHAR(255) NOT NULL COMMENT '固定规则生成的安全描述，不存请求和原始异常',
	status VARCHAR(16) NOT NULL COMMENT 'open未确认，acknowledged已确认，resolved已恢复',
	first_seen_at DATETIME(6) NOT NULL COMMENT '首次观测到条件成立时间UTC',
	opened_at DATETIME(6) NOT NULL COMMENT '本轮告警开始时间UTC',
	last_seen_at DATETIME(6) NOT NULL COMMENT '最近观测到条件成立时间UTC',
	last_evaluated_at DATETIME(6) NOT NULL COMMENT '该规则最近可判断时间UTC；来源未知时不推进',
	condition_known BOOL NOT NULL COMMENT '最新后台评估能否判断该规则；未知不作恢复',
	acknowledged_at DATETIME(6) COMMENT '本轮操作员确认时间UTC；重开清空',
	resolved_at DATETIME(6) COMMENT '本轮条件恢复时间UTC；重开清空',
	occurrences BIGINT UNSIGNED NOT NULL COMMENT '累计告警轮次数；持续成立不累加',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	CONSTRAINT pk_collection_alerts PRIMARY KEY (id),
	CONSTRAINT uq_collection_alert_market_code UNIQUE (platform, region, code),
	CONSTRAINT ck_collection_alerts_status CHECK (status IN ('open','acknowledged','resolved')),
	CONSTRAINT ck_collection_alerts_severity CHECK (severity IN ('info','warning','critical'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='同市场固定规则唯一的持久告警现态；确认不等于故障恢复';

CREATE INDEX ix_collection_alert_status ON collection_alerts (platform, region, status, id);

CREATE TABLE IF NOT EXISTS collection_attempt_facts (
	schema_version INTEGER NOT NULL COMMENT '派生结构版本',
	attempt_id BIGINT UNSIGNED NOT NULL COMMENT '源尝试主键与幂等键；不是完成水位',
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '来源平台',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场',
	task_id VARCHAR(36) NOT NULL COMMENT '任务业务UUID',
	task_type VARCHAR(32) NOT NULL COMMENT '任务类型；来源与本地处理分别统计',
	task_group VARCHAR(8) NOT NULL COMMENT '来源采集source或本地处理local；支持有界失败明细',
	attempt_no INTEGER NOT NULL COMMENT '任务尝试序号',
	worker_id VARCHAR(128) COMMENT '执行实例标识',
	finished_at DATETIME(6) NOT NULL COMMENT '源尝试完成时间UTC',
	succeeded BOOL NOT NULL COMMENT '业务尝试是否成功',
	error_type VARCHAR(64) COMMENT '白名单格式错误分类；不含异常详情',
	response_status INTEGER COMMENT 'HTTP响应状态',
	duration_us BIGINT UNSIGNED NOT NULL COMMENT '完整执行耗时微秒；非HTTP单次延迟',
	processed BOOL NOT NULL COMMENT '是否与分钟计数同事务完成投影' DEFAULT '0',
	CONSTRAINT pk_collection_attempt_facts PRIMARY KEY (schema_version, attempt_id),
	CONSTRAINT ck_collection_attempt_facts_task_group CHECK (task_group IN ('source','local'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='与尝试完成同事务写入的幂等事实；仅保留监控所需安全字段';

CREATE INDEX ix_collection_facts_failures ON collection_attempt_facts (schema_version, platform, region, succeeded, finished_at, attempt_id);

CREATE INDEX ix_collection_facts_group_failures ON collection_attempt_facts (schema_version, platform, region, task_group, succeeded, finished_at, attempt_id);

CREATE INDEX ix_collection_facts_pending ON collection_attempt_facts (schema_version, platform, region, processed, finished_at, attempt_id);

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

CREATE TABLE IF NOT EXISTS collection_minute_rollups (
	minute_at DATETIME(6) NOT NULL COMMENT 'UTC分钟起点，左闭右开',
	task_type VARCHAR(32) NOT NULL COMMENT '任务类型',
	completed BIGINT UNSIGNED NOT NULL COMMENT '完成尝试总数',
	succeeded BIGINT UNSIGNED NOT NULL COMMENT '成功尝试总数',
	duration_us BIGINT UNSIGNED NOT NULL COMMENT '完整执行耗时总和微秒',
	histogram JSON NOT NULL COMMENT '1.1倍几何耗时直方图；P95取桶上界',
	errors JSON NOT NULL COMMENT '安全错误分类计数',
	schema_version INTEGER NOT NULL COMMENT '可重建统计结构版本',
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '来源平台',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场',
	CONSTRAINT pk_collection_minute_rollups PRIMARY KEY (schema_version, platform, region, minute_at, task_type),
	CONSTRAINT ck_collection_minute_rollups_success_count CHECK (succeeded <= completed)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='每市场每任务类型分钟统计；准确计数与耗时和，直方图分位数为显式近似';

CREATE TABLE IF NOT EXISTS collection_monitor_states (
	activated_at DATETIME(6) NOT NULL COMMENT '新写入器已启用后的激活时间UTC',
	coverage_since DATETIME(6) NOT NULL COMMENT '当前回填或修复覆盖起点UTC',
	backfill_until DATETIME(6) NOT NULL COMMENT '当前回填或修复固定终点UTC，不随重启改变',
	cursor_finished_at DATETIME(6) COMMENT '按完成时间排序的历史回填游标',
	cursor_attempt_id BIGINT UNSIGNED COMMENT '相同完成时间的分页辅助键，不是完成水位',
	backfill_complete BOOL NOT NULL COMMENT '固定历史区间是否已完整扫描' DEFAULT '0',
	generation BIGINT UNSIGNED NOT NULL COMMENT '完整统计快照发布代数' DEFAULT '0',
	last_progress_at DATETIME(6) NOT NULL COMMENT '后台最近成功处理时间',
	published_at DATETIME(6) COMMENT '最近完整快照发布时间',
	schema_version INTEGER NOT NULL COMMENT '可重建统计结构版本',
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '来源平台',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场',
	CONSTRAINT pk_collection_monitor_states PRIMARY KEY (schema_version, platform, region)
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='新写入器切换后显式激活的可恢复监控回填进度；缺失代表未激活';

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
	archived_at DATETIME(6) COMMENT '归档时间；归档计划保留历史且禁止新派发，恢复后保持停用',
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

CREATE TABLE IF NOT EXISTS crawl_tasks (
	task_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '任务业务UUID，与数据库内部主键id不同',
	idempotency_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '任务类型、资源、窗口与上下文的SHA-256幂等键',
	task_type VARCHAR(32) NOT NULL COMMENT '任务类型：shop_discovery=店铺发现，shop_profile=店铺档案，product_discovery=商品发现，product_detail=详情，product_reviews=评论，product_snapshot=快照，change_detection=变化检测，report_generation=报表',
	resource_type VARCHAR(32) NOT NULL COMMENT '资源类型：seed=种子，shop=店铺，product=商品，variant=SKU，report=报表',
	resource_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '任务资源标识；同一资源类型使用统一的ID体系',
	payload JSON NOT NULL COMMENT '任务业务参数JSON；不得保存密码或会话凭据',
	checkpoint JSON COMMENT '持久化分页游标或执行进度，用于断点恢复',
	priority INTEGER NOT NULL COMMENT '调度优先级，0-9；值越大优先级越高' DEFAULT '0',
	status VARCHAR(32) NOT NULL COMMENT '任务状态：pending=待执行，running=执行中，retry_waiting=待重试，succeeded=成功，abandoned=租约失效，dead=失败耗尽，cancelled=取消' DEFAULT 'pending',
	attempt_count INTEGER NOT NULL COMMENT '已开始的执行尝试次数，含首次执行' DEFAULT '0',
	max_attempts INTEGER NOT NULL COMMENT '最大总尝试次数，含首次执行；默认3' DEFAULT '3',
	scheduled_for DATETIME(6) NOT NULL COMMENT '原始逻辑调度窗口时间（UTC），重试时保留' DEFAULT CURRENT_TIMESTAMP(6),
	available_at DATETIME(6) NOT NULL COMMENT '允许开始或再次执行的最早时间（UTC）' DEFAULT CURRENT_TIMESTAMP(6),
	started_at DATETIME(6) COMMENT '最近一次执行开始时间（UTC）',
	finished_at DATETIME(6) COMMENT '任务最终结束时间（UTC）',
	lease_expires_at DATETIME(6) COMMENT '当前Worker租约到期时间（UTC）',
	lease_token CHAR(36) CHARACTER SET ascii COLLATE ascii_bin COMMENT '本次执行租约UUID，防止过期Worker覆盖新执行结果',
	worker_id VARCHAR(128) COMMENT '最近执行该任务的Worker实例标识',
	last_error TEXT COMMENT '最近一次执行错误的脱敏描述',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_crawl_tasks PRIMARY KEY (id),
	CONSTRAINT uq_crawl_tasks_task_id UNIQUE (task_id),
	CONSTRAINT uq_crawl_tasks_idempotency UNIQUE (idempotency_key),
	CONSTRAINT ck_crawl_tasks_priority CHECK (priority >= 0 AND priority <= 9),
	CONSTRAINT ck_crawl_tasks_attempt_count CHECK (attempt_count >= 0),
	CONSTRAINT ck_crawl_tasks_max_attempts CHECK (max_attempts >= 1),
	CONSTRAINT ck_crawl_tasks_task_type CHECK (task_type IN ('shop_discovery', 'shop_profile', 'product_discovery', 'product_detail', 'product_reviews', 'product_snapshot', 'change_detection', 'report_generation')),
	CONSTRAINT ck_crawl_tasks_resource_type CHECK (resource_type IN ('seed', 'shop', 'product', 'variant', 'report')),
	CONSTRAINT ck_crawl_tasks_status CHECK (status IN ('pending', 'running', 'retry_waiting', 'succeeded', 'abandoned', 'dead', 'cancelled'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='采集任务调度状态、重试、检查点与Worker租约';

CREATE INDEX ix_crawl_tasks_dispatch ON crawl_tasks (status, available_at, priority);

CREATE INDEX ix_crawl_tasks_lease ON crawl_tasks (status, lease_expires_at);

CREATE INDEX ix_crawl_tasks_resource ON crawl_tasks (platform, region, resource_type, resource_id);

CREATE INDEX ix_crawl_tasks_summary ON crawl_tasks (platform, region, task_type, status, id);

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

CREATE INDEX ix_read_products_recent_scope ON product_read_models (schema_version, basis, platform, region, currency, pricing_context, eligible, observed_at, product_id, shop_id);

CREATE INDEX ix_read_products_scope ON product_read_models (schema_version, basis, platform, region, currency, pricing_context, eligible, shop_id, product_id);

CREATE INDEX ix_read_products_shop ON product_read_models (platform, region, basis, shop_id, product_id);

CREATE INDEX ix_read_products_sku ON product_read_models (platform, region, basis, sku_count, product_id);

CREATE INDEX ix_read_products_stock ON product_read_models (platform, region, basis, stock_status, product_id);

CREATE INDEX ix_read_products_title ON product_read_models (platform, region, basis, title, product_id);

CREATE TABLE IF NOT EXISTS seed_shop_observations (
	category VARCHAR(64) NOT NULL COMMENT '观察到店铺的服装类目代码',
	seed_type VARCHAR(32) NOT NULL COMMENT '种子类型：shop_name=店名，shop_url=店铺链接，shop_id=平台店铺ID' DEFAULT 'shop_name',
	seed_value VARCHAR(255) NOT NULL COMMENT '原始种子值；店名不作为正式店铺唯一身份',
	shop_url TEXT COMMENT '原始观察中的店铺链接；尚未解析时为空',
	source_url TEXT NOT NULL COMMENT '种子来源的公开类目页或发现入口',
	verification_status VARCHAR(64) NOT NULL COMMENT '原始观察的验证依据，例如已在公开类目页观察到' DEFAULT 'observed_public_category_page',
	observed_at DATE NOT NULL COMMENT '原始观察日期',
	row_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '原始种子行的SHA-256摘要，用于幂等导入',
	raw_record JSON COMMENT '原始CSV行的JSON内容，默认延迟加载',
	resolution_status VARCHAR(32) NOT NULL COMMENT '解析状态：pending=待解析，resolved=已确认，ambiguous=多候选，not_found=未找到，failed=失败' DEFAULT 'pending',
	resolution_error TEXT COMMENT '最近一次店铺身份解析失败原因',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_seed_shop_observations PRIMARY KEY (id),
	CONSTRAINT uq_seed_observations_row_hash UNIQUE (row_hash),
	CONSTRAINT uq_seed_observations_scope UNIQUE (id, platform, region),
	CONSTRAINT ck_seed_shop_observations_seed_type CHECK (seed_type IN ('shop_name', 'shop_url', 'shop_id')),
	CONSTRAINT ck_seed_shop_observations_resolution_status CHECK (resolution_status IN ('pending', 'resolved', 'ambiguous', 'not_found', 'failed'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='店铺种子原始观察与身份解析状态';

CREATE INDEX ix_seed_resolution ON seed_shop_observations (region, resolution_status, category);

CREATE TABLE IF NOT EXISTS shops (
	platform_shop_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '平台稳定店铺ID，按字符串保留前导零和大小写',
	canonical_name VARCHAR(255) NOT NULL COMMENT '当前店铺名称；允许改名和不同店铺同名',
	canonical_url TEXT COMMENT '标准化的店铺公开入口链接',
	discovery_category VARCHAR(64) COMMENT '初次发现来源类目，不代表全部主营类目',
	currency VARCHAR(3) NOT NULL COMMENT '店铺价格币种，美国站默认 USD' DEFAULT 'USD',
	rating NUMERIC(3, 2) COMMENT '公开店铺评分，范围 0-5；不可见时为空',
	follower_count BIGINT UNSIGNED COMMENT '公开店铺关注数；未获取时为空',
	product_count BIGINT UNSIGNED COMMENT '平台展示的店铺商品数，区别于本系统已采集数量',
	logo_url TEXT COMMENT '本次观察到的公开店铺头像地址；未获取时为空',
	verification_type VARCHAR(32) COMMENT '来源明确展示的认证类型，例如 blue_v；空不代表未认证',
	identity_label_key VARCHAR(64) COMMENT '来源店铺身份标签代码，例如 official',
	identity_label_text VARCHAR(255) COMMENT '来源店铺身份标签展示文案，例如 OFFICIAL SHOP',
	follower_count_text VARCHAR(128) COMMENT '本次公开粉丝数原始展示文案，保留缩写和加号',
	sales_count BIGINT UNSIGNED COMMENT '本次公开店铺累计已售精确值；不能视为指定时期销量',
	sales_count_text VARCHAR(128) COMMENT '本次公开店铺累计已售原始展示文案',
	global_sales_count BIGINT UNSIGNED COMMENT '本次来源标注的全球累计已售精确值；未获取时为空',
	global_sales_count_text VARCHAR(128) COMMENT '本次来源标注的全球累计已售原始展示文案',
	video_count BIGINT UNSIGNED COMMENT '本次店铺页展示的视频数量；不推断视频归属口径',
	video_count_text VARCHAR(128) COMMENT '本次店铺页展示的视频数原始文案',
	product_count_text VARCHAR(128) COMMENT '平台展示的在售商品数原始文案；与商品数观察时间一致',
	review_count BIGINT UNSIGNED COMMENT '本次公开店铺评价数量；未获取时为空',
	product_count_captured_at DATETIME(6) COMMENT '当前平台商品数的独立实际观察时间（UTC）',
	profile_captured_at DATETIME(6) COMMENT '当前店铺档案实际观察时间（UTC），独立于商品发现时间',
	profile_source_url TEXT COMMENT '当前档案的公开来源链接，移除跟踪与签名参数',
	profile_parser_version VARCHAR(64) COMMENT '当前店铺档案解析器版本',
	profile_response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '当前店铺档案来源响应的SHA-256摘要',
	profile_evidence_path TEXT COMMENT '当前档案白名单业务证据文件路径',
	profile_field_state JSON COMMENT '当前档案各字段状态、来源路径和指标口径',
	profile_observation_status VARCHAR(16) COMMENT '当前档案观察完整度：complete=完整，partial=部分字段缺失或无效',
	status VARCHAR(32) NOT NULL COMMENT '店铺状态：unknown=未知，active=有效，inactive=失效' DEFAULT 'unknown',
	first_seen_at DATETIME(6) NOT NULL COMMENT '首次发现店铺的时间（UTC）' DEFAULT CURRENT_TIMESTAMP(6),
	last_seen_at DATETIME(6) NOT NULL COMMENT '最近成功观察到店铺的时间（UTC，业务更新）' DEFAULT CURRENT_TIMESTAMP(6),
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_shops PRIMARY KEY (id),
	CONSTRAINT uq_shops_identity UNIQUE (platform, region, platform_shop_id),
	CONSTRAINT uq_shops_scope UNIQUE (id, platform, region),
	CONSTRAINT ck_shops_rating CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
	CONSTRAINT ck_shops_profile_observation_status CHECK (profile_observation_status IS NULL OR profile_observation_status IN ('complete', 'partial')),
	CONSTRAINT ck_shops_status CHECK (status IN ('unknown', 'active', 'inactive'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='已确认平台身份的正式店铺与当前指标';

CREATE INDEX ix_shops_market_status ON shops (region, status, discovery_category);

CREATE TABLE IF NOT EXISTS collection_alert_events (
	alert_id BIGINT UNSIGNED NOT NULL COMMENT '所属持久告警主键',
	event_type VARCHAR(16) NOT NULL COMMENT 'opened、acknowledged、resolved或reopened',
	status VARCHAR(16) NOT NULL COMMENT '事件后的告警状态',
	occurred_at DATETIME(6) NOT NULL COMMENT '状态迁移时间UTC',
	occurrence BIGINT UNSIGNED NOT NULL COMMENT '所属告警轮次',
	message VARCHAR(255) NOT NULL COMMENT '当时安全描述；不记录凭据或个人信息',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_collection_alert_events PRIMARY KEY (id),
	CONSTRAINT ck_collection_alert_events_event_type CHECK (event_type IN ('opened','acknowledged','resolved','reopened')),
	CONSTRAINT fk_collection_alert_events_alert_id_collection_alerts FOREIGN KEY(alert_id) REFERENCES collection_alerts (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='只记录告警状态迁移的历史；重复评估不产生重复事件';

CREATE INDEX ix_collection_alert_events_history ON collection_alert_events (alert_id, id);

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

CREATE TABLE IF NOT EXISTS crawl_attempts (
	task_id BIGINT UNSIGNED NOT NULL COMMENT '任务内部主键，关联 crawl_tasks.id；不是任务业务UUID',
	attempt_no INTEGER NOT NULL COMMENT '该任务的尝试序号，从1开始',
	worker_id VARCHAR(128) COMMENT '执行该次尝试的Worker实例标识',
	request_url TEXT COMMENT '脱敏后的请求URL，去除凭据与敏感签名',
	request_method VARCHAR(16) NOT NULL COMMENT 'HTTP请求方法，默认 GET' DEFAULT 'GET',
	response_status INTEGER COMMENT 'HTTP响应状态码，100-599；无响应时为空',
	response_bytes BIGINT UNSIGNED COMMENT '响应正文大小，单位字节',
	latency_ms INTEGER COMMENT '请求或执行耗时，单位毫秒',
	succeeded BOOL COMMENT '该次业务处理是否成功：1=成功，0=失败，空=未完成',
	error_type VARCHAR(64) COMMENT '可分类错误类型，例如超时、限流、解析失败',
	error_message TEXT COMMENT '该次执行错误的脱敏描述',
	evidence_path TEXT COMMENT '脱敏响应或排查证据文件路径',
	response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '响应正文的SHA-256摘要',
	started_at DATETIME(6) NOT NULL COMMENT '本次执行尝试开始时间（UTC）' DEFAULT CURRENT_TIMESTAMP(6),
	finished_at DATETIME(6) COMMENT '本次执行尝试结束时间（UTC）',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_crawl_attempts PRIMARY KEY (id),
	CONSTRAINT uq_crawl_attempts_number UNIQUE (task_id, attempt_no),
	CONSTRAINT ck_crawl_attempts_attempt_no CHECK (attempt_no >= 1),
	CONSTRAINT ck_crawl_attempts_response_status CHECK (response_status IS NULL OR (response_status >= 100 AND response_status <= 599)),
	CONSTRAINT ck_crawl_attempts_latency_ms CHECK (latency_ms IS NULL OR latency_ms >= 0),
	CONSTRAINT fk_crawl_attempts_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='采集任务每次执行尝试的请求结果与失败证据';

CREATE INDEX ix_crawl_attempts_finished ON crawl_attempts (finished_at, error_type);

CREATE INDEX ix_crawl_attempts_monitor ON crawl_attempts (finished_at, id, task_id);

CREATE TABLE IF NOT EXISTS products (
	platform_product_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '平台稳定商品ID，按字符串保存',
	shop_id BIGINT UNSIGNED NOT NULL COMMENT '所属店铺内部主键，关联 shops.id',
	product_url TEXT NOT NULL COMMENT '商品公开详情入口链接',
	title VARCHAR(512) COMMENT '商品标题；发现阶段可暂缺，详情解析应校验',
	description TEXT COMMENT '商品原始描述，默认延迟加载',
	original_category TEXT COMMENT '平台原始类目路径',
	normalized_category VARCHAR(64) COMMENT '清洗后的服装标准类目代码',
	category_rule_version VARCHAR(32) COMMENT '类目清洗规则版本',
	brand VARCHAR(255) COMMENT '公开商品品牌',
	gender VARCHAR(32) COMMENT '适用性别或人群标签',
	season VARCHAR(64) COMMENT '适用季节标签',
	style VARCHAR(128) COMMENT '服装风格标签，例如休闲、运动',
	fit VARCHAR(64) COMMENT '服装版型，例如修身、宽松',
	garment_length VARCHAR(64) COMMENT '衣长、裤长或裙长属性',
	material TEXT COMMENT '面料与材质描述',
	pattern VARCHAR(128) COMMENT '图案或纹理标签',
	color_summary JSON COMMENT '商品可见颜色标签数组',
	size_summary JSON COMMENT '商品可见尺码标签数组，保留原始体系',
	raw_attributes JSON COMMENT '原始服装属性JSON，便于复核清洗结果',
	stock_status VARCHAR(32) NOT NULL COMMENT '库存状态：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分规格有货' DEFAULT 'unknown',
	availability_status VARCHAR(32) NOT NULL COMMENT '可用状态：unknown=未知，available=可访问，unavailable=暂不可用，removed=确认下架' DEFAULT 'unknown',
	sales_count BIGINT UNSIGNED COMMENT '可确认的精确公开销量；缺失或仅有区间时为空',
	sales_count_text VARCHAR(128) COMMENT '公开销量原始文案，例如 1K+ sold',
	rating NUMERIC(3, 2) COMMENT '公开商品评分，范围 0-5；未获取时为空',
	review_count BIGINT UNSIGNED COMMENT '公开评价数量；未获取时为空',
	first_seen_at DATETIME(6) NOT NULL COMMENT '首次发现商品的时间（UTC）' DEFAULT CURRENT_TIMESTAMP(6),
	last_seen_at DATETIME(6) NOT NULL COMMENT '最近成功观察到商品的时间（UTC，业务更新）' DEFAULT CURRENT_TIMESTAMP(6),
	last_success_at DATETIME(6) COMMENT '最近有效详情采集完成时间（UTC）',
	next_capture_at DATETIME(6) COMMENT '下一次计划采集时间（UTC）',
	content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '稳定业务内容SHA-256摘要，排除采集时间与随机字段',
	parser_version VARCHAR(32) COMMENT '当前商品详情解析器版本',
	data_version INTEGER NOT NULL COMMENT '业务数据版本，默认1；内容变更时由服务层增加' DEFAULT '1',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	currency VARCHAR(3) NOT NULL COMMENT '价格币种代码，美国站默认 USD' DEFAULT 'USD',
	pricing_context VARCHAR(64) NOT NULL COMMENT '价格观察上下文，public_listed 表示公开标价' DEFAULT 'public_listed',
	price_text VARCHAR(255) COMMENT '页面原始价格文案，保留范围与展示格式',
	price_min NUMERIC(12, 2) COMMENT '商品当前价格下限；未获取时为空',
	price_max NUMERIC(12, 2) COMMENT '商品当前价格上限；单价商品与下限相同',
	original_price_min NUMERIC(12, 2) COMMENT '页面展示的原价下限；未展示时为空',
	original_price_max NUMERIC(12, 2) COMMENT '页面展示的原价上限；未展示时为空',
	discount_rate NUMERIC(7, 4) COMMENT '已确认的优惠比例，范围 0-1；0.2 表示优惠两成',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_products PRIMARY KEY (id),
	CONSTRAINT uq_products_identity UNIQUE (platform, region, platform_product_id),
	CONSTRAINT fk_products_shop_scope FOREIGN KEY(shop_id, platform, region) REFERENCES shops (id, platform, region) ON DELETE RESTRICT,
	CONSTRAINT ck_products_price_min CHECK (price_min IS NULL OR price_min >= 0),
	CONSTRAINT ck_products_price_max CHECK (price_max IS NULL OR price_max >= 0),
	CONSTRAINT ck_products_price_range CHECK (price_min <= price_max),
	CONSTRAINT ck_products_original_min CHECK (original_price_min IS NULL OR original_price_min >= 0),
	CONSTRAINT ck_products_original_max CHECK (original_price_max IS NULL OR original_price_max >= 0),
	CONSTRAINT ck_products_original_range CHECK (original_price_min <= original_price_max),
	CONSTRAINT ck_products_discount_rate CHECK (discount_rate IS NULL OR (discount_rate >= 0 AND discount_rate <= 1)),
	CONSTRAINT ck_products_data_version CHECK (data_version >= 1),
	CONSTRAINT ck_products_rating CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
	CONSTRAINT ck_products_stock_status CHECK (stock_status IN ('unknown', 'in_stock', 'out_of_stock', 'partial')),
	CONSTRAINT ck_products_availability_status CHECK (availability_status IN ('unknown', 'available', 'unavailable', 'removed'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品当前详情、服装属性、价格区间与采集状态';

CREATE INDEX ix_products_category_price ON products (region, normalized_category, price_min);

CREATE INDEX ix_products_next_capture ON products (availability_status, next_capture_at);

CREATE INDEX ix_products_recent_detail ON products (platform, region, last_success_at, id, title);

CREATE INDEX ix_products_shop_status ON products (shop_id, availability_status);

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

CREATE TABLE IF NOT EXISTS shop_seed_links (
	seed_id BIGINT UNSIGNED NOT NULL COMMENT '种子观察内部主键，关联 seed_shop_observations.id',
	shop_id BIGINT UNSIGNED NOT NULL COMMENT '正式店铺内部主键，关联 shops.id',
	resolved_at DATETIME(6) NOT NULL COMMENT '店铺身份确认时间（UTC）',
	resolution_method VARCHAR(64) NOT NULL COMMENT '身份确认方式，例如 source_id 或 manual',
	evidence_url TEXT COMMENT '用于确认店铺身份的公开证据链接',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	platform VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '数据来源平台，默认 tiktok_shop' DEFAULT 'tiktok_shop',
	region VARCHAR(2) COLLATE utf8mb4_bin NOT NULL COMMENT '市场区域代码，例如 US；统一使用大写',
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_shop_seed_links PRIMARY KEY (id),
	CONSTRAINT uq_shop_seed_links_seed UNIQUE (seed_id),
	CONSTRAINT fk_shop_seed_links_seed FOREIGN KEY(seed_id, platform, region) REFERENCES seed_shop_observations (id, platform, region) ON DELETE RESTRICT,
	CONSTRAINT fk_shop_seed_links_shop FOREIGN KEY(shop_id, platform, region) REFERENCES shops (id, platform, region) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='种子观察与正式店铺的确认映射';

CREATE INDEX ix_shop_seed_links_shop ON shop_seed_links (shop_id, platform, region);

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

CREATE TABLE IF NOT EXISTS product_contents (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '商品内部主键，每个商品保留一份最新介绍',
	description_text MEDIUMTEXT COMMENT '商家介绍纯文本投影，不含评论与推断内容',
	sections JSON NOT NULL COMMENT '商品参数与介绍的有序安全节点JSON',
	completeness VARCHAR(16) NOT NULL COMMENT '内容覆盖：missing缺失、empty显式空、partial部分、complete完整',
	unsupported_blocks JSON NOT NULL COMMENT '未支持节点或缺失结构的原因代码数组',
	source_shape JSON NOT NULL COMMENT '白名单来源字段存在性、节点类型计数与章节覆盖',
	source_version VARCHAR(64) NOT NULL COMMENT '介绍解析版本，独立于商品与图片解析版本',
	content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '介绍节点、文本与覆盖状态的SHA256摘要',
	content_version INTEGER NOT NULL COMMENT '独立介绍版本，仅内容摘要变化时增加',
	source_url TEXT NOT NULL COMMENT '实际观察介绍的公开商品详情链接',
	response_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '本次公开详情响应的SHA256证据摘要',
	observed_at DATETIME(6) NOT NULL COMMENT '当前介绍最近成功观察时间（UTC）',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	CONSTRAINT pk_product_contents PRIMARY KEY (id),
	CONSTRAINT uq_product_contents_product UNIQUE (product_id),
	CONSTRAINT ck_product_contents_completeness CHECK (completeness IN ('missing', 'empty', 'partial', 'complete')),
	CONSTRAINT ck_product_contents_content_version CHECK (content_version >= 1),
	CONSTRAINT fk_product_contents_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品公开介绍的安全有序节点、参数与独立采集证据';

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

CREATE TABLE IF NOT EXISTS product_snapshots (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '快照所属商品内部主键，关联 products.id',
	task_id BIGINT UNSIGNED COMMENT '采集任务内部主键，关联 crawl_tasks.id；手动采集可空',
	scheduled_for DATETIME(6) NOT NULL COMMENT '逻辑采集窗口时间（UTC）；同一任务重试保持不变',
	captured_at DATETIME(6) NOT NULL COMMENT '本次实际观察时间（UTC）',
	stock_status VARCHAR(32) NOT NULL COMMENT '本次库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分规格有货' DEFAULT 'unknown',
	availability_status VARCHAR(32) NOT NULL COMMENT '本次可用状态：unknown=未知，available=可访问，unavailable=暂不可用，removed=确认下架' DEFAULT 'unknown',
	sales_count BIGINT UNSIGNED COMMENT '本次可确认的精确公开销量；未获取时为空',
	sales_count_text VARCHAR(128) COMMENT '本次公开销量的原始展示文案',
	rating NUMERIC(3, 2) COMMENT '本次公开商品评分，范围 0-5；未获取时为空',
	review_count BIGINT UNSIGNED COMMENT '本次公开评价数量；未获取时为空',
	variant_count INTEGER COMMENT '本次成功解析的SKU规格数量',
	content_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '本次稳定商品内容的SHA-256摘要',
	parser_version VARCHAR(32) NOT NULL COMMENT '生成该快照的解析器版本',
	source_status INTEGER COMMENT '来源响应HTTP状态码；业务有效性另行校验',
	source_url TEXT COMMENT '本次快照数据的公开来源链接',
	raw_payload JSON COMMENT '脱敏后的来源JSON，便于复现与解析排查；默认延迟加载',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	currency VARCHAR(3) NOT NULL COMMENT '价格币种代码，美国站默认 USD' DEFAULT 'USD',
	pricing_context VARCHAR(64) NOT NULL COMMENT '价格观察上下文，public_listed 表示公开标价' DEFAULT 'public_listed',
	price_text VARCHAR(255) COMMENT '页面原始价格文案，保留范围与展示格式',
	price_min NUMERIC(12, 2) COMMENT '商品当前价格下限；未获取时为空',
	price_max NUMERIC(12, 2) COMMENT '商品当前价格上限；单价商品与下限相同',
	original_price_min NUMERIC(12, 2) COMMENT '页面展示的原价下限；未展示时为空',
	original_price_max NUMERIC(12, 2) COMMENT '页面展示的原价上限；未展示时为空',
	discount_rate NUMERIC(7, 4) COMMENT '已确认的优惠比例，范围 0-1；0.2 表示优惠两成',
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_product_snapshots PRIMARY KEY (id),
	CONSTRAINT uq_product_snapshots_window UNIQUE (product_id, scheduled_for, pricing_context),
	CONSTRAINT uq_product_snapshots_scope UNIQUE (id, product_id),
	CONSTRAINT ck_product_snapshots_price_min CHECK (price_min IS NULL OR price_min >= 0),
	CONSTRAINT ck_product_snapshots_price_max CHECK (price_max IS NULL OR price_max >= 0),
	CONSTRAINT ck_product_snapshots_price_range CHECK (price_min <= price_max),
	CONSTRAINT ck_product_snapshots_original_min CHECK (original_price_min IS NULL OR original_price_min >= 0),
	CONSTRAINT ck_product_snapshots_original_max CHECK (original_price_max IS NULL OR original_price_max >= 0),
	CONSTRAINT ck_product_snapshots_original_range CHECK (original_price_min <= original_price_max),
	CONSTRAINT ck_product_snapshots_discount_rate CHECK (discount_rate IS NULL OR (discount_rate >= 0 AND discount_rate <= 1)),
	CONSTRAINT ck_product_snapshots_rating CHECK (rating IS NULL OR (rating >= 0 AND rating <= 5)),
	CONSTRAINT ck_product_snapshots_source_status CHECK (source_status IS NULL OR (source_status >= 100 AND source_status <= 599)),
	CONSTRAINT ck_product_snapshots_variant_count CHECK (variant_count IS NULL OR variant_count >= 0),
	CONSTRAINT ck_product_snapshots_stock_status CHECK (stock_status IN ('unknown', 'in_stock', 'out_of_stock', 'partial')),
	CONSTRAINT ck_product_snapshots_availability_status CHECK (availability_status IN ('unknown', 'available', 'unavailable', 'removed')),
	CONSTRAINT fk_product_snapshots_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT,
	CONSTRAINT fk_product_snapshots_task_id_crawl_tasks FOREIGN KEY(task_id) REFERENCES crawl_tasks (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='按逻辑采集窗口保存的商品价格与指标历史快照';

CREATE INDEX ix_product_snapshots_captured ON product_snapshots (captured_at);

CREATE INDEX ix_product_snapshots_history ON product_snapshots (product_id, captured_at);

CREATE TABLE IF NOT EXISTS product_variants (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '所属商品内部主键，关联 products.id',
	variant_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '规格身份SHA-256键，由平台SKU ID或稳定规格组合生成',
	platform_variant_id VARCHAR(128) COLLATE utf8mb4_bin COMMENT '平台原始SKU ID；未提供时为空',
	color VARCHAR(128) COMMENT '该SKU的颜色标签',
	size VARCHAR(64) COMMENT '该SKU的尺码标签，保留原始体系',
	material VARCHAR(255) COMMENT '该SKU的面料或材质',
	options JSON COMMENT '完整规格键值组合JSON',
	currency VARCHAR(3) NOT NULL COMMENT 'SKU价格币种，美国站默认 USD' DEFAULT 'USD',
	price NUMERIC(12, 2) COMMENT '该SKU的当前明确单价；未获取时为空',
	original_price NUMERIC(12, 2) COMMENT '该SKU页面展示的原价；未展示时为空',
	stock_status VARCHAR(32) NOT NULL COMMENT 'SKU库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分可用' DEFAULT 'unknown',
	available_quantity BIGINT UNSIGNED COMMENT '平台可见可售数量；未公开时为空',
	variant_payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '稳定SKU内容的SHA-256摘要',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	updated_at DATETIME(6) NOT NULL COMMENT '记录最后修改时间（UTC，数据库自动更新）' DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_product_variants PRIMARY KEY (id),
	CONSTRAINT uq_variants_identity UNIQUE (product_id, variant_key),
	CONSTRAINT uq_variants_platform_id UNIQUE (product_id, platform_variant_id),
	CONSTRAINT uq_variants_product_scope UNIQUE (id, product_id),
	CONSTRAINT ck_product_variants_price CHECK (price IS NULL OR price >= 0),
	CONSTRAINT ck_product_variants_original_price CHECK (original_price IS NULL OR original_price >= 0),
	CONSTRAINT ck_product_variants_stock_status CHECK (stock_status IN ('unknown', 'in_stock', 'out_of_stock', 'partial')),
	CONSTRAINT fk_product_variants_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品颜色、尺码等SKU规格的当前价格与库存';

CREATE TABLE IF NOT EXISTS change_events (
	product_id BIGINT UNSIGNED NOT NULL COMMENT '发生变化的商品内部主键，关联 products.id',
	variant_id BIGINT UNSIGNED COMMENT '发生变化的SKU内部主键；商品级事件可空',
	snapshot_id BIGINT UNSIGNED NOT NULL COMMENT '变化后的商品快照主键，关联 product_snapshots.id',
	previous_snapshot_id BIGINT UNSIGNED COMMENT '变化前的商品快照主键；首次发现商品时为空',
	event_type VARCHAR(32) NOT NULL COMMENT '事件：product_new=新发现，price_changed=价格变化，discount_started/ended=折扣始末，stockout/restocked=缺补货，variant_changed/content_changed=规格或内容变化，product_removed=确认下架',
	old_value JSON COMMENT '变化前的业务值JSON；金额用字符串保存',
	new_value JSON COMMENT '变化后的业务值JSON；金额用字符串保存',
	detected_at DATETIME(6) NOT NULL COMMENT '检测到该变化的时间（UTC）' DEFAULT CURRENT_TIMESTAMP(6),
	event_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '变化事件SHA-256幂等键，避免同一对快照重复生成事件',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_change_events PRIMARY KEY (id),
	CONSTRAINT uq_change_events_hash UNIQUE (event_hash),
	CONSTRAINT fk_change_events_snapshot FOREIGN KEY(snapshot_id, product_id) REFERENCES product_snapshots (id, product_id) ON DELETE RESTRICT,
	CONSTRAINT fk_change_events_previous FOREIGN KEY(previous_snapshot_id, product_id) REFERENCES product_snapshots (id, product_id) ON DELETE RESTRICT,
	CONSTRAINT fk_change_events_variant FOREIGN KEY(variant_id, product_id) REFERENCES product_variants (id, product_id) ON DELETE RESTRICT,
	CONSTRAINT ck_change_events_event_type CHECK (event_type IN ('product_new', 'price_changed', 'discount_started', 'discount_ended', 'stockout', 'restocked', 'variant_changed', 'content_changed', 'product_removed')),
	CONSTRAINT fk_change_events_product_id_products FOREIGN KEY(product_id) REFERENCES products (id) ON DELETE RESTRICT
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='商品与SKU变化事件及前后快照证据';

CREATE INDEX ix_change_events_product_time ON change_events (product_id, detected_at);

CREATE INDEX ix_change_events_type_time ON change_events (event_type, detected_at);

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

CREATE TABLE IF NOT EXISTS variant_snapshots (
	product_snapshot_id BIGINT UNSIGNED NOT NULL COMMENT '本次商品快照内部主键，关联 product_snapshots.id',
	product_id BIGINT UNSIGNED NOT NULL COMMENT '所属商品内部主键，用于校验SKU与商品快照归属一致',
	variant_id BIGINT UNSIGNED NOT NULL COMMENT 'SKU内部主键，关联 product_variants.id',
	captured_at DATETIME(6) NOT NULL COMMENT '该SKU本次实际观察时间（UTC）',
	currency VARCHAR(3) NOT NULL COMMENT '该次SKU价格币种，美国站默认 USD' DEFAULT 'USD',
	price NUMERIC(12, 2) COMMENT '该SKU本次明确单价；未获取时为空',
	original_price NUMERIC(12, 2) COMMENT '该SKU本次展示原价；未展示时为空',
	stock_status VARCHAR(32) NOT NULL COMMENT '本次SKU库存：unknown=未知，in_stock=有货，out_of_stock=缺货，partial=部分可用' DEFAULT 'unknown',
	available_quantity BIGINT UNSIGNED COMMENT '该SKU本次公开可售数量；未公开时为空',
	variant_payload_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin COMMENT '该SKU本次稳定内容的SHA-256摘要',
	id BIGINT UNSIGNED NOT NULL COMMENT '数据库内部自增主键' AUTO_INCREMENT,
	created_at DATETIME(6) NOT NULL COMMENT '记录创建时间（UTC，微秒精度）' DEFAULT CURRENT_TIMESTAMP(6),
	CONSTRAINT pk_variant_snapshots PRIMARY KEY (id),
	CONSTRAINT uq_variant_snapshots_capture UNIQUE (product_snapshot_id, variant_id),
	CONSTRAINT fk_variant_snapshots_capture FOREIGN KEY(product_snapshot_id, product_id) REFERENCES product_snapshots (id, product_id) ON DELETE RESTRICT,
	CONSTRAINT fk_variant_snapshots_variant FOREIGN KEY(variant_id, product_id) REFERENCES product_variants (id, product_id) ON DELETE RESTRICT,
	CONSTRAINT ck_variant_snapshots_price CHECK (price IS NULL OR price >= 0),
	CONSTRAINT ck_variant_snapshots_original_price CHECK (original_price IS NULL OR original_price >= 0),
	CONSTRAINT ck_variant_snapshots_stock_status CHECK (stock_status IN ('unknown', 'in_stock', 'out_of_stock', 'partial'))
) ENGINE=InnoDB CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='同一次商品采集下各SKU的价格与库存历史快照';

CREATE INDEX ix_variant_snapshots_history ON variant_snapshots (variant_id, captured_at);

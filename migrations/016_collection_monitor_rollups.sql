-- Rebuildable collection monitor facts and minute rollups. Does not activate backfill.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

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

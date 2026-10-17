-- Persistent collection console alerts. No external notification integration.
SET NAMES utf8mb4;
SET time_zone = '+00:00';

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

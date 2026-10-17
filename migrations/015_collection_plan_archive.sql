-- Reversible plan archival. Apply with scripts.migrate_collection_plan_archive.
-- Existing plans remain unarchived; no business rows are rewritten.
ALTER TABLE collection_plans ADD COLUMN archived_at DATETIME(6) COMMENT '归档时间；归档计划保留历史且禁止新派发，恢复后保持停用', ALGORITHM=INSTANT;

-- Ordered recent attempts for bounded collection monitoring.
ALTER TABLE `crawl_attempts` ADD INDEX `ix_crawl_attempts_monitor` (`finished_at`, `id`, `task_id`), ALGORITHM=INPLACE, LOCK=NONE;

-- Migration: 022_add_today_plan_and_todo_urgency.sql
-- Today's Plan daily arrangement layer + independent is_urgent flag.
-- Idempotent: skip ALTER/CREATE when objects already exist.

USE growth_log;

-- 1) todos.is_urgent
SET @urgent_col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND COLUMN_NAME = 'is_urgent'
);

SET @urgent_col_ddl := IF(
  @urgent_col_exists = 0,
  'ALTER TABLE `todos` ADD COLUMN `is_urgent` TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''独立紧急标记，与 P0-P4 无关'' AFTER `is_done`',
  'SELECT ''todos.is_urgent already exists'' AS migration_022_column_noop'
);

PREPARE stmt_022_col FROM @urgent_col_ddl;
EXECUTE stmt_022_col;
DEALLOCATE PREPARE stmt_022_col;

UPDATE `todos`
SET `is_urgent` = 0
WHERE `is_urgent` IS NULL;

-- 2) sort/filter index for unfinished urgent work
SET @urgent_idx_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND INDEX_NAME = 'idx_todos_user_done_urgent_due'
);

SET @urgent_idx_ddl := IF(
  @urgent_idx_exists = 0,
  'ALTER TABLE `todos` ADD INDEX `idx_todos_user_done_urgent_due` (`user_id`, `is_done`, `is_urgent`, `due_date`)',
  'SELECT ''idx_todos_user_done_urgent_due already exists'' AS migration_022_index_noop'
);

PREPARE stmt_022_idx FROM @urgent_idx_ddl;
EXECUTE stmt_022_idx;
DEALLOCATE PREPARE stmt_022_idx;

-- 3) daily plan items (reference layer; no Todo content snapshot)
SET @plan_table_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.TABLES
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todo_daily_plan_items'
);

SET @plan_table_ddl := IF(
  @plan_table_exists = 0,
  'CREATE TABLE `todo_daily_plan_items` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `user_id` BIGINT UNSIGNED NOT NULL,
    `todo_id` BIGINT UNSIGNED NOT NULL,
    `plan_date` DATE NOT NULL,
    `source` VARCHAR(16) NOT NULL DEFAULT ''manual'' COMMENT ''manual|rollover'',
    `status` VARCHAR(16) NOT NULL DEFAULT ''active'' COMMENT ''active|completed|carried|returned'',
    `carried_from_id` BIGINT UNSIGNED NULL DEFAULT NULL,
    `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uq_todo_daily_plan_user_todo_date` (`user_id`, `todo_id`, `plan_date`),
    KEY `idx_todo_daily_plan_user_date_status` (`user_id`, `plan_date`, `status`),
    KEY `idx_todo_daily_plan_todo` (`todo_id`),
    KEY `idx_todo_daily_plan_carried_from` (`carried_from_id`),
    CONSTRAINT `fk_todo_daily_plan_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_todo_daily_plan_todo` FOREIGN KEY (`todo_id`) REFERENCES `todos` (`id`) ON DELETE CASCADE,
    CONSTRAINT `fk_todo_daily_plan_carried_from` FOREIGN KEY (`carried_from_id`) REFERENCES `todo_daily_plan_items` (`id`) ON DELETE SET NULL
  ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci',
  'SELECT ''todo_daily_plan_items already exists'' AS migration_022_table_noop'
);

PREPARE stmt_022_table FROM @plan_table_ddl;
EXECUTE stmt_022_table;
DEALLOCATE PREPARE stmt_022_table;

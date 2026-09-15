-- Migration: 020_add_priority_to_todos.sql
-- 说明：为 todos 增加 P0-P4 优先级；历史和异常值统一回填 P4。
-- 幂等：列和索引已存在时跳过 ALTER。

USE growth_log;

SET @priority_col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND COLUMN_NAME = 'priority'
);

SET @priority_col_ddl := IF(
  @priority_col_exists = 0,
  'ALTER TABLE `todos` ADD COLUMN `priority` VARCHAR(2) NOT NULL DEFAULT ''P4'' COMMENT ''优先级 P0-P4'' AFTER `content`',
  'SELECT ''todos.priority already exists'' AS migration_020_column_noop'
);

PREPARE stmt_020_col FROM @priority_col_ddl;
EXECUTE stmt_020_col;
DEALLOCATE PREPARE stmt_020_col;

UPDATE `todos`
SET `priority` = 'P4'
WHERE `priority` IS NULL OR `priority` NOT IN ('P0', 'P1', 'P2', 'P3', 'P4');

SET @priority_idx_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND INDEX_NAME = 'idx_todos_user_priority'
);

SET @priority_idx_ddl := IF(
  @priority_idx_exists = 0,
  'ALTER TABLE `todos` ADD INDEX `idx_todos_user_priority` (`user_id`, `priority`)',
  'SELECT ''idx_todos_user_priority already exists'' AS migration_020_index_noop'
);

PREPARE stmt_020_idx FROM @priority_idx_ddl;
EXECUTE stmt_020_idx;
DEALLOCATE PREPARE stmt_020_idx;

-- Migration: 024_add_todos_completion_note.sql
-- Optional note captured when marking a todo done (收获/心得).
-- Idempotent: skip ALTER when column already exists.

USE growth_log;

SET @col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND COLUMN_NAME = 'completion_note'
);

SET @col_ddl := IF(
  @col_exists = 0,
  'ALTER TABLE `todos` ADD COLUMN `completion_note` VARCHAR(1000) NULL DEFAULT NULL COMMENT ''完成时可选批注（收获/心得）'' AFTER `completed_at`',
  'SELECT ''todos.completion_note already exists'' AS migration_024_column_noop'
);

PREPARE stmt_024_col FROM @col_ddl;
EXECUTE stmt_024_col;
DEALLOCATE PREPARE stmt_024_col;

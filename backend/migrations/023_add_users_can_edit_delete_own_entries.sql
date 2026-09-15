-- Migration: 023_add_users_can_edit_delete_own_entries.sql
-- User-level permission: allow edit/delete of own original entries.
-- Idempotent: skip ALTER when column already exists.
-- Does NOT hardcode any username or grant privilege to existing users.

USE growth_log;

SET @col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'can_edit_delete_own_entries'
);

SET @col_ddl := IF(
  @col_exists = 0,
  'ALTER TABLE `users` ADD COLUMN `can_edit_delete_own_entries` TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''允许删改自己的原记录'' AFTER `is_admin`',
  'SELECT ''users.can_edit_delete_own_entries already exists'' AS migration_023_column_noop'
);

PREPARE stmt_023_col FROM @col_ddl;
EXECUTE stmt_023_col;
DEALLOCATE PREPARE stmt_023_col;

UPDATE `users`
SET `can_edit_delete_own_entries` = 0
WHERE `can_edit_delete_own_entries` IS NULL;

-- Migration: 019_add_users_is_admin.sql
-- 说明：为 users 表增加通用 is_admin 角色字段（默认 false）。
-- 幂等：列已存在时跳过 ALTER。
-- 禁止：写死用户名、自动授予任何账号管理员。

USE growth_log;

SET @col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'users'
    AND COLUMN_NAME = 'is_admin'
);

SET @ddl := IF(
  @col_exists = 0,
  'ALTER TABLE `users` ADD COLUMN `is_admin` TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''管理员标志，0=普通用户'' AFTER `is_active`',
  'SELECT ''users.is_admin already exists'' AS migration_019_noop'
);

PREPARE stmt_019 FROM @ddl;
EXECUTE stmt_019;
DEALLOCATE PREPARE stmt_019;

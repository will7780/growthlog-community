-- R6 follow-up: ensure confirm_key exists on ai_derived_contents (idempotent)
-- Needed when table was created by an older schema without confirm_key.

SET @col_exists := (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_derived_contents'
    AND COLUMN_NAME = 'confirm_key'
);
SET @sql := IF(
  @col_exists = 0,
  'ALTER TABLE `ai_derived_contents` ADD COLUMN `confirm_key` varchar(64) DEFAULT NULL COMMENT ''幂等键 preview_id+item_id'' AFTER `status`',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @uk_exists := (
  SELECT COUNT(*) FROM information_schema.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_derived_contents'
    AND INDEX_NAME = 'uk_derived_user_confirm_key'
);
SET @sql := IF(
  @uk_exists = 0,
  'ALTER TABLE `ai_derived_contents` ADD UNIQUE KEY `uk_derived_user_confirm_key` (`user_id`, `confirm_key`)',
  'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Migration: 008_upgrade_entry_attachments_for_rag.sql
-- 说明：兼容旧版 entry_attachments 表，补齐多模态附件检索所需字段
-- 适用场景：线上库已经执行过早期 002_create_entry_attachments.sql，CREATE TABLE IF NOT EXISTS 不会自动补字段

USE growth_log;

DELIMITER //

DROP PROCEDURE IF EXISTS add_column_if_missing //
CREATE PROCEDURE add_column_if_missing(
  IN table_name_param varchar(64),
  IN column_name_param varchar(64),
  IN alter_sql text
)
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = table_name_param
      AND COLUMN_NAME = column_name_param
  ) THEN
    SET @ddl = alter_sql;
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END //

DROP PROCEDURE IF EXISTS add_index_if_missing //
CREATE PROCEDURE add_index_if_missing(
  IN table_name_param varchar(64),
  IN index_name_param varchar(64),
  IN alter_sql text
)
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = table_name_param
      AND INDEX_NAME = index_name_param
  ) THEN
    SET @ddl = alter_sql;
    PREPARE stmt FROM @ddl;
    EXECUTE stmt;
    DEALLOCATE PREPARE stmt;
  END IF;
END //

DELIMITER ;

CALL add_column_if_missing('entry_attachments', 'user_id', 'ALTER TABLE entry_attachments ADD COLUMN user_id bigint unsigned NULL AFTER entry_id');
CALL add_column_if_missing('entry_attachments', 'original_filename', 'ALTER TABLE entry_attachments ADD COLUMN original_filename varchar(255) NULL AFTER user_id');
CALL add_column_if_missing('entry_attachments', 'storage_filename', 'ALTER TABLE entry_attachments ADD COLUMN storage_filename varchar(255) NULL AFTER original_filename');
CALL add_column_if_missing('entry_attachments', 'file_ext', 'ALTER TABLE entry_attachments ADD COLUMN file_ext varchar(20) NULL AFTER mime_type');
CALL add_column_if_missing('entry_attachments', 'content_hash', 'ALTER TABLE entry_attachments ADD COLUMN content_hash char(64) NULL AFTER file_size');
CALL add_column_if_missing('entry_attachments', 'storage_path', 'ALTER TABLE entry_attachments ADD COLUMN storage_path varchar(500) NULL AFTER content_hash');
CALL add_column_if_missing('entry_attachments', 'status', 'ALTER TABLE entry_attachments ADD COLUMN status enum(''uploaded'',''processing'',''indexed'',''failed'') NOT NULL DEFAULT ''uploaded'' AFTER storage_path');
CALL add_column_if_missing('entry_attachments', 'page_count', 'ALTER TABLE entry_attachments ADD COLUMN page_count int DEFAULT NULL AFTER status');
CALL add_column_if_missing('entry_attachments', 'slide_count', 'ALTER TABLE entry_attachments ADD COLUMN slide_count int DEFAULT NULL AFTER page_count');
CALL add_column_if_missing('entry_attachments', 'preview_path', 'ALTER TABLE entry_attachments ADD COLUMN preview_path varchar(500) DEFAULT NULL AFTER slide_count');
CALL add_column_if_missing('entry_attachments', 'error_message', 'ALTER TABLE entry_attachments ADD COLUMN error_message text DEFAULT NULL AFTER preview_path');
CALL add_column_if_missing('entry_attachments', 'processed_at', 'ALTER TABLE entry_attachments ADD COLUMN processed_at timestamp NULL DEFAULT NULL AFTER error_message');
CALL add_column_if_missing('entry_attachments', 'updated_at', 'ALTER TABLE entry_attachments ADD COLUMN updated_at timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP AFTER created_at');

DROP PROCEDURE IF EXISTS add_column_if_missing;

-- 回填 user_id：从 entries 表继承记录所属用户
UPDATE entry_attachments ea
JOIN entries e ON e.id = ea.entry_id
SET ea.user_id = e.user_id
WHERE ea.user_id IS NULL;

DELIMITER //

DROP PROCEDURE IF EXISTS backfill_legacy_attachment_columns //
CREATE PROCEDURE backfill_legacy_attachment_columns()
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'entry_attachments'
      AND COLUMN_NAME = 'file_name'
  ) THEN
    UPDATE entry_attachments
    SET
      original_filename = COALESCE(original_filename, file_name),
      storage_filename = COALESCE(storage_filename, file_name)
    WHERE original_filename IS NULL OR storage_filename IS NULL;
  END IF;

  UPDATE entry_attachments
  SET storage_filename = COALESCE(storage_filename, SUBSTRING_INDEX(storage_path, '/', -1))
  WHERE storage_filename IS NULL AND storage_path IS NOT NULL;

  UPDATE entry_attachments
  SET file_ext = LOWER(SUBSTRING_INDEX(COALESCE(original_filename, storage_filename), '.', -1))
  WHERE file_ext IS NULL
    AND COALESCE(original_filename, storage_filename) LIKE '%.%';

  UPDATE entry_attachments
  SET content_hash = SHA2(CONCAT_WS('|', id, entry_id, COALESCE(storage_path, ''), COALESCE(original_filename, '')), 256)
  WHERE content_hash IS NULL;
END //

DELIMITER ;

CALL backfill_legacy_attachment_columns();
DROP PROCEDURE IF EXISTS backfill_legacy_attachment_columns;

-- 旧表字段名可能为 file_name/file_path；本脚本会自动从 file_name 回填 original_filename/storage_filename。
-- 以下约束只在字段已经完整回填后执行，避免线上旧脏数据导致迁移中断。
-- ALTER TABLE entry_attachments MODIFY user_id bigint unsigned NOT NULL;
-- ALTER TABLE entry_attachments MODIFY original_filename varchar(255) NOT NULL;
-- ALTER TABLE entry_attachments MODIFY storage_filename varchar(255) NOT NULL;
-- ALTER TABLE entry_attachments MODIFY file_ext varchar(20) NOT NULL;
-- ALTER TABLE entry_attachments MODIFY content_hash char(64) NOT NULL;
-- ALTER TABLE entry_attachments MODIFY storage_path varchar(500) NOT NULL;

CALL add_index_if_missing('entry_attachments', 'idx_entry_attachments_user_time', 'CREATE INDEX idx_entry_attachments_user_time ON entry_attachments (user_id, created_at)');
CALL add_index_if_missing('entry_attachments', 'idx_entry_attachments_status', 'CREATE INDEX idx_entry_attachments_status ON entry_attachments (status)');
CALL add_index_if_missing('entry_attachments', 'idx_entry_attachments_hash', 'CREATE INDEX idx_entry_attachments_hash ON entry_attachments (user_id, content_hash)');

DROP PROCEDURE IF EXISTS add_index_if_missing;

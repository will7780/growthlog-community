-- Migration: 029_add_ai_stream_request_id.sql
-- R11.3: request_id for streaming turn idempotency (ai_conversations.request_id).
-- Idempotent. Does not modify 027/028. Not executed on any database yet (isolation-only).

USE growth_log;

-- ---------------------------------------------------------------------------
-- ai_conversations.request_id
-- ---------------------------------------------------------------------------
SET @has_rid := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND COLUMN_NAME = 'request_id'
);
SET @sql_rid := IF(
  @has_rid = 0,
  'ALTER TABLE `ai_conversations` ADD COLUMN `request_id` VARCHAR(64) NULL DEFAULT NULL AFTER `proposal_id`',
  'SELECT 1'
);
PREPARE stmt_rid FROM @sql_rid;
EXECUTE stmt_rid;
DEALLOCATE PREPARE stmt_rid;

SET @has_rid_idx := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND INDEX_NAME = 'idx_ai_conversations_request_id'
);
SET @sql_rid_idx := IF(
  @has_rid_idx = 0,
  'ALTER TABLE `ai_conversations` ADD KEY `idx_ai_conversations_request_id` (`request_id`)',
  'SELECT 1'
);
PREPARE stmt_rid_idx FROM @sql_rid_idx;
EXECUTE stmt_rid_idx;
DEALLOCATE PREPARE stmt_rid_idx;

SET @has_uq := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND INDEX_NAME = 'uq_ai_conversations_user_session_request_role'
);
SET @sql_uq := IF(
  @has_uq = 0,
  'ALTER TABLE `ai_conversations` ADD UNIQUE KEY `uq_ai_conversations_user_session_request_role` (`user_id`, `session_id`, `request_id`, `role`)',
  'SELECT 1'
);
PREPARE stmt_uq FROM @sql_uq;
EXECUTE stmt_uq;
DEALLOCATE PREPARE stmt_uq;

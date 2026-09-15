-- Migration: 028_link_ai_messages_to_source_proposals.sql
-- R11.2-Fix3: link explainer messages to source proposals via proposal_id.
-- Idempotent. Does not modify 027. Does not backfill legacy rows (remain NULL).

USE growth_log;

-- ---------------------------------------------------------------------------
-- ai_conversations.proposal_id
-- ---------------------------------------------------------------------------
SET @has_pid := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND COLUMN_NAME = 'proposal_id'
);
SET @sql_pid := IF(
  @has_pid = 0,
  'ALTER TABLE `ai_conversations` ADD COLUMN `proposal_id` BIGINT UNSIGNED NULL DEFAULT NULL AFTER `source_set_version`',
  'SELECT 1'
);
PREPARE stmt_pid FROM @sql_pid;
EXECUTE stmt_pid;
DEALLOCATE PREPARE stmt_pid;

SET @has_pid_idx := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND INDEX_NAME = 'idx_ai_conversations_proposal_id'
);
SET @sql_pid_idx := IF(
  @has_pid_idx = 0,
  'ALTER TABLE `ai_conversations` ADD KEY `idx_ai_conversations_proposal_id` (`proposal_id`)',
  'SELECT 1'
);
PREPARE stmt_pid_idx FROM @sql_pid_idx;
EXECUTE stmt_pid_idx;
DEALLOCATE PREPARE stmt_pid_idx;

SET @has_uq := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND INDEX_NAME = 'uq_ai_conversations_user_session_proposal_role'
);
SET @sql_uq := IF(
  @has_uq = 0,
  'ALTER TABLE `ai_conversations` ADD UNIQUE KEY `uq_ai_conversations_user_session_proposal_role` (`user_id`, `session_id`, `proposal_id`, `role`)',
  'SELECT 1'
);
PREPARE stmt_uq FROM @sql_uq;
EXECUTE stmt_uq;
DEALLOCATE PREPARE stmt_uq;

SET @has_fk := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND CONSTRAINT_NAME = 'fk_ai_conversations_proposal'
    AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
SET @sql_fk := IF(
  @has_fk = 0,
  'ALTER TABLE `ai_conversations` ADD CONSTRAINT `fk_ai_conversations_proposal` FOREIGN KEY (`proposal_id`) REFERENCES `ai_conversation_source_sets` (`id`) ON DELETE SET NULL ON UPDATE CASCADE',
  'SELECT 1'
);
PREPARE stmt_fk FROM @sql_fk;
EXECUTE stmt_fk;
DEALLOCATE PREPARE stmt_fk;

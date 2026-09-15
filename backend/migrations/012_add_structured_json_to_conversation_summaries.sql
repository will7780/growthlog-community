-- Migration: 012_add_structured_json_to_conversation_summaries.sql
-- 说明：为 AI 会话摘要增加结构化 JSON 字段，预留 open_questions / decisions / pending_actions 等结构化压缩结果。

USE growth_log;

SET @column_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversation_summaries'
    AND COLUMN_NAME = 'structured_json'
);

SET @sql := IF(
  @column_exists = 0,
  'ALTER TABLE `ai_conversation_summaries` ADD COLUMN `structured_json` json DEFAULT NULL AFTER `summary`',
  'SELECT "structured_json already exists"'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Migration: 015_add_ai_conversations_fulltext.sql
-- 说明：为 ai_conversations.content 增加 FULLTEXT 索引，供 ai_session_search 优先使用。
-- 兼容策略：未执行本迁移时，Agent 会自动回退 LIKE 检索，不影响回答。
-- 若生产 MySQL 已启用 ngram parser 且需要更好的中文分词，可按环境改为：
--   ADD FULLTEXT INDEX ft_ai_conversations_content (content) WITH PARSER ngram;
-- 默认脚本不指定 parser，优先保证兼容性。

USE growth_log;

SET @index_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND INDEX_NAME = 'ft_ai_conversations_content'
);

SET @sql := IF(
  @index_exists = 0,
  'ALTER TABLE `ai_conversations` ADD FULLTEXT INDEX `ft_ai_conversations_content` (`content`)',
  'SELECT "ft_ai_conversations_content already exists"'
);

PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 回滚脚本（如需回滚）
-- ALTER TABLE `ai_conversations` DROP INDEX `ft_ai_conversations_content`;

-- Migration: 016_create_ai_conversation_embeddings.sql
-- 说明：创建 AI 历史会话专用 embedding 表，供 ai_session_search 向量检索使用。
-- 兼容策略：未执行本迁移或未回填向量时，Agent 自动回退 FULLTEXT/LIKE。
-- 约束：会话向量不得写入 entries/embeddings/knowledge_embeddings/attachment_embeddings。

USE growth_log;

CREATE TABLE IF NOT EXISTS `ai_conversation_embeddings` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `conversation_id` bigint unsigned NOT NULL COMMENT 'AI 会话消息 ID',
  `user_id` bigint unsigned NOT NULL COMMENT '用户 ID',
  `session_id` varchar(64) NOT NULL COMMENT '会话 ID',
  `role` enum('user','assistant') NOT NULL COMMENT '消息角色',
  `content_hash` char(64) NOT NULL COMMENT '消息内容 SHA256',
  `embedding_model` varchar(100) NOT NULL COMMENT '向量模型名',
  `vector` json NOT NULL COMMENT '768 维文本向量',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_conv_emb_conversation_model` (`conversation_id`, `embedding_model`),
  KEY `idx_ai_conv_emb_user` (`user_id`),
  KEY `idx_ai_conv_emb_session` (`session_id`),
  KEY `idx_ai_conv_emb_user_session` (`user_id`, `session_id`),
  CONSTRAINT `fk_ai_conv_emb_conversation` FOREIGN KEY (`conversation_id`) REFERENCES `ai_conversations` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_ai_conv_emb_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 回滚脚本（如需回滚）
-- DROP TABLE IF EXISTS `ai_conversation_embeddings`;

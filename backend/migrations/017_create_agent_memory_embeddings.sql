-- Migration: 017_create_agent_memory_embeddings.sql
-- 说明：创建 Agent 长期记忆专用 embedding 表，供记忆向量召回使用。
-- 兼容策略：未执行本迁移或未回填向量时，Agent context 自动回退最近 active 记忆。
-- 约束：记忆向量不得写入 entries/embeddings/knowledge_embeddings/attachment_embeddings/ai_conversation_embeddings。

USE growth_log;

CREATE TABLE IF NOT EXISTS `agent_memory_embeddings` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `memory_id` bigint unsigned NOT NULL COMMENT 'agent_memories.id',
  `user_id` bigint unsigned NOT NULL COMMENT '用户 ID',
  `memory_type` enum('goal','preference','project','profile','insight') NOT NULL COMMENT '记忆类型',
  `content_hash` char(64) NOT NULL COMMENT '记忆内容 SHA256',
  `embedding_model` varchar(100) NOT NULL COMMENT '向量模型名',
  `vector` json NOT NULL COMMENT '768 维文本向量',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_agent_mem_emb_memory_model` (`memory_id`, `embedding_model`),
  KEY `idx_agent_mem_emb_user` (`user_id`),
  KEY `idx_agent_mem_emb_user_type` (`user_id`, `memory_type`),
  CONSTRAINT `fk_agent_mem_emb_memory` FOREIGN KEY (`memory_id`) REFERENCES `agent_memories` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_agent_mem_emb_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 回滚脚本（如需回滚）
-- DROP TABLE IF EXISTS `agent_memory_embeddings`;

-- Migration: 009_create_agent_runtime_tables.sql
-- 说明：创建 Agent 工具审计、长期记忆、会话摘要表

USE growth_log;

CREATE TABLE IF NOT EXISTS `agent_tool_audits` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `session_id` varchar(64) NOT NULL,
  `tool_name` varchar(100) NOT NULL,
  `permission` enum('read','write','admin') NOT NULL DEFAULT 'read',
  `status` enum('allowed','denied','failed') NOT NULL,
  `input_json` json DEFAULT NULL,
  `output_summary` varchar(500) DEFAULT NULL,
  `error_message` text DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_agent_tool_audits_user_time` (`user_id`, `created_at`),
  KEY `idx_agent_tool_audits_session` (`session_id`),
  KEY `idx_agent_tool_audits_tool` (`tool_name`),
  CONSTRAINT `fk_agent_tool_audits_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `agent_memories` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `memory_type` enum('goal','preference','project','profile','insight') NOT NULL,
  `content` text NOT NULL,
  `source` varchar(100) NOT NULL DEFAULT 'agent',
  `confidence` float NOT NULL DEFAULT 0.6,
  `is_active` tinyint(1) NOT NULL DEFAULT 1,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_agent_memories_user_type` (`user_id`, `memory_type`, `is_active`),
  KEY `idx_agent_memories_user_time` (`user_id`, `created_at`),
  CONSTRAINT `fk_agent_memories_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `ai_conversation_summaries` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `session_id` varchar(64) NOT NULL,
  `summary` text NOT NULL,
  `message_count` int NOT NULL DEFAULT 0,
  `last_message_at` timestamp NULL DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_ai_conversation_summaries_session` (`user_id`, `session_id`),
  KEY `idx_ai_conversation_summaries_user_time` (`user_id`, `updated_at`),
  CONSTRAINT `fk_ai_conversation_summaries_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

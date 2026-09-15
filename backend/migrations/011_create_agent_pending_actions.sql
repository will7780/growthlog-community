-- Migration: 011_create_agent_pending_actions.sql
-- 说明：创建 Agent 写操作确认表。Agent 只生成 pending action，用户确认后才执行白名单写操作。

USE growth_log;

CREATE TABLE IF NOT EXISTS `agent_pending_actions` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `session_id` varchar(64) DEFAULT NULL,
  `action_type` enum('create_todo','create_memory','update_memory','save_summary_entry') NOT NULL,
  `payload_json` json NOT NULL,
  `status` enum('pending','confirmed','rejected','executed','failed') NOT NULL DEFAULT 'pending',
  `result_json` json DEFAULT NULL,
  `error_message` text DEFAULT NULL,
  `created_by_message_id` bigint unsigned DEFAULT NULL,
  `confirmed_at` timestamp NULL DEFAULT NULL,
  `executed_at` timestamp NULL DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_agent_pending_actions_user_status` (`user_id`, `status`, `created_at`),
  KEY `idx_agent_pending_actions_session` (`session_id`),
  KEY `idx_agent_pending_actions_type` (`action_type`),
  CONSTRAINT `fk_agent_pending_actions_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

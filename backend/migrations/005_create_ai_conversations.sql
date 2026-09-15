-- =====================================================
-- AI 会话表
-- 创建日期: 2026-03-28
-- 版本: 0.8.0
-- =====================================================

CREATE TABLE `ai_conversations` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL COMMENT '用户ID',
  `session_id` varchar(64) NOT NULL COMMENT '会话ID，用于区分不同对话',
  `role` enum('user', 'assistant') NOT NULL COMMENT '角色：user=用户，assistant=AI',
  `content` text NOT NULL COMMENT '消息内容',
  `references` json DEFAULT NULL COMMENT '引用记录，JSON数组',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_conversations_user_session` (`user_id`, `session_id`, `created_at`),
  KEY `idx_conversations_session` (`session_id`),
  CONSTRAINT `fk_conversations_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- 回滚脚本（如需回滚）
-- =====================================================
-- DROP TABLE IF EXISTS `ai_conversations`;
-- =====================================================

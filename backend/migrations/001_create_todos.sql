-- =====================================================
-- 小要事模块 - 数据库迁移脚本
-- 创建日期: 2026-03-28
-- 版本: 0.5.0
-- =====================================================

-- 创建 todos 表
CREATE TABLE IF NOT EXISTS `todos` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `content` varchar(500) NOT NULL COMMENT '小要事内容',
  `due_date` date DEFAULT NULL COMMENT '截止日期',
  `is_done` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否完成：0-未完成，1-已完成',
  `completed_at` timestamp NULL DEFAULT NULL COMMENT '完成时间',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_todos_user_due` (`user_id`, `due_date`),
  KEY `idx_todos_user_done` (`user_id`, `is_done`),
  CONSTRAINT `fk_todos_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- 回滚脚本（如需回滚）
-- DROP TABLE IF EXISTS `todos`;
-- =====================================================

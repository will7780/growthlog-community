-- Migration: 002_create_entry_attachments.sql
-- 说明：创建 entry_attachments 表，用于存储记录的附件（图片、文件等）
-- 执行方式：在 Navicat 中打开此文件，连接到 growth_log 数据库，执行

USE growth_log;

-- 检查表是否已存在（可选，仅用于查看）
SELECT COUNT(*) as table_exists
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'growth_log'
  AND TABLE_NAME = 'entry_attachments';

-- 如果不存在，则创建表
CREATE TABLE IF NOT EXISTS `entry_attachments` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `entry_id` bigint unsigned NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `original_filename` varchar(255) NOT NULL,
  `storage_filename` varchar(255) NOT NULL,
  `mime_type` varchar(100) NOT NULL,
  `file_ext` varchar(20) NOT NULL,
  `file_size` bigint NOT NULL,
  `content_hash` char(64) NOT NULL,
  `storage_path` varchar(500) NOT NULL COMMENT '相对上传根目录路径，如 user_id/entry_id/uuid.pdf',
  `status` enum('uploaded','processing','indexed','failed') NOT NULL DEFAULT 'uploaded',
  `page_count` int DEFAULT NULL,
  `slide_count` int DEFAULT NULL,
  `preview_path` varchar(500) DEFAULT NULL,
  `error_message` text DEFAULT NULL,
  `processed_at` timestamp NULL DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_entry_attachments_entry` (`entry_id`),
  KEY `idx_entry_attachments_user_time` (`user_id`, `created_at`),
  KEY `idx_entry_attachments_status` (`status`),
  KEY `idx_entry_attachments_hash` (`user_id`, `content_hash`),
  CONSTRAINT `fk_entry_attachments_entry` FOREIGN KEY (`entry_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_entry_attachments_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 验证：查看表结构
DESCRIBE `entry_attachments`;

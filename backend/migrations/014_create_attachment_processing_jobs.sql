-- Migration: 014_create_attachment_processing_jobs.sql
-- 说明：创建附件处理独立任务表，支持 worker 领取、重试与超时回收

USE growth_log;

CREATE TABLE IF NOT EXISTS `attachment_processing_jobs` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `attachment_id` bigint unsigned NOT NULL,
  `entry_id` bigint unsigned NOT NULL,
  `job_type` varchar(50) NOT NULL DEFAULT 'extract_text' COMMENT '任务类型，如 extract_text',
  `status` enum('pending','processing','succeeded','failed','cancelled') NOT NULL DEFAULT 'pending',
  `priority` int NOT NULL DEFAULT 0 COMMENT '数值越大越优先',
  `attempt_count` int NOT NULL DEFAULT 0 COMMENT '已尝试次数',
  `max_attempts` int NOT NULL DEFAULT 3 COMMENT '最大尝试次数',
  `locked_by` varchar(100) DEFAULT NULL COMMENT '领取 worker 标识',
  `locked_at` timestamp NULL DEFAULT NULL COMMENT '领取时间',
  `available_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '可再次领取时间',
  `started_at` timestamp NULL DEFAULT NULL COMMENT '首次开始处理时间',
  `finished_at` timestamp NULL DEFAULT NULL COMMENT '完成/失败/取消时间',
  `error_message` text DEFAULT NULL,
  `metadata_json` json DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_attachment_processing_jobs_user` (`user_id`),
  KEY `idx_attachment_processing_jobs_attachment` (`attachment_id`),
  KEY `idx_attachment_processing_jobs_status_available` (`status`, `available_at`),
  KEY `idx_attachment_processing_jobs_locked_at` (`locked_at`),
  CONSTRAINT `fk_attachment_processing_jobs_attachment` FOREIGN KEY (`attachment_id`) REFERENCES `entry_attachments` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_processing_jobs_entry` FOREIGN KEY (`entry_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_processing_jobs_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

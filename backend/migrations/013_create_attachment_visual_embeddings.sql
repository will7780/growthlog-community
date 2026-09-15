-- Migration: 013_create_attachment_visual_embeddings.sql
-- 说明：创建独立视觉 embedding 预留表，不与 E5 文本向量混用

USE growth_log;

CREATE TABLE IF NOT EXISTS `attachment_visual_embeddings` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `attachment_id` bigint unsigned NOT NULL,
  `entry_id` bigint unsigned NOT NULL,
  `image_ref` varchar(500) DEFAULT NULL COMMENT '图片路径或对象存储引用',
  `page_no` int DEFAULT NULL COMMENT 'PDF 页码或页面级引用，可为空',
  `provider` varchar(100) NOT NULL DEFAULT 'disabled' COMMENT '视觉 embedding 提供方',
  `model` varchar(100) NOT NULL DEFAULT 'disabled' COMMENT '视觉 embedding 模型名',
  `embedding_dim` int NOT NULL DEFAULT 0 COMMENT '向量维度，0 表示尚未写入',
  `vector_json` json DEFAULT NULL COMMENT '视觉向量 JSON，当前阶段可为空',
  `metadata_json` json DEFAULT NULL COMMENT '抽取/索引元数据',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_attachment_visual_embeddings_user` (`user_id`),
  KEY `idx_attachment_visual_embeddings_attachment` (`attachment_id`),
  KEY `idx_attachment_visual_embeddings_entry` (`entry_id`),
  CONSTRAINT `fk_attachment_visual_embeddings_attachment` FOREIGN KEY (`attachment_id`) REFERENCES `entry_attachments` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_visual_embeddings_entry` FOREIGN KEY (`entry_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_visual_embeddings_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

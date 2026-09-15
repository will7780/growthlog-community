-- Migration: 007_create_attachment_rag_tables.sql
-- 说明：创建附件内容切片和向量表，用于 PDF/PPTX/图片 OCR 文本检索

USE growth_log;

CREATE TABLE IF NOT EXISTS `attachment_chunks` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `attachment_id` bigint unsigned NOT NULL,
  `entry_id` bigint unsigned NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `chunk_index` int NOT NULL,
  `modality` enum('pdf_text','ppt_text','ocr','caption','table') NOT NULL,
  `page_no` int DEFAULT NULL,
  `slide_no` int DEFAULT NULL,
  `bbox_json` json DEFAULT NULL,
  `content` text NOT NULL,
  `token_count` int DEFAULT NULL,
  `metadata_json` json DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_attachment_chunk` (`attachment_id`, `chunk_index`),
  KEY `idx_attachment_chunks_attachment` (`attachment_id`),
  KEY `idx_attachment_chunks_user` (`user_id`),
  KEY `idx_attachment_chunks_entry` (`entry_id`),
  CONSTRAINT `fk_attachment_chunks_attachment` FOREIGN KEY (`attachment_id`) REFERENCES `entry_attachments` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_chunks_entry` FOREIGN KEY (`entry_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_chunks_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `attachment_embeddings` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `chunk_id` bigint unsigned NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `embedding_model` varchar(100) NOT NULL,
  `vector` json NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_attachment_embeddings_chunk` (`chunk_id`),
  KEY `idx_attachment_embeddings_chunk` (`chunk_id`),
  KEY `idx_attachment_embeddings_user` (`user_id`),
  CONSTRAINT `fk_attachment_embeddings_chunk` FOREIGN KEY (`chunk_id`) REFERENCES `attachment_chunks` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_attachment_embeddings_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- 向量嵌入表 - 存储记录的 embedding 向量
-- 创建日期: 2026-03-28
-- 版本: 0.7.0
-- =====================================================

CREATE TABLE `embeddings` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `entry_id` bigint unsigned NOT NULL COMMENT '关联的记录ID',
  `entry_type` enum('main', 'child') NOT NULL DEFAULT 'main' COMMENT '记录类型：main=主记录，child=子记录',
  `title_vector` json DEFAULT NULL COMMENT '标题向量，768维，JSON格式存储',
  `content_vector` json DEFAULT NULL COMMENT '内容向量，768维，JSON格式存储',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_entry_type` (`entry_id`, `entry_type`),
  KEY `idx_embeddings_entry` (`entry_id`),
  CONSTRAINT `fk_embeddings_entry` FOREIGN KEY (`entry_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- =====================================================
-- 回滚脚本（如需回滚）
-- =====================================================
-- DROP TABLE IF EXISTS `embeddings`;
-- =====================================================

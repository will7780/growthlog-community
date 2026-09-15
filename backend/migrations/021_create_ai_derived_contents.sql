-- =====================================================
-- R6: AI 派生内容表（整合结果等，不污染 Entry）
-- 幂等：可重复执行
-- =====================================================

CREATE TABLE IF NOT EXISTS `ai_derived_contents` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL COMMENT '用户ID',
  `type` varchar(32) NOT NULL COMMENT 'organize_summary / tag_suggestion / action_plan / annotation / memory_note / review',
  `title` varchar(255) NOT NULL COMMENT '派生内容标题',
  `content` text NOT NULL COMMENT '派生正文，不写入原始 Entry',
  `scope_type` varchar(32) DEFAULT NULL COMMENT 'days_labels / entries / recent',
  `source_entry_ids` json NOT NULL COMMENT '来源记录 ID 列表',
  `metadata` json DEFAULT NULL COMMENT 'confidence/confidence/preview_id/item_id 等',
  `status` varchar(32) NOT NULL DEFAULT 'confirmed' COMMENT 'draft / confirmed / dismissed',
  `confirm_key` varchar(64) DEFAULT NULL COMMENT '幂等键 preview_id+item_id',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_derived_user_confirm_key` (`user_id`, `confirm_key`),
  KEY `idx_derived_user_type_created` (`user_id`, `type`, `created_at`),
  KEY `idx_derived_user_status_created` (`user_id`, `status`, `created_at`),
  CONSTRAINT `fk_derived_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

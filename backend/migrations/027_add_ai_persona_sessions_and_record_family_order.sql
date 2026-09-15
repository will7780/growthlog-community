-- Migration: 027_add_ai_persona_sessions_and_record_family_order.sql
-- R11.1: persona sessions, versioned source sets, message source_set_version, attachment sort_order.
-- Idempotent. Does not backfill persona for legacy sessions or delete existing data.

USE growth_log;

-- ---------------------------------------------------------------------------
-- ai_chat_sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_chat_sessions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `session_id` VARCHAR(64) NOT NULL,
  `persona` ENUM('retriever', 'explainer', 'organizer') NOT NULL,
  `title` VARCHAR(255) NULL DEFAULT NULL,
  `status` ENUM('active', 'archived') NOT NULL DEFAULT 'active',
  `current_source_set_version` INT UNSIGNED NULL DEFAULT NULL,
  `last_message_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_chat_sessions_user_session` (`user_id`, `session_id`),
  KEY `idx_ai_chat_sessions_user_last_message` (`user_id`, `last_message_at`),
  CONSTRAINT `fk_ai_chat_sessions_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- ai_conversation_source_sets
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `ai_conversation_source_sets` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `session_id` VARCHAR(64) NOT NULL,
  `version` INT UNSIGNED NULL DEFAULT NULL COMMENT 'NULL while proposed; 1+ when locked/superseded/stale',
  `base_version` INT UNSIGNED NULL DEFAULT NULL COMMENT 'CAS base for expansion proposals; 0 for initial lock',
  `status` ENUM('proposed', 'locked', 'superseded', 'stale') NOT NULL DEFAULT 'proposed',
  `source_manifest` JSON NOT NULL,
  `content_fingerprint` CHAR(64) NULL DEFAULT NULL,
  `locked_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_source_sets_user_session_version` (`user_id`, `session_id`, `version`),
  KEY `idx_ai_source_sets_user_session_status` (`user_id`, `session_id`, `status`),
  CONSTRAINT `fk_ai_source_sets_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `fk_ai_source_sets_session`
    FOREIGN KEY (`user_id`, `session_id`)
    REFERENCES `ai_chat_sessions` (`user_id`, `session_id`)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- ai_conversations.source_set_version
-- ---------------------------------------------------------------------------
SET @has_ssv := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'ai_conversations'
    AND COLUMN_NAME = 'source_set_version'
);
SET @sql_ssv := IF(
  @has_ssv = 0,
  'ALTER TABLE `ai_conversations` ADD COLUMN `source_set_version` INT UNSIGNED NULL DEFAULT NULL AFTER `references`',
  'SELECT 1'
);
PREPARE stmt_ssv FROM @sql_ssv;
EXECUTE stmt_ssv;
DEALLOCATE PREPARE stmt_ssv;

-- ---------------------------------------------------------------------------
-- entry_attachments.sort_order
-- ---------------------------------------------------------------------------
SET @has_sort := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'entry_attachments'
    AND COLUMN_NAME = 'sort_order'
);
SET @sql_sort := IF(
  @has_sort = 0,
  'ALTER TABLE `entry_attachments` ADD COLUMN `sort_order` INT UNSIGNED NULL DEFAULT NULL AFTER `status`',
  'SELECT 1'
);
PREPARE stmt_sort FROM @sql_sort;
EXECUTE stmt_sort;
DEALLOCATE PREPARE stmt_sort;

SET @has_sort_idx := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'entry_attachments'
    AND INDEX_NAME = 'idx_entry_attachments_entry_sort'
);
SET @sql_sort_idx := IF(
  @has_sort_idx = 0,
  'ALTER TABLE `entry_attachments` ADD KEY `idx_entry_attachments_entry_sort` (`entry_id`, `sort_order`, `created_at`, `id`)',
  'SELECT 1'
);
PREPARE stmt_sort_idx FROM @sql_sort_idx;
EXECUTE stmt_sort_idx;
DEALLOCATE PREPARE stmt_sort_idx;

-- Deterministic backfill: only rows still NULL; preserves existing order on re-run.
UPDATE `entry_attachments` AS ea
INNER JOIN (
  SELECT
    `id`,
    ROW_NUMBER() OVER (
      PARTITION BY `entry_id`
      ORDER BY `created_at` ASC, `id` ASC
    ) - 1 AS `rn`
  FROM `entry_attachments`
) AS ranked ON ea.`id` = ranked.`id`
SET ea.`sort_order` = ranked.`rn`
WHERE ea.`sort_order` IS NULL;

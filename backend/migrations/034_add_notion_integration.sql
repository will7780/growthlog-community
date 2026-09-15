-- Migration: 034_add_notion_integration.sql
-- GrowthLog 11.12.0: Notion read-only OAuth, local page mirror, and Knowledge ENUM.
-- Idempotent. Does not rewrite existing Knowledge/Todo/033 rows.

USE growth_log;

SET @ks_type := (
  SELECT COLUMN_TYPE
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'knowledge_sources'
    AND COLUMN_NAME = 'source_type'
);

SET @ks_type_sql := IF(
  @ks_type LIKE '%notion_page%',
  'SELECT ''knowledge_sources.source_type already includes notion_page'' AS migration_034_ks_noop',
  'ALTER TABLE `knowledge_sources` MODIFY COLUMN `source_type` ENUM(''entry'',''attachment'',''memory'',''todo'',''notion_page'') NOT NULL'
);

PREPARE stmt_034_ks FROM @ks_type_sql;
EXECUTE stmt_034_ks;
DEALLOCATE PREPARE stmt_034_ks;

SET @kc_origin := (
  SELECT COLUMN_TYPE
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'knowledge_chunks'
    AND COLUMN_NAME = 'origin_type'
);

SET @kc_origin_sql := IF(
  @kc_origin LIKE '%notion_page%',
  'SELECT ''knowledge_chunks.origin_type already includes notion_page'' AS migration_034_kc_origin_noop',
  'ALTER TABLE `knowledge_chunks` MODIFY COLUMN `origin_type` ENUM(''entry'',''attachment'',''memory'',''todo'',''notion_page'') NOT NULL'
);

PREPARE stmt_034_kc_origin FROM @kc_origin_sql;
EXECUTE stmt_034_kc_origin;
DEALLOCATE PREPARE stmt_034_kc_origin;

SET @kc_chunk := (
  SELECT COLUMN_TYPE
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'knowledge_chunks'
    AND COLUMN_NAME = 'chunk_type'
);

SET @kc_chunk_sql := IF(
  @kc_chunk LIKE '%notion_text%',
  'SELECT ''knowledge_chunks.chunk_type already includes notion_text'' AS migration_034_kc_chunk_noop',
  'ALTER TABLE `knowledge_chunks` MODIFY COLUMN `chunk_type` ENUM(''entry_text'',''pdf_text'',''ppt_text'',''ocr'',''caption'',''memory'',''notion_text'') NOT NULL DEFAULT ''entry_text'''
);

PREPARE stmt_034_kc_chunk FROM @kc_chunk_sql;
EXECUTE stmt_034_kc_chunk;
DEALLOCATE PREPARE stmt_034_kc_chunk;

CREATE TABLE IF NOT EXISTS `notion_connections` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `bot_id` VARCHAR(64) NOT NULL,
  `workspace_id` VARCHAR(64) NOT NULL,
  `workspace_name` VARCHAR(255) NOT NULL DEFAULT '',
  `owner_notion_user_id` VARCHAR(64) NULL,
  `access_token_encrypted` VARBINARY(2048) NOT NULL,
  `refresh_token_encrypted` VARBINARY(2048) NULL,
  `token_crypto_version` TINYINT UNSIGNED NOT NULL DEFAULT 1,
  `status` ENUM('active','reauth_required','disconnected') NOT NULL DEFAULT 'active',
  `sync_status` ENUM('pending','syncing','ready','partial','failed') NOT NULL DEFAULT 'pending',
  `last_sync_started_at` TIMESTAMP NULL,
  `last_sync_completed_at` TIMESTAMP NULL,
  `last_reconcile_at` TIMESTAMP NULL,
  `last_error_code` VARCHAR(64) NULL,
  `token_expires_at` TIMESTAMP NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_notion_connections_user` (`user_id`),
  UNIQUE KEY `uk_notion_connections_bot` (`bot_id`),
  KEY `idx_notion_connections_status` (`status`,`sync_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `notion_oauth_states` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `state_hash` CHAR(64) NOT NULL,
  `expires_at` TIMESTAMP NOT NULL,
  `consumed_at` TIMESTAMP NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_notion_oauth_states_hash` (`state_hash`),
  KEY `idx_notion_oauth_states_user_exp` (`user_id`,`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `notion_pages` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `connection_id` BIGINT NOT NULL,
  `user_id` BIGINT NOT NULL,
  `notion_page_uuid` CHAR(36) NOT NULL,
  `parent_notion_page_uuid` CHAR(36) NULL,
  `title` VARCHAR(512) NOT NULL DEFAULT '',
  `breadcrumb` VARCHAR(1024) NOT NULL DEFAULT '',
  `notion_url` VARCHAR(512) NULL,
  `normalized_text` MEDIUMTEXT NULL,
  `remote_last_edited_at` TIMESTAMP NULL,
  `observed_content_hash` CHAR(64) NOT NULL DEFAULT '',
  `indexed_content_hash` CHAR(64) NULL,
  `sync_status` ENUM('pending','processing','indexed','partial','failed','permission_lost','deleted') NOT NULL DEFAULT 'pending',
  `unsupported_block_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `last_error_code` VARCHAR(64) NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_notion_pages_connection_uuid` (`connection_id`,`notion_page_uuid`),
  KEY `idx_notion_pages_user_status` (`user_id`,`sync_status`),
  CONSTRAINT `fk_notion_pages_connection`
    FOREIGN KEY (`connection_id`) REFERENCES `notion_connections` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `notion_sync_jobs` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT NOT NULL,
  `connection_id` BIGINT NOT NULL,
  `notion_page_id` BIGINT NULL,
  `job_type` ENUM('initial_discovery','reconcile','sync_page','delete_page') NOT NULL,
  `status` ENUM('pending','processing','retry','succeeded','failed','cancelled') NOT NULL DEFAULT 'pending',
  `provider_event_id` VARCHAR(128) NULL,
  `attempt_count` INT UNSIGNED NOT NULL DEFAULT 0,
  `available_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `locked_at` TIMESTAMP NULL,
  `lock_owner` VARCHAR(100) NULL,
  `lease_until` TIMESTAMP NULL,
  `last_error_code` VARCHAR(64) NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_notion_sync_jobs_event` (`provider_event_id`),
  KEY `idx_notion_sync_jobs_claim` (`status`,`available_at`,`id`),
  KEY `idx_notion_sync_jobs_connection` (`connection_id`,`status`),
  CONSTRAINT `fk_notion_sync_jobs_connection`
    FOREIGN KEY (`connection_id`) REFERENCES `notion_connections` (`id`)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rollback policy: retain tables and ENUM values. Older images ignore them.

-- Migration: 025_create_notification_tables.sql
-- R9 WxPusher optional WeChat reminders: bindings, bind sessions, outbox.
-- Idempotent: CREATE TABLE IF NOT EXISTS.

USE growth_log;

CREATE TABLE IF NOT EXISTS `user_notification_bindings` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `provider` VARCHAR(32) NOT NULL DEFAULT 'wxpusher',
  `uid_ciphertext` VARBINARY(512) NOT NULL,
  `uid_hash` CHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'active',
  `reminders_enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `today_plan_time` TIME NULL DEFAULT '09:00:00',
  `unfinished_time` TIME NULL DEFAULT '20:00:00',
  `urgent_overdue_enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `quiet_hours_start` TIME NULL DEFAULT NULL,
  `quiet_hours_end` TIME NULL DEFAULT NULL,
  `bound_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_verified_at` TIMESTAMP NULL DEFAULT NULL,
  `last_test_sent_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_user_notification_bindings_user_id` (`user_id`),
  UNIQUE KEY `uq_user_notification_bindings_uid_hash` (`uid_hash`),
  KEY `idx_user_notification_bindings_status` (`status`),
  CONSTRAINT `fk_user_notification_bindings_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `notification_binding_sessions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `nonce_hash` CHAR(64) NOT NULL,
  `provider_qrcode_code` VARCHAR(128) NULL DEFAULT NULL,
  `provider_qrcode_url` VARCHAR(512) NULL DEFAULT NULL,
  `last_provider_poll_at` TIMESTAMP NULL DEFAULT NULL,
  `expires_at` TIMESTAMP NOT NULL,
  `consumed_at` TIMESTAMP NULL DEFAULT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_notification_binding_sessions_nonce_hash` (`nonce_hash`),
  KEY `idx_notification_binding_sessions_user_status` (`user_id`, `status`),
  KEY `idx_notification_binding_sessions_expires` (`expires_at`),
  CONSTRAINT `fk_notification_binding_sessions_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `notification_outbox` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `binding_id` BIGINT UNSIGNED NOT NULL,
  `notification_type` VARCHAR(32) NOT NULL,
  `local_date` DATE NOT NULL,
  `dedupe_key` VARCHAR(191) NOT NULL,
  `safe_payload_json` JSON NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
  `attempt_count` INT NOT NULL DEFAULT 0,
  `max_attempts` INT NOT NULL DEFAULT 5,
  `available_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `locked_by` VARCHAR(128) NULL DEFAULT NULL,
  `locked_at` TIMESTAMP NULL DEFAULT NULL,
  `provider_message_id` VARCHAR(128) NULL DEFAULT NULL,
  `error_code` VARCHAR(64) NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `sent_at` TIMESTAMP NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_notification_outbox_dedupe_key` (`dedupe_key`),
  KEY `idx_notification_outbox_claim` (`status`, `available_at`),
  KEY `idx_notification_outbox_user_date` (`user_id`, `local_date`),
  CONSTRAINT `fk_notification_outbox_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_notification_outbox_binding`
    FOREIGN KEY (`binding_id`) REFERENCES `user_notification_bindings` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

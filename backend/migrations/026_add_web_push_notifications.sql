-- Migration: 026_add_web_push_notifications.sql
-- R10 Web Push: provider-neutral preferences + multi-device subscriptions + outbox fan-out.
-- Idempotent. Compatible with 025; does not DROP any 025 tables/columns/data.

USE growth_log;

CREATE TABLE IF NOT EXISTS `notification_preferences` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `reminders_enabled` TINYINT(1) NOT NULL DEFAULT 0,
  `today_plan_time` TIME NULL DEFAULT '09:00:00',
  `unfinished_time` TIME NULL DEFAULT '20:00:00',
  `urgent_overdue_enabled` TINYINT(1) NOT NULL DEFAULT 1,
  `quiet_hours_start` TIME NULL DEFAULT NULL,
  `quiet_hours_end` TIME NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_notification_preferences_user_id` (`user_id`),
  CONSTRAINT `fk_notification_preferences_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One-time preference seed from legacy bindings. INSERT IGNORE keeps later edits.
INSERT IGNORE INTO `notification_preferences` (
  `user_id`, `reminders_enabled`, `today_plan_time`, `unfinished_time`,
  `urgent_overdue_enabled`, `quiet_hours_start`, `quiet_hours_end`, `created_at`
)
SELECT
  `user_id`,
  `reminders_enabled`,
  COALESCE(`today_plan_time`, '09:00:00'),
  COALESCE(`unfinished_time`, '20:00:00'),
  `urgent_overdue_enabled`,
  `quiet_hours_start`,
  `quiet_hours_end`,
  COALESCE(`created_at`, CURRENT_TIMESTAMP)
FROM `user_notification_bindings`;

CREATE TABLE IF NOT EXISTS `web_push_subscriptions` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `subscription_ciphertext` VARBINARY(4096) NOT NULL,
  `endpoint_hash` CHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'active',
  `failure_count` INT NOT NULL DEFAULT 0,
  `last_success_at` TIMESTAMP NULL DEFAULT NULL,
  `last_failure_at` TIMESTAMP NULL DEFAULT NULL,
  `last_error_code` VARCHAR(64) NULL DEFAULT NULL,
  `last_test_sent_at` TIMESTAMP NULL DEFAULT NULL,
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_web_push_subscriptions_endpoint_hash` (`endpoint_hash`),
  KEY `idx_web_push_subscriptions_user_status` (`user_id`, `status`),
  CONSTRAINT `fk_web_push_subscriptions_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Make binding_id nullable for Web Push-only outbox rows (keep FK + history).
SET @col_null := (
  SELECT IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notification_outbox'
    AND COLUMN_NAME = 'binding_id'
  LIMIT 1
);
SET @sql_bind := IF(
  @col_null = 'NO',
  'ALTER TABLE `notification_outbox` MODIFY COLUMN `binding_id` BIGINT UNSIGNED NULL',
  'SELECT 1'
);
PREPARE stmt_bind FROM @sql_bind;
EXECUTE stmt_bind;
DEALLOCATE PREPARE stmt_bind;

SET @has_wp := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notification_outbox'
    AND COLUMN_NAME = 'web_push_subscription_id'
);
SET @sql_wp := IF(
  @has_wp = 0,
  'ALTER TABLE `notification_outbox` ADD COLUMN `web_push_subscription_id` BIGINT UNSIGNED NULL AFTER `binding_id`',
  'SELECT 1'
);
PREPARE stmt_wp FROM @sql_wp;
EXECUTE stmt_wp;
DEALLOCATE PREPARE stmt_wp;

SET @has_wp_idx := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notification_outbox'
    AND INDEX_NAME = 'idx_notification_outbox_web_push_subscription_id'
);
SET @sql_wp_idx := IF(
  @has_wp_idx = 0,
  'ALTER TABLE `notification_outbox` ADD KEY `idx_notification_outbox_web_push_subscription_id` (`web_push_subscription_id`)',
  'SELECT 1'
);
PREPARE stmt_wp_idx FROM @sql_wp_idx;
EXECUTE stmt_wp_idx;
DEALLOCATE PREPARE stmt_wp_idx;

SET @has_wp_fk := (
  SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'notification_outbox'
    AND CONSTRAINT_NAME = 'fk_notification_outbox_web_push_subscription'
    AND CONSTRAINT_TYPE = 'FOREIGN KEY'
);
SET @sql_wp_fk := IF(
  @has_wp_fk = 0,
  'ALTER TABLE `notification_outbox` ADD CONSTRAINT `fk_notification_outbox_web_push_subscription` FOREIGN KEY (`web_push_subscription_id`) REFERENCES `web_push_subscriptions` (`id`) ON DELETE CASCADE',
  'SELECT 1'
);
PREPARE stmt_wp_fk FROM @sql_wp_fk;
EXECUTE stmt_wp_fk;
DEALLOCATE PREPARE stmt_wp_fk;

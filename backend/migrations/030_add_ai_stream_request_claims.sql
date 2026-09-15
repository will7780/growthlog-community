-- Migration: 030_add_ai_stream_request_claims.sql
-- R11.3-Fix: durable cross-worker request_id claim / lease for streaming turns.
-- Idempotent. Does not modify 027/028/029. Isolation-only until R11.4.

USE growth_log;

CREATE TABLE IF NOT EXISTS `ai_stream_request_claims` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` BIGINT UNSIGNED NOT NULL,
  `session_id` VARCHAR(64) NOT NULL,
  `request_id` VARCHAR(64) NOT NULL,
  `content_hash` CHAR(64) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'claimed',
  `lease_until` DATETIME(6) NOT NULL,
  `owner_token` VARCHAR(64) NOT NULL,
  `result_fingerprint` CHAR(64) NULL DEFAULT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  `updated_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_stream_claims_user_session_request` (`user_id`, `session_id`, `request_id`),
  KEY `idx_ai_stream_claims_lease` (`status`, `lease_until`),
  CONSTRAINT `fk_ai_stream_claims_user`
    FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

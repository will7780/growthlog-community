-- Unified knowledge RAG tables
-- Phase 2: source -> chunk -> embedding abstraction.
-- This migration is additive and keeps existing embeddings / attachment_* tables intact.

CREATE TABLE IF NOT EXISTS knowledge_sources (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_id BIGINT UNSIGNED NOT NULL,
  source_type ENUM('entry', 'attachment', 'memory') NOT NULL,
  source_id BIGINT UNSIGNED NOT NULL,
  title VARCHAR(255) NOT NULL DEFAULT '',
  status ENUM('pending', 'indexed', 'failed') NOT NULL DEFAULT 'pending',
  metadata_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_knowledge_source (user_id, source_type, source_id),
  KEY idx_knowledge_sources_user (user_id),
  KEY idx_knowledge_sources_type (source_type, source_id),
  KEY idx_knowledge_sources_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  origin_type ENUM('entry', 'attachment', 'memory') NOT NULL,
  origin_id BIGINT UNSIGNED NOT NULL,
  entry_id BIGINT UNSIGNED NULL,
  chunk_index INT NOT NULL,
  chunk_type ENUM('entry_text', 'pdf_text', 'ppt_text', 'ocr', 'caption', 'memory') NOT NULL DEFAULT 'entry_text',
  title VARCHAR(255) NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  page_no INT NULL,
  slide_no INT NULL,
  metadata_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_knowledge_chunk (source_id, chunk_index),
  KEY idx_knowledge_chunks_source (source_id),
  KEY idx_knowledge_chunks_user (user_id),
  KEY idx_knowledge_chunks_origin (origin_type, origin_id),
  KEY idx_knowledge_chunks_entry (entry_id),
  CONSTRAINT fk_knowledge_chunks_source
    FOREIGN KEY (source_id) REFERENCES knowledge_sources(id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS knowledge_embeddings (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  chunk_id BIGINT UNSIGNED NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  embedding_model VARCHAR(100) NOT NULL,
  vector JSON NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_knowledge_embedding (chunk_id, embedding_model),
  KEY idx_knowledge_embeddings_chunk (chunk_id),
  KEY idx_knowledge_embeddings_user (user_id),
  CONSTRAINT fk_knowledge_embeddings_chunk
    FOREIGN KEY (chunk_id) REFERENCES knowledge_chunks(id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

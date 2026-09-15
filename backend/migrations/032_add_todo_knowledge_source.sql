-- Migration: 032_add_todo_knowledge_source.sql
-- GrowthLog 11.10.0: allow Todo mirrors in the existing knowledge RAG tables.
-- Idempotent and backward compatible. No existing rows are rewritten.

USE growth_log;

ALTER TABLE `knowledge_sources`
  MODIFY COLUMN `source_type`
  ENUM('entry','attachment','memory','todo') NOT NULL;

ALTER TABLE `knowledge_chunks`
  MODIFY COLUMN `origin_type`
  ENUM('entry','attachment','memory','todo') NOT NULL;
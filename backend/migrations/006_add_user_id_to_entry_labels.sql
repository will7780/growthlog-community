-- Migration: Add user_id to entry_labels table
-- Purpose: Support user-specific custom labels
-- Date: 2026-03-30

-- Add user_id column to entry_labels
ALTER TABLE entry_labels
ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER sort_order,
ADD INDEX idx_entry_labels_user (user_id);

-- Add comment to the column
ALTER TABLE entry_labels
MODIFY COLUMN user_id BIGINT UNSIGNED NULL COMMENT '所属用户ID，NULL表示系统预设';

-- Note: Existing labels have user_id = NULL (system labels)
-- Custom labels will have user_id = their owner's user_id
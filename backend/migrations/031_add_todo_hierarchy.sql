-- Migration: 031_add_todo_hierarchy.sql
-- GrowthLog 11.9.0: immutable Todo parent chain, root + at most 3 subtask levels.
-- Idempotent. Existing rows remain roots (parent_id IS NULL).

USE growth_log;

SET @parent_col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND COLUMN_NAME = 'parent_id'
);

SET @parent_col_ddl := IF(
  @parent_col_exists = 0,
  'ALTER TABLE `todos` ADD COLUMN `parent_id` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT ''父任务；创建后不可修改'' AFTER `user_id`',
  'SELECT ''todos.parent_id already exists'' AS migration_031_column_noop'
);

PREPARE stmt_031_col FROM @parent_col_ddl;
EXECUTE stmt_031_col;
DEALLOCATE PREPARE stmt_031_col;

SET @owner_key_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND INDEX_NAME = 'uq_todos_id_user'
);

SET @owner_key_ddl := IF(
  @owner_key_exists = 0,
  'ALTER TABLE `todos` ADD UNIQUE KEY `uq_todos_id_user` (`id`, `user_id`)',
  'SELECT ''uq_todos_id_user already exists'' AS migration_031_owner_key_noop'
);

PREPARE stmt_031_owner_key FROM @owner_key_ddl;
EXECUTE stmt_031_owner_key;
DEALLOCATE PREPARE stmt_031_owner_key;

SET @tree_idx_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND INDEX_NAME = 'idx_todos_user_parent_created'
);

SET @tree_idx_ddl := IF(
  @tree_idx_exists = 0,
  'ALTER TABLE `todos` ADD INDEX `idx_todos_user_parent_created` (`user_id`, `parent_id`, `created_at`, `id`)',
  'SELECT ''idx_todos_user_parent_created already exists'' AS migration_031_tree_index_noop'
);

PREPARE stmt_031_tree_idx FROM @tree_idx_ddl;
EXECUTE stmt_031_tree_idx;
DEALLOCATE PREPARE stmt_031_tree_idx;

SET @parent_fk_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND CONSTRAINT_NAME = 'fk_todos_parent_owner'
);

SET @parent_fk_ddl := IF(
  @parent_fk_exists = 0,
  'ALTER TABLE `todos`
     ADD CONSTRAINT `fk_todos_parent_owner`
     FOREIGN KEY (`parent_id`, `user_id`)
     REFERENCES `todos` (`id`, `user_id`)
     ON DELETE CASCADE',
  'SELECT ''fk_todos_parent_owner already exists'' AS migration_031_parent_fk_noop'
);

PREPARE stmt_031_parent_fk FROM @parent_fk_ddl;
EXECUTE stmt_031_parent_fk;
DEALLOCATE PREPARE stmt_031_parent_fk;

-- Backward-compatible rollback policy:
-- do not drop the column during an image rollback. Older application versions ignore it.
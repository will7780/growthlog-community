-- Migration: 033_add_todo_sort_order.sql
-- GrowthLog 11.11.0: explicit sibling order for step-style subtasks.
-- Idempotent. Root task business attributes are never rewritten.

USE growth_log;

SET @sort_col_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND COLUMN_NAME = 'sort_order'
);

SET @sort_col_ddl := IF(
  @sort_col_exists = 0,
  'ALTER TABLE `todos` ADD COLUMN `sort_order` INT UNSIGNED NULL DEFAULT NULL COMMENT ''同一父任务下的子任务顺序；根任务为空'' AFTER `parent_id`',
  'SELECT ''todos.sort_order already exists'' AS migration_033_column_noop'
);

PREPARE stmt_033_col FROM @sort_col_ddl;
EXECUTE stmt_033_col;
DEALLOCATE PREPARE stmt_033_col;

SET @sort_idx_exists := (
  SELECT COUNT(*)
  FROM INFORMATION_SCHEMA.STATISTICS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'todos'
    AND INDEX_NAME = 'idx_todos_user_parent_sort'
);

-- The index is the completion marker. A rerun after an interrupted first run
-- recalculates child positions before installing the index.
SET @sort_tmp_reset := IF(
  @sort_idx_exists = 0,
  'DROP TEMPORARY TABLE IF EXISTS `tmp_todo_sort_033`',
  'SELECT ''todo child order temp table not needed'' AS migration_033_tmp_reset_noop'
);

PREPARE stmt_033_tmp_reset FROM @sort_tmp_reset;
EXECUTE stmt_033_tmp_reset;
DEALLOCATE PREPARE stmt_033_tmp_reset;

SET @sort_tmp_ddl := IF(
  @sort_idx_exists = 0,
  'CREATE TEMPORARY TABLE `tmp_todo_sort_033` ENGINE=InnoDB AS
   SELECT `id`, CAST(ROW_NUMBER() OVER (
     PARTITION BY `user_id`, `parent_id`
     ORDER BY
       `is_done` ASC,
       CASE WHEN `is_done` = 0 THEN `is_urgent` END DESC,
       CASE WHEN `is_done` = 0 THEN FIELD(UPPER(`priority`), ''P0'', ''P1'', ''P2'', ''P3'', ''P4'') END ASC,
       CASE WHEN `is_done` = 0 THEN (`due_date` IS NULL) END ASC,
       CASE WHEN `is_done` = 0 THEN `due_date` END ASC,
       CASE WHEN `is_done` = 1 THEN `completed_at` END DESC,
       `created_at` DESC,
       `id` DESC
   ) - 1 AS UNSIGNED) AS `new_sort_order`
   FROM `todos`
   WHERE `parent_id` IS NOT NULL',
  'SELECT ''todo child order already backfilled'' AS migration_033_tmp_noop'
);

PREPARE stmt_033_tmp FROM @sort_tmp_ddl;
EXECUTE stmt_033_tmp;
DEALLOCATE PREPARE stmt_033_tmp;

SET @sort_backfill_sql := IF(
  @sort_idx_exists = 0,
  'UPDATE `todos` AS target
   JOIN `tmp_todo_sort_033` AS ranked ON ranked.`id` = target.`id`
   SET target.`sort_order` = ranked.`new_sort_order`',
  'SELECT ''todo child order already backfilled'' AS migration_033_backfill_noop'
);

PREPARE stmt_033_backfill FROM @sort_backfill_sql;
EXECUTE stmt_033_backfill;
DEALLOCATE PREPARE stmt_033_backfill;

SET @sort_tmp_drop := IF(
  @sort_idx_exists = 0,
  'DROP TEMPORARY TABLE IF EXISTS `tmp_todo_sort_033`',
  'SELECT ''todo child order temp table not needed'' AS migration_033_tmp_drop_noop'
);

PREPARE stmt_033_tmp_drop FROM @sort_tmp_drop;
EXECUTE stmt_033_tmp_drop;
DEALLOCATE PREPARE stmt_033_tmp_drop;

SET @step_normalize_sql := IF(
  @sort_idx_exists = 0,
  'UPDATE `todos`
   SET `priority` = ''P4'', `is_urgent` = 0, `due_date` = NULL
   WHERE `parent_id` IS NOT NULL',
  'SELECT ''todo child attributes already normalized'' AS migration_033_normalize_noop'
);

PREPARE stmt_033_normalize FROM @step_normalize_sql;
EXECUTE stmt_033_normalize;
DEALLOCATE PREPARE stmt_033_normalize;

SET @sort_idx_ddl := IF(
  @sort_idx_exists = 0,
  'ALTER TABLE `todos` ADD INDEX `idx_todos_user_parent_sort` (`user_id`, `parent_id`, `sort_order`, `id`)',
  'SELECT ''idx_todos_user_parent_sort already exists'' AS migration_033_index_noop'
);

PREPARE stmt_033_idx FROM @sort_idx_ddl;
EXECUTE stmt_033_idx;
DEALLOCATE PREPARE stmt_033_idx;

-- Rollback policy: retain the nullable column/index. Older images ignore them.
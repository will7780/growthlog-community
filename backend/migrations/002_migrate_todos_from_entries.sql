-- =====================================================
-- 小要事数据迁移脚本
-- 将 entries 表中 label_code='todo' 的记录迁移到 todos 表
-- 创建日期: 2026-03-28
-- 版本: 0.5.0
-- =====================================================

-- 1. 迁移数据：插入 todos 表（默认截止日期为明天）
INSERT INTO todos (user_id, content, due_date, is_done, completed_at, created_at, updated_at)
SELECT 
    user_id,
    content,
    DATE_ADD(CURDATE(), INTERVAL 1 DAY) AS due_date,  -- 默认截止日期：明天
    FALSE AS is_done,                                  -- 默认未完成
    NULL AS completed_at,                             -- 未完成，无完成时间
    created_at,                                       -- 使用原记录创建时间
    NOW() AS updated_at                              -- 更新时间
FROM entries 
WHERE label_code = 'todo';

-- 2. 验证迁移结果
SELECT 
    (SELECT COUNT(*) FROM todos WHERE user_id IN (SELECT user_id FROM entries WHERE label_code = 'todo')) AS migrated_count,
    (SELECT COUNT(*) FROM entries WHERE label_code = 'todo') AS original_count;

-- =====================================================
-- 回滚脚本（如需回滚）
-- 注意：回滚前请确保 todos 表中没有其他新增数据
-- =====================================================
-- DELETE FROM todos WHERE created_at = updated_at AND is_done = FALSE;
-- =====================================================

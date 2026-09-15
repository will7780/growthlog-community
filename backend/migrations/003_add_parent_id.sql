-- =====================================================
-- 添加 parent_id 字段到 entries 表
-- 创建日期: 2026-03-28
-- 版本: 0.6.0
-- =====================================================

-- 1. 添加 parent_id 字段
ALTER TABLE `entries` ADD COLUMN `parent_id` BIGINT UNSIGNED NULL DEFAULT NULL COMMENT '父记录ID，顶级记录为NULL' AFTER `label_code`;

-- 2. 添加索引
ALTER TABLE `entries` ADD INDEX `idx_entries_parent` (`parent_id`);

-- 3. 添加外键约束（可选，建议生产环境添加）
-- ALTER TABLE `entries` ADD CONSTRAINT `fk_entries_parent` FOREIGN KEY (`parent_id`) REFERENCES `entries` (`id`) ON DELETE CASCADE;

-- =====================================================
-- 回滚脚本（如需回滚）
-- =====================================================
-- ALTER TABLE `entries` DROP COLUMN `parent_id`;
-- =====================================================

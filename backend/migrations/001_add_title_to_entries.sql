-- Migration: 001_add_title_to_entries.sql
-- 说明：为 entries 表添加 title 字段，用于记录标题
-- 执行方式：在 Navicat 中打开此文件，连接到 growth_log 数据库，执行

USE growth_log;

-- 检查 title 字段是否已存在
SELECT COUNT(*) as column_exists
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'growth_log'
  AND TABLE_NAME = 'entries'
  AND COLUMN_NAME = 'title';

-- 如果不存在，则添加 title 字段
ALTER TABLE `entries`
ADD COLUMN `title` VARCHAR(200) NOT NULL DEFAULT '' AFTER `label_code`;

-- 验证：查看表结构
DESCRIBE `entries`;

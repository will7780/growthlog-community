/**
 * 创建记录表单组件
 * 支持创建顶级记录和子记录（通过 parent_id）
 */
import { useEffect, useState, type FormEvent } from 'react';
import { joinContent } from '../../utils/parseContent';
import Input from '../common/Input';
import Textarea from '../common/Textarea';
import Button from '../common/Button';
import AttachmentPreviewList from './AttachmentPreviewList';
import type { LabelResponse } from '../../types/api';

interface SubmitOptions {
  parentId?: number | null;
  files?: File[];
}

interface AppendTarget {
  entryId: number;
  labelCode: string;
  title: string;
}

interface EntryFormProps {
  labels: LabelResponse[];
  onSubmit: (labelCode: string, content: string, options?: SubmitOptions) => Promise<void>;
  appendTarget?: AppendTarget | null;
  onCancelAppend?: () => void;
  loading?: boolean;
}

export default function EntryForm({
  labels,
  onSubmit,
  appendTarget = null,
  onCancelAppend,
  loading = false,
}: EntryFormProps) {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [selectedLabelCode, setSelectedLabelCode] = useState<string>('');
  const [files, setFiles] = useState<File[]>([]);
  const [error, setError] = useState('');

  // 进入"追加模式"时，自动带入目标记录的标签和标题
  useEffect(() => {
    if (appendTarget) {
      setSelectedLabelCode(appendTarget.labelCode);
      setTitle(appendTarget.title);
    }
  }, [appendTarget]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');

    if (!selectedLabelCode) {
      setError('请选择标签');
      return;
    }

    if (!content.trim()) {
      setError('请输入内容');
      return;
    }

    try {
      // 将 title 拼接到 content 第一行
      const fullContent = joinContent(title, content);
      await onSubmit(selectedLabelCode, fullContent, {
        parentId: appendTarget?.entryId ?? null,
        files,
      });
      // 追加模式下保留标题和标签，便于连续记录；普通模式清空全部
      if (appendTarget) {
        setContent('');
      } else {
        setTitle('');
        setContent('');
        setSelectedLabelCode('');
      }
      setFiles([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '创建失败，请重试');
    }
  };

  return (
    <form onSubmit={handleSubmit} className="growth-card growth-entry-form">
      <h2 className="growth-form-title">
        {appendTarget ? '追加记录' : '新建记录'}
      </h2>

      {appendTarget && (
        <div className="growth-append-notice">
          正在追加到：「{appendTarget.title}」
          <button
            type="button"
            onClick={onCancelAppend}
            className="growth-link-button"
          >
            取消
          </button>
        </div>
      )}

      <div className="growth-field">
        <label htmlFor="label" className="growth-field-label">
          标签 <span className="text-red-500">*</span>
        </label>
        {labels.length === 0 ? (
          <div className="growth-muted-text">加载中...</div>
        ) : (
          <select
            id="label"
            value={selectedLabelCode}
            onChange={(e) => setSelectedLabelCode(e.target.value)}
            className="growth-control"
            required
          >
            <option value="">请选择标签</option>
            {labels.filter(l => l.code !== 'todo').map((label) => (
              <option key={label.code} value={label.code}>
                {label.name}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="growth-field">
        <Input
          label="标题（可选）"
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="记录标题（可选）"
          className="growth-control"
        />
      </div>

      <div className="growth-field">
        <Textarea
          label="内容"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder={appendTarget ? '记录内容...' : '记录内容...'}
          required
          rows={6}
          className="growth-control growth-textarea"
        />
      </div>

      <div className="growth-field growth-attachment-field">
        <label htmlFor="attachments" className="growth-field-label">
          附件（可选）
        </label>
        <input
          id="attachments"
          type="file"
          multiple
          accept="image/jpeg,image/png,image/webp,application/pdf,application/vnd.openxmlformats-officedocument.presentationml.presentation,.jpg,.jpeg,.png,.webp,.pdf,.pptx"
          onChange={(e) => setFiles(Array.from(e.target.files || []))}
          className="growth-file-input"
        />
        <AttachmentPreviewList files={files} />
      </div>

      {error && (
        <div className="growth-error">
          {error}
        </div>
      )}

      <Button type="submit" loading={loading} className="growth-submit-button">
        {appendTarget ? '追加' : '提交'}
      </Button>
    </form>
  );
}

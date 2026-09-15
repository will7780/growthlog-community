import { BookOpen, NotebookPen, ListTodo, Paperclip } from 'lucide-react';

export default function ReferenceSourceBadge({ sourceType }: { sourceType: string }) {
  const notion = sourceType === 'notion_page';
  const todo = sourceType === 'todo';
  const attachment = sourceType === 'attachment' || sourceType === 'attachment_chunk';
  const Icon = notion ? BookOpen : todo ? ListTodo : attachment ? Paperclip : NotebookPen;
  const label = notion ? 'Notion' : todo ? '小要事' : attachment ? '附件' : '我的记录';
  return <span className={`reference-source-badge${notion ? ' reference-source-badge--notion' : ''}`}>
    <Icon size={14} aria-hidden="true" />{label}
  </span>;
}

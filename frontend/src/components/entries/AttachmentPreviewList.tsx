import { useEffect, useState } from 'react';

import { FileText } from 'lucide-react';

interface LocalPreview {
  key: string;
  name: string;
  size: number;
  type: string;
  url: string | null;
}

interface AttachmentPreviewListProps {
  files: File[];
  compact?: boolean;
}

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(jpg|jpeg|png|webp|gif)$/i.test(file.name);
}

function formatFileSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.max(1, Math.round(size / 1024))}KB`;
}

export default function AttachmentPreviewList({
  files,
  compact = false,
}: AttachmentPreviewListProps) {
  const [previews, setPreviews] = useState<LocalPreview[]>([]);

  useEffect(() => {
    const nextPreviews = files.map((file) => ({
      key: `${file.name}-${file.size}-${file.lastModified}`,
      name: file.name,
      size: file.size,
      type: file.type,
      url: isImageFile(file) ? URL.createObjectURL(file) : null,
    }));

    setPreviews(nextPreviews);

    return () => {
      nextPreviews.forEach((preview) => {
        if (preview.url) URL.revokeObjectURL(preview.url);
      });
    };
  }, [files]);

  if (previews.length === 0) return null;

  return (
    <div className={compact ? 'growth-upload-preview-list growth-upload-preview-list--compact' : 'growth-upload-preview-list'}>
      {previews.map((preview) => (
        <div key={preview.key} className="growth-upload-preview-card">
          {preview.url ? (
            <img
              src={preview.url}
              alt={preview.name}
              className="growth-upload-preview-image"
            />
          ) : (
            <div className="growth-upload-preview-file" aria-hidden="true">
              <FileText size={20} strokeWidth={1.5} />
            </div>
          )}
          <div className="growth-upload-preview-meta">
            <div className="growth-upload-preview-name" title={preview.name}>
              {preview.name}
            </div>
            <div className="growth-upload-preview-size">{formatFileSize(preview.size)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

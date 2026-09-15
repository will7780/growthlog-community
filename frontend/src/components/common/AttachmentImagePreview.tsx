import { useEffect, useId, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Maximize2, X } from 'lucide-react';
import { getAttachmentPreviewBlob, getAttachmentThumbnailBlob } from '../../api/attachments';

interface AttachmentImageThumbnailProps {
  attachmentId: number;
  filename: string;
  onOpen: () => void;
  className?: string;
}

interface AttachmentImageDialogProps {
  attachmentId: number | null;
  filename: string;
  open: boolean;
  onClose: () => void;
}

interface ImageRequestOptions {
  signal?: AbortSignal;
}

type ImageBlobLoader = (attachmentId: number, options?: ImageRequestOptions) => Promise<Blob>;

function useNearViewport(targetRef: RefObject<HTMLElement>): boolean {
  const [nearViewport, setNearViewport] = useState(false);

  useEffect(() => {
    if (nearViewport) return;
    const target = targetRef.current;
    if (!target) return;
    if (typeof IntersectionObserver === 'undefined') {
      setNearViewport(true);
      return;
    }

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry.isIntersecting) return;
        setNearViewport(true);
        observer.disconnect();
      },
      { rootMargin: '320px 0px' },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [nearViewport, targetRef]);

  return nearViewport;
}

function useImageBlob(
  attachmentId: number | null,
  enabled: boolean,
  loader: ImageBlobLoader,
) {
  const [url, setUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!enabled || attachmentId == null) {
      setUrl(null);
      setLoading(false);
      setError('');
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoading(true);
    setError('');

    loader(attachmentId, { signal: controller.signal })
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((reason) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : '图片暂时无法预览');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachmentId, enabled, loader]);

  return { url, loading, error };
}

export function AttachmentImageThumbnail({
  attachmentId,
  filename,
  onOpen,
  className = '',
}: AttachmentImageThumbnailProps) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const nearViewport = useNearViewport(buttonRef);
  const { url, loading, error } = useImageBlob(
    attachmentId,
    nearViewport,
    getAttachmentThumbnailBlob,
  );

  return (
    <button
      ref={buttonRef}
      type="button"
      className={`growth-auth-image-thumbnail ${className}`.trim()}
      onClick={onOpen}
      disabled={!url}
      data-image-load-state={url ? 'ready' : loading ? 'loading' : nearViewport ? 'error' : 'deferred'}
      title={url ? `查看原图：${filename}` : undefined}
      aria-label={`查看原图：${filename}`}
    >
      {url ? (
        <img src={url} alt={filename} loading="lazy" decoding="async" />
      ) : (
        <span>{loading ? '图片加载中...' : error || '图片待加载'}</span>
      )}
      {url && (
        <span className="growth-auth-image-thumbnail__action" aria-hidden="true">
          <Maximize2 size={15} />
          查看原图
        </span>
      )}
    </button>
  );
}

export function AttachmentImageDialog({
  attachmentId,
  filename,
  open,
  onClose,
}: AttachmentImageDialogProps) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const { url, loading, error } = useImageBlob(attachmentId, open, getAttachmentPreviewBlob);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.requestAnimationFrame(() => closeRef.current?.focus());

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onCloseRef.current();
      } else if (event.key === 'Tab') {
        event.preventDefault();
        closeRef.current?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocusRef.current?.focus();
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div
      className="growth-image-viewer__backdrop"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section
        className="growth-image-viewer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <header className="growth-image-viewer__header">
          <h2 id={titleId}>{filename}</h2>
          <button
            ref={closeRef}
            type="button"
            className="icon-button icon-button--md icon-button--ghost"
            aria-label="关闭原图"
            title="关闭原图"
            onClick={onClose}
          >
            <X aria-hidden="true" size={18} strokeWidth={2} />
          </button>
        </header>
        <div className="growth-image-viewer__canvas">
          {loading && <span role="status">正在加载原图...</span>}
          {error && <span role="alert">{error}</span>}
          {url && <img src={url} alt={filename} decoding="async" />}
        </div>
      </section>
    </div>,
    document.body,
  );
}

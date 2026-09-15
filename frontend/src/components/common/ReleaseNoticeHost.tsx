import { useEffect, useId, useRef, useState } from 'react';
import { Sparkles, X } from 'lucide-react';
import {
  getReleaseNotes,
  getRuntimeVersion,
  type PublicReleaseNotes,
} from '../../api/version';

interface ReleaseNoticeHostProps {
  userId: number;
}

const storageKey = (userId: number, version: string) =>
  `growthlog:release-notice:${userId}:${version}`;

function wasAcknowledged(userId: number, version: string): boolean {
  try {
    return window.localStorage.getItem(storageKey(userId, version)) === '1';
  } catch {
    return false;
  }
}

function rememberAcknowledged(userId: number, version: string): void {
  try {
    window.localStorage.setItem(storageKey(userId, version), '1');
  } catch {
    // A storage failure must not block use of the application.
  }
}

export default function ReleaseNoticeHost({ userId }: ReleaseNoticeHostProps) {
  const titleId = useId();
  const descId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [notes, setNotes] = useState<PublicReleaseNotes | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let timer = 0;
    let observer: MutationObserver | null = null;

    const showWhenNoOtherDialog = (payload: PublicReleaseNotes) => {
      const show = () => {
        if (wasAcknowledged(userId, payload.version)) {
          observer?.disconnect();
          observer = null;
          return true;
        }
        const activeModal = document.querySelector('[aria-modal="true"]');
        if (activeModal) return false;
        setNotes(payload);
        setOpen(true);
        observer?.disconnect();
        observer = null;
        return true;
      };

      timer = window.setTimeout(() => {
        if (show()) return;
        observer = new MutationObserver(() => {
          show();
        });
        observer.observe(document.body, { childList: true, subtree: true, attributes: true });
      }, 800);
    };

    void Promise.all([
      getRuntimeVersion(controller.signal),
      getReleaseNotes(controller.signal),
    ]).then(([runtimeVersion, releaseNotes]) => {
      if (
        !runtimeVersion
        || runtimeVersion === '0.0.0-dev'
        || !releaseNotes
        || releaseNotes.version !== runtimeVersion
        || wasAcknowledged(userId, runtimeVersion)
      ) {
        return;
      }
      showWhenNoOtherDialog(releaseNotes);
    });

    return () => {
      controller.abort();
      window.clearTimeout(timer);
      observer?.disconnect();
    };
  }, [userId]);

  useEffect(() => {
    if (!open || !notes) return;
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const focusTimer = window.setTimeout(() => closeRef.current?.focus(), 0);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        rememberAcknowledged(userId, notes.version);
        setOpen(false);
        return;
      }
      if (event.key !== 'Tab' || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [notes, open, userId]);

  if (!open || !notes) return null;

  const close = () => {
    rememberAcknowledged(userId, notes.version);
    setOpen(false);
  };

  return (
    <div className="release-notice__overlay" data-testid="release-notice-overlay">
      <div
        ref={dialogRef}
        className="release-notice"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descId}
        data-testid="release-notice-dialog"
      >
        <div className="release-notice__head">
          <div className="release-notice__icon" aria-hidden="true">
            <Sparkles size={20} />
          </div>
          <div className="release-notice__heading">
            <h2 id={titleId}>{notes.title}</h2>
            <span className="release-notice__version">版本 {notes.version}</span>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="release-notice__close"
            onClick={close}
            aria-label="关闭更新说明"
          >
            <X size={20} aria-hidden="true" />
          </button>
        </div>

        <p id={descId} className="release-notice__intro">
          本次更新内容
        </p>
        <ul className="release-notice__changes">
          {notes.changes.map((change) => (
            <li key={change}>{change}</li>
          ))}
        </ul>

        <div className="release-notice__actions">
          <button type="button" className="release-notice__confirm" onClick={close}>
            知道了
          </button>
        </div>
      </div>
    </div>
  );
}
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { LabelManager } from '../components/profile/LabelManager';
import NotionConnectionPanel from '../components/profile/NotionConnectionPanel';
import IconButton from '../components/common/IconButton';
import Header from '../components/layout/Header';
import { formatRuntimeVersion, getRuntimeVersion } from '../api/version';

export const ProfilePage = () => {
  const navigate = useNavigate();
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const [runtimeVersion, setRuntimeVersion] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void getRuntimeVersion(controller.signal).then((version) => {
      if (!controller.signal.aborted) setRuntimeVersion(version);
    });
    return () => controller.abort();
  }, []);

  const showSuccess = (message: string) => {
    setToast({ type: 'success', message });
    setTimeout(() => setToast(null), 3000);
  };

  const showError = (message: string) => {
    setToast({ type: 'error', message });
    setTimeout(() => setToast(null), 3000);
  };

  return (
    <div className="profile-page">
      <Header />
      <div className="profile-page__header">
        <div className="profile-page__header-inner">
          <IconButton
            icon={ArrowLeft}
            label="返回"
            variant="ghost"
            onClick={() => navigate('/app')}
          />
          <h1 className="profile-page__title">个人中心</h1>
          <span style={{ width: 36 }} aria-hidden="true" />
        </div>
      </div>

      <div className="profile-page__body">
        <div className="profile-card">
          <LabelManager onError={showError} onSuccess={showSuccess} />
        </div>

        <div className="profile-card" style={{ marginTop: 16 }}>
          <NotionConnectionPanel onError={showError} onSuccess={showSuccess} />
        </div>

        <section className="profile-about" aria-labelledby="profile-about-title">
          <div>
            <h2 id="profile-about-title" className="profile-about__title">关于 GrowthLog</h2>
            <p className="profile-about__subtitle">当前运行版本</p>
            <a href="https://github.com/will7780/growthlog-community/tree/v11.13.2-community.1" target="_blank" rel="noopener noreferrer">社区源码 · AGPL-3.0</a>
          </div>
          <output className="profile-about__version" aria-live="polite">
            {formatRuntimeVersion(runtimeVersion)}
          </output>
        </section>
      </div>

      {toast && (
        <div
          className={`growth-toast growth-toast--${toast.type}`}
          role="status"
        >
          {toast.message}
        </div>
      )}
    </div>
  );
};

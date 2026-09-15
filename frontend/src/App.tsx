/**
 * 根组件
 * 配置路由和认证保护
 */
import { useEffect, type ReactElement } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useNavigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { TodoProvider } from './contexts/TodoContext';
import TodayPlanReviewHost from './components/todos/TodayPlanReviewHost';
import ReleaseNoticeHost from './components/common/ReleaseNoticeHost';
import LoginPage from './pages/LoginPage';
import AppPage from './pages/AppPage';
import { ProfilePage } from './pages/ProfilePage';
import AdminPage from './pages/AdminPage';

function ProtectedRoute({ children }: { children: ReactElement }) {
  const { user, loading } = useAuth();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">加载中...</div>;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return children;
}

function AdminRoute({ children }: { children: ReactElement }) {
  const { user, loading } = useAuth();
  const navigate = useNavigate();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">加载中...</div>;
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (!user.is_admin) {
    return (
      <div className="admin-forbidden">
        <p className="admin-forbidden__text">无管理员权限</p>
        <button
          type="button"
          className="admin-btn admin-btn--ghost"
          onClick={() => navigate('/app')}
        >
          返回应用
        </button>
      </div>
    );
  }

  return children;
}

function AppRoutes() {
  const { user, loading } = useAuth();

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">加载中...</div>;
  }

  return (
    <>
      {user ? <ReleaseNoticeHost userId={user.id} /> : null}
      <Routes>
      <Route
        path="/login"
        element={user ? <Navigate to="/app" replace /> : <LoginPage />}
      />
      <Route
        path="/app"
        element={
          <ProtectedRoute>
            <TodoProvider>
              <TodayPlanReviewHost />
              <AppPage />
            </TodoProvider>
          </ProtectedRoute>
        }
      />
      <Route
        path="/profile"
        element={
          <ProtectedRoute>
            <ProfilePage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/admin"
        element={
          <AdminRoute>
            <AdminPage />
          </AdminRoute>
        }
      />
        <Route path="/" element={<Navigate to={user ? '/app' : '/login'} replace />} />
      </Routes>
    </>
  );
}

export default function App() {
  useEffect(() => {
    void import('./pwa/registerServiceWorker').then(({ registerServiceWorker }) => {
      void registerServiceWorker();
    });
  }, []);

  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}

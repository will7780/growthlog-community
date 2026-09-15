/**
 * 顶部导航栏 — 工作台风格
 */
import { Settings, User, LogOut } from 'lucide-react';
import { useAuth } from '../../contexts/AuthContext';
import { useNavigate } from 'react-router-dom';
import IconButton from '../common/IconButton';

export default function Header() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <header className="growth-header">
      <div className="growth-header-inner">
        <button type="button" className="growth-brand" onClick={() => navigate('/app')}>
          <span className="growth-brand-mark">G</span>
          <span className="growth-brand-text">Growth Log</span>
        </button>

        <nav className="growth-header-actions" aria-label="主导航">
          {user && (
            <>
              {user.is_admin === true && (
                <button
                  type="button"
                  onClick={() => navigate('/admin')}
                  className="growth-nav-pill"
                  title="管理后台"
                  aria-label="管理后台"
                >
                  <Settings size={16} aria-hidden="true" />
                  <span>管理</span>
                </button>
              )}
              <button
                type="button"
                onClick={() => navigate('/profile')}
                className="growth-nav-pill"
                title="个人中心"
                aria-label="个人中心"
              >
                <User size={16} aria-hidden="true" />
                <span>{user.username}</span>
              </button>
              <IconButton
                icon={LogOut}
                label="退出登录"
                onClick={handleLogout}
                variant="ghost"
              />
            </>
          )}
        </nav>
      </div>
    </header>
  );
}

/**
 * 登录页
 */
import { useState, useEffect, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Eye, EyeOff } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import Input from '../components/common/Input';
import Button from '../components/common/Button';
import IconButton from '../components/common/IconButton';

export default function LoginPage() {
  const navigate = useNavigate();
  const { login, user } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [loginSuccess, setLoginSuccess] = useState(false);

  const canSubmit = username.trim().length > 0 && password.length > 0 && !loading;

  useEffect(() => {
    if (loginSuccess && user) {
      const timer = setTimeout(() => {
        navigate('/app', { replace: true });
      }, 100);
      return () => clearTimeout(timer);
    }
  }, [loginSuccess, user, navigate]);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setError('');
    setLoading(true);

    try {
      if (!login) {
        throw new Error('login 函数不存在');
      }
      await login(username, password);
      setLoginSuccess(true);
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : '登录失败，请检查用户名和密码';
      setError(errorMsg);
      setLoginSuccess(false);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="growth-login-shell">
      <div className="growth-login-card">
        <div className="growth-login-brand">
          <span className="growth-brand-mark">G</span>
          <h1 className="growth-login-title">Growth Log</h1>
          <p className="growth-login-subtitle">把每天的成长，整理成可复盘的资产</p>
        </div>

        <form onSubmit={handleSubmit}>
          <Input
            label="用户名"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            autoFocus
            disabled={loading}
          />
          <div className="mb-4">
            <label htmlFor="login-password" className="block text-sm font-medium text-gray-700 mb-1">
              密码
            </label>
            <div className="growth-input-with-action">
              <input
                id="login-password"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                disabled={loading}
                className="w-full px-3 py-2 border rounded-md text-base min-h-[44px] wb-focus-visible border-[var(--wb-border)]"
                style={{ letterSpacing: 'var(--wb-letter-spacing)' }}
              />
              <span className="growth-input-action">
                <IconButton
                  icon={showPassword ? EyeOff : Eye}
                  label={showPassword ? '隐藏密码' : '显示密码'}
                  size="sm"
                  variant="ghost"
                  onClick={() => setShowPassword((v) => !v)}
                  disabled={loading}
                />
              </span>
            </div>
          </div>
          {error && <div className="growth-error mb-4">{error}</div>}
          <Button
            type="submit"
            loading={loading}
            disabled={!canSubmit}
            className="growth-submit-button w-full"
          >
            登录
          </Button>
        </form>
      </div>
    </div>
  );
}

/**
 * 认证 Context
 * 管理用户登录状态（禁止输出 token / 密码 / 完整认证响应）
 */
import { createContext, useContext, useState, useEffect } from 'react';
import type { ReactNode } from 'react';
import { login as apiLogin, getCurrentUser } from '../api/auth';
import { saveToken, clearToken } from '../api/client';
import type { UserResponse } from '../types/api';
import { clearTodayPlanReviewSession } from '../utils/todayPlanSession';

interface AuthContextType {
  user: UserResponse | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  init: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

function normalizeUser(user: UserResponse): UserResponse {
  return {
    ...user,
    is_admin: user.is_admin === true,
    is_active: user.is_active === true,
    can_edit_delete_own_entries: user.can_edit_delete_own_entries === true,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const init = async () => {
    try {
      const token = localStorage.getItem('token');
      if (token) {
        try {
          const userData = await getCurrentUser();
          setUser(normalizeUser(userData));
        } catch {
          clearToken();
          setUser(null);
        }
      }
    } catch {
      clearToken();
      setUser(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    init();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = async (username: string, password: string) => {
    clearTodayPlanReviewSession();
    const response = await apiLogin({ username, password });
    saveToken(response.access_token);
    setUser(normalizeUser(response.user));
    await new Promise((resolve) => setTimeout(resolve, 0));
  };

  const logout = () => {
    clearTodayPlanReviewSession();
    clearToken();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout, init }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}

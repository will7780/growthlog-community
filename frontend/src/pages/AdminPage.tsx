/**
 * 同站管理员用户管理页（紧凑工作台表格）
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import Header from '../components/layout/Header';
import { useAuth } from '../contexts/AuthContext';
import {
  createAdminUser,
  listAdminUsers,
  resetAdminUserPassword,
  updateAdminUser,
} from '../api/admin';
import type { UserResponse } from '../types/api';

function formatError(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return '请求失败';
}

function isAdminDenied(err: unknown): boolean {
  const status = (err as { status?: number } | null)?.status;
  const text = formatError(err);
  if (status === 403 && text.includes('需要管理员权限')) return true;
  return text.includes('需要管理员权限');
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

type PasswordDialogState =
  | { mode: 'reset'; user: UserResponse }
  | null;

export default function AdminPage() {
  const { init } = useAuth();
  const [users, setUsers] = useState<UserResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newPasswordConfirm, setNewPasswordConfirm] = useState('');
  const [openMenuId, setOpenMenuId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [passwordDialog, setPasswordDialog] = useState<PasswordDialogState>(null);
  const [resetPassword, setResetPassword] = useState('');
  const [resetPasswordConfirm, setResetPasswordConfirm] = useState('');
  const [resetError, setResetError] = useState<string | null>(null);
  const [resetSubmitting, setResetSubmitting] = useState(false);
  const resetInputRef = useRef<HTMLInputElement>(null);

  const enterForbidden = useCallback(async () => {
    setForbidden(true);
    setUsers([]);
    setError('当前账号无管理员权限');
    try {
      await init();
    } catch {
      /* ignore refresh errors */
    }
  }, [init]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setForbidden(false);
    try {
      const data = await listAdminUsers();
      setUsers(data.items);
    } catch (err) {
      if (isAdminDenied(err)) {
        await enterForbidden();
      } else {
        setError(formatError(err));
        setUsers([]);
      }
    } finally {
      setLoading(false);
    }
  }, [enterForbidden]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (passwordDialog) {
      setResetPassword('');
      setResetPasswordConfirm('');
      setResetError(null);
      const t = window.setTimeout(() => resetInputRef.current?.focus(), 0);
      return () => window.clearTimeout(t);
    }
  }, [passwordDialog]);

  async function runMutation(userId: number, fn: () => Promise<unknown>) {
    setBusyId(userId);
    setMessage(null);
    setError(null);
    try {
      await fn();
      await load();
      setMessage('已更新');
    } catch (err) {
      if (isAdminDenied(err)) {
        await enterForbidden();
      } else {
        setError(formatError(err));
      }
    } finally {
      setBusyId(null);
      setOpenMenuId(null);
    }
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    if (newPassword !== newPasswordConfirm) {
      setError('两次输入的密码不一致');
      return;
    }
    setCreating(true);
    setError(null);
    setMessage(null);
    try {
      await createAdminUser({ username: newUsername.trim(), password: newPassword });
      setNewUsername('');
      setNewPassword('');
      setNewPasswordConfirm('');
      setMessage('用户已创建');
      await load();
    } catch (err) {
      if (isAdminDenied(err)) {
        await enterForbidden();
      } else {
        setError(formatError(err));
      }
    } finally {
      setCreating(false);
    }
  }

  async function submitResetPassword(e: FormEvent) {
    e.preventDefault();
    if (!passwordDialog) return;
    if (!resetPassword.trim()) {
      setResetError('密码不能为空');
      return;
    }
    if (resetPassword !== resetPasswordConfirm) {
      setResetError('两次输入的密码不一致');
      return;
    }
    const target = passwordDialog.user;
    setResetSubmitting(true);
    setBusyId(target.id);
    setResetError(null);
    setError(null);
    try {
      await resetAdminUserPassword(target.id, { new_password: resetPassword });
      setPasswordDialog(null);
      setResetPassword('');
      setResetPasswordConfirm('');
      setMessage('已更新');
      await load();
    } catch (err) {
      if (isAdminDenied(err)) {
        setPasswordDialog(null);
        await enterForbidden();
      } else {
        setResetError(formatError(err));
      }
    } finally {
      setResetSubmitting(false);
      setBusyId(null);
      setOpenMenuId(null);
    }
  }

  return (
    <div className="admin-page">
      <Header />
      <main className="admin-page__main">
        <div className="admin-toolbar">
          <h1 className="admin-toolbar__title">用户管理</h1>
        </div>

        {forbidden && (
          <div className="admin-alert admin-alert--warn" role="alert">
            无管理员权限
          </div>
        )}
        {error && !forbidden && (
          <div className="admin-alert admin-alert--error" role="alert">
            {error}
          </div>
        )}
        {message && !forbidden && (
          <div className="admin-alert admin-alert--ok" role="status">
            {message}
          </div>
        )}

        {!forbidden && (
          <form onSubmit={handleCreate} className="admin-create-form">
            <input
              className="admin-input"
              placeholder="新用户名"
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              autoComplete="off"
              required
            />
            <input
              className="admin-input"
              placeholder="初始密码"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
              required
            />
            <input
              className="admin-input"
              placeholder="确认密码"
              type="password"
              value={newPasswordConfirm}
              onChange={(e) => setNewPasswordConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
            <button type="submit" disabled={creating} className="admin-btn admin-btn--primary">
              {creating ? '创建中…' : '创建用户'}
            </button>
          </form>
        )}

        {!forbidden && (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>启用</th>
                  <th>管理员</th>
                  <th>原记录删改</th>
                  <th className="admin-table__col-time">创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {loading && (
                  <tr>
                    <td colSpan={6} className="admin-table__empty">
                      加载中…
                    </td>
                  </tr>
                )}
                {!loading && users.length === 0 && (
                  <tr>
                    <td colSpan={6} className="admin-table__empty">
                      暂无用户
                    </td>
                  </tr>
                )}
                {!loading &&
                  users.map((user) => {
                    const busy = busyId === user.id;
                    const canMutate = user.can_edit_delete_own_entries === true;
                    return (
                      <tr key={user.id}>
                        <td className="admin-table__user">{user.username}</td>
                        <td>{user.is_active ? '是' : '否'}</td>
                        <td>{user.is_admin ? '是' : '否'}</td>
                        <td>
                          <label className="admin-perm-toggle">
                            <input
                              type="checkbox"
                              checked={canMutate}
                              disabled={busy}
                              aria-label={`允许删改原记录：${user.username}`}
                              onChange={() =>
                                void runMutation(user.id, () =>
                                  updateAdminUser(user.id, {
                                    can_edit_delete_own_entries: !canMutate,
                                  })
                                )
                              }
                            />
                            <span>{canMutate ? '开' : '关'}</span>
                          </label>
                        </td>
                        <td className="admin-table__col-time">{formatTime(user.created_at)}</td>
                        <td className="admin-table__actions">
                          <div className="admin-actions-desktop">
                            <button
                              type="button"
                              disabled={busy}
                              title={user.is_active ? '停用' : '启用'}
                              className="admin-btn admin-btn--ghost"
                              onClick={() =>
                                void runMutation(user.id, () =>
                                  updateAdminUser(user.id, { is_active: !user.is_active })
                                )
                              }
                            >
                              {user.is_active ? '停用' : '启用'}
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              title={user.is_admin ? '撤销管理员' : '授予管理员'}
                              className="admin-btn admin-btn--ghost"
                              onClick={() =>
                                void runMutation(user.id, () =>
                                  updateAdminUser(user.id, { is_admin: !user.is_admin })
                                )
                              }
                            >
                              {user.is_admin ? '撤权' : '授权'}
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              title="重置密码"
                              className="admin-btn admin-btn--ghost"
                              onClick={() => setPasswordDialog({ mode: 'reset', user })}
                            >
                              重置密码
                            </button>
                          </div>
                          <div className="admin-actions-mobile">
                            <button
                              type="button"
                              className="admin-btn admin-btn--ghost"
                              title="更多操作"
                              aria-label="更多操作"
                              onClick={() =>
                                setOpenMenuId((id) => (id === user.id ? null : user.id))
                              }
                            >
                              操作
                            </button>
                            {openMenuId === user.id && (
                              <div className="admin-menu">
                                <button
                                  type="button"
                                  disabled={busy}
                                  className="admin-menu__item"
                                  onClick={() =>
                                    void runMutation(user.id, () =>
                                      updateAdminUser(user.id, {
                                        is_active: !user.is_active,
                                      })
                                    )
                                  }
                                >
                                  {user.is_active ? '停用' : '启用'}
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  className="admin-menu__item"
                                  onClick={() =>
                                    void runMutation(user.id, () =>
                                      updateAdminUser(user.id, {
                                        is_admin: !user.is_admin,
                                      })
                                    )
                                  }
                                >
                                  {user.is_admin ? '撤权' : '授权'}
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  className="admin-menu__item"
                                  onClick={() =>
                                    void runMutation(user.id, () =>
                                      updateAdminUser(user.id, {
                                        can_edit_delete_own_entries: !canMutate,
                                      })
                                    )
                                  }
                                >
                                  {canMutate ? '关闭原记录删改' : '开启原记录删改'}
                                </button>
                                <button
                                  type="button"
                                  disabled={busy}
                                  className="admin-menu__item"
                                  onClick={() => setPasswordDialog({ mode: 'reset', user })}
                                >
                                  重置密码
                                </button>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        )}
      </main>

      {passwordDialog && (
        <div
          className="admin-dialog-backdrop"
          role="presentation"
          onClick={() => {
            if (!resetSubmitting) setPasswordDialog(null);
          }}
        >
          <div
            className="admin-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-reset-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="admin-reset-title" className="admin-dialog__title">
              重置密码 · {passwordDialog.user.username}
            </h2>
            <form onSubmit={submitResetPassword} className="admin-dialog__form">
              <label className="admin-dialog__label">
                新密码
                <input
                  ref={resetInputRef}
                  className="admin-input"
                  type="password"
                  value={resetPassword}
                  onChange={(e) => setResetPassword(e.target.value)}
                  autoComplete="new-password"
                  required
                />
              </label>
              <label className="admin-dialog__label">
                确认密码
                <input
                  className="admin-input"
                  type="password"
                  value={resetPasswordConfirm}
                  onChange={(e) => setResetPasswordConfirm(e.target.value)}
                  autoComplete="new-password"
                  required
                />
              </label>
              {resetError && (
                <div className="admin-alert admin-alert--error" role="alert">
                  {resetError}
                </div>
              )}
              <div className="admin-dialog__actions">
                <button
                  type="button"
                  className="admin-btn admin-btn--ghost"
                  disabled={resetSubmitting}
                  onClick={() => setPasswordDialog(null)}
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="admin-btn admin-btn--primary"
                  disabled={resetSubmitting}
                >
                  {resetSubmitting ? '提交中…' : '确认重置'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * 管理员用户管理 API（JWT 管理员；不含 ADMIN_TOKEN）
 */
import { get, patch, post } from './client';
import type {
  AdminCreateUserRequest,
  AdminResetPasswordRequest,
  AdminUpdateUserRequest,
  AdminUserListResponse,
  UserResponse,
} from '../types/api';

export async function listAdminUsers(username?: string): Promise<AdminUserListResponse> {
  const q = username?.trim()
    ? `?username=${encodeURIComponent(username.trim())}`
    : '';
  return get<AdminUserListResponse>(`/api/admin/users${q}`);
}

export async function createAdminUser(body: AdminCreateUserRequest): Promise<UserResponse> {
  return post<UserResponse>('/api/admin/users', body);
}

export async function updateAdminUser(
  userId: number,
  body: AdminUpdateUserRequest
): Promise<UserResponse> {
  return patch<UserResponse>(`/api/admin/users/${userId}`, body);
}

export async function resetAdminUserPassword(
  userId: number,
  body: AdminResetPasswordRequest
): Promise<UserResponse> {
  return post<UserResponse>(`/api/admin/users/${userId}/reset-password`, body);
}

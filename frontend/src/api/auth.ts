/**
 * 认证相关 API
 */
import { get, post } from './client';
import type { LoginRequest, LoginResponse, UserResponse } from '../types/api';

/**
 * 用户登录
 */
export async function login(data: LoginRequest): Promise<LoginResponse> {
  return post<LoginResponse>('/api/auth/login', data);
}

/**
 * 获取当前用户信息
 */
export async function getCurrentUser(): Promise<UserResponse> {
  return get<UserResponse>('/api/auth/me');
}

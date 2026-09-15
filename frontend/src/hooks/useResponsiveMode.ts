/**
 * 桌面 / 移动布局模式（与 CSS 断点 768px 对齐）。
 */
import { useMediaQuery } from './useMediaQuery';

export type ResponsiveMode = 'mobile' | 'desktop';

/** max-width: 768px → mobile；与现有 @media (max-width: 768px) 一致 */
export const MOBILE_MEDIA_QUERY = '(max-width: 768px)';

export function useResponsiveMode(): ResponsiveMode {
  const isMobile = useMediaQuery(MOBILE_MEDIA_QUERY);
  return isMobile ? 'mobile' : 'desktop';
}

export function useIsMobile(): boolean {
  return useResponsiveMode() === 'mobile';
}

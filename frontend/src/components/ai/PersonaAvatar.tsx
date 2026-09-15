/**
 * 统一角色头像（禁止 emoji / CDN 图片，使用本地生成的 PNG）。
 */
import { personaMeta } from './personaMeta';
import type { Persona } from '../../api/ai';

interface PersonaAvatarProps {
  persona: Persona | null | undefined;
  size?: number;
  className?: string;
}

export default function PersonaAvatar({ persona, size = 32, className = '' }: PersonaAvatarProps) {
  const meta = personaMeta(persona);
  if (!meta) {
    return (
      <div
        className={`persona-avatar persona-avatar--placeholder ${className}`}
        style={{ width: size, height: size }}
        aria-hidden="true"
      />
    );
  }
  return (
    <img
      src={meta.avatar}
      alt={meta.label}
      className={`persona-avatar ${className}`}
      data-testid={`persona-avatar-${persona}`}
      style={{ width: size, height: size }}
      draggable={false}
    />
  );
}

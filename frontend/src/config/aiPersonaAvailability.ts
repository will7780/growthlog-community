import type { Persona } from '../api/ai';

export const AI_PERSONA_AVAILABILITY: Record<Persona, boolean> = {
  retriever: true,
  explainer: true,
  organizer: false,
};

export function isPersonaAvailable(persona: Persona | null | undefined): boolean {
  return Boolean(persona && AI_PERSONA_AVAILABILITY[persona]);
}

export const ORGANIZER_MAINTENANCE_MESSAGE =
  '整理鼠正在维护，暂时不能创建新会话或发送消息。历史整理结果仍可查看。';

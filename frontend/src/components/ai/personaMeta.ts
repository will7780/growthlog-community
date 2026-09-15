/**
 * 三角色共享元数据（头像 / 标签 / 简介）。
 *
 * PersonaPicker、会话列表、聊天头部都从这里取数据，避免在多处重复写死
 * 中文文案和图片路径。
 */
import type { Persona } from '../../api/ai';
import retrieverAvatar from '../../assets/personas/retriever.svg';
import explainerAvatar from '../../assets/personas/explainer.svg';
import organizerAvatar from '../../assets/personas/organizer.svg';

export interface PersonaMetaInfo {
  persona: Persona;
  label: string;
  avatar: string;
  tagline: string;
  placeholder: string;
  emptyHint: string;
}

export const PERSONA_META: Record<Persona, PersonaMetaInfo> = {
  retriever: {
    persona: 'retriever',
    label: '检索鼠',
    avatar: retrieverAvatar,
    tagline: '钻进记录堆，快速翻出带出处的答案',
    placeholder: '问问检索鼠……',
    emptyHint: '例如：我之前有没有记过关于项目管理的笔记？',
  },
  explainer: {
    persona: 'explainer',
    label: '讲解鼠',
    avatar: explainerAvatar,
    tagline: '先咬定来源，再慢慢讲透，还能继续追问',
    placeholder: '想请讲解鼠讲清楚哪件事？',
    emptyHint: '例如：帮我讲清楚上个月项目复盘的结论。',
  },
  organizer: {
    persona: 'organizer',
    label: '整理鼠',
    avatar: organizerAvatar,
    tagline: '按范围收拾记录，叼出可确认的整合建议',
    placeholder: '跟整理鼠说说想怎么收拾……',
    emptyHint: '例如：把最近关于项目推进的记录整理成行动线索。',
  },
};

export const PERSONA_ORDER: Persona[] = ['retriever', 'explainer', 'organizer'];

export function personaMeta(persona: Persona | null | undefined): PersonaMetaInfo | null {
  if (!persona) return null;
  return PERSONA_META[persona] ?? null;
}

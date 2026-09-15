/**
 * R11.3 三角色共享会话/流式状态机。
 *
 * 唯一持有检索员/讲解员 streaming 业务逻辑的地方 —— AIAssistantPanel（桌面）
 * 和 AIMPContent（移动）都只消费这个 hook，不重复实现请求/解析/幂等/中断逻辑。
 *
 * 角色语义（见 docs/00-主设计与升级计划.md §4）：
 * - 会话创建后 persona 不可变，新对话必须先选角色。
 * - 历史会话（无 persona）只读展示，不可继续，只能删除。
 * - 检索员/讲解员使用真实 provider streaming；整理师沿用既有
 *   preview → diff → confirm 非流式语义（由 OrganizeControls 承担）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  cancelSourceSetProposal,
  compactChatSession,
  confirmSourceSetProposal,
  createPersonaSession,
  deleteChatSession,
  expandSourceSet,
  getChatHistory,
  getCurrentLockedSources,
  getPendingProposal,
  listPersonaSessions,
  sendChatMessage,
  type ChatMessage,
  type ChatSession,
  type CurrentLockedSourcesResponse,
  type OrganizeDays,
  type OrganizePreviewResponse,
  type PendingProposalResponse,
  type Persona,
} from '../api/ai';
import type { AgentAnalysis, AgentStep, ReferenceItem } from '../types/api';
import type { ReviewPreviewResponse } from '../api/ai';
import {
  generateRequestId,
  streamExplainerResume,
  streamExplainerTurn,
  streamRetrieverTurn,
  type PersonaStreamEvent,
  type StreamCandidateItem,
  type StreamReferenceItem,
} from '../api/aiStream';
import {
  markExplicitNewAiChat,
  pickResumeSessionId,
  setLastAiSessionId,
} from '../utils/aiSessionStorage';
import { aiErrorMessage } from '../utils/aiSafeErrors';
import {
  isPersonaAvailable,
  ORGANIZER_MAINTENANCE_MESSAGE,
} from '../config/aiPersonaAvailability';

let idSeq = 0;
function nextId(prefix: string): string {
  idSeq += 1;
  return `${prefix}-${idSeq}-${Date.now().toString(36)}`;
}

/** provider / 内部错误 → 安全用户文案（与旧面板保持一致的脱敏规则）。 */
function toSafeUserError(raw: string, code?: string | null): string {
  if (code) {
    const mapped = aiErrorMessage(code, raw);
    if (mapped) return mapped;
  }
  if (!raw || raw === '[object Object]') {
    return 'AI 服务暂时不可用，请稍后重试';
  }
  if (raw.includes('INSUFFICIENT_ENTRIES') || raw.includes('记录数量太少')) {
    return '记录数量太少啦，要先记多一点噢';
  }
  if (
    /openai|anthropic|azure|gemini|api[_\s-]?key|timeout|429|500|502|503|504|rate limit|provider|upstream|connection refused/i.test(
      raw,
    )
  ) {
    return 'AI 服务暂时不可用，请稍后重试';
  }
  return aiErrorMessage(null, raw);
}

function statusLabel(status: string): string {
  switch (status) {
    case 'retrieving':
      return '正在查找相关记录…';
    case 'awaiting_source_confirm':
      return '请确认要讲解的来源';
    case 'awaiting_expansion_confirm':
      return '请确认新增来源';
    case 'generating':
      return '正在生成回答…';
    case 'grounding_check':
      return '正在校验引用…';
    default:
      return '处理中…';
  }
}

export interface LegacyMessage {
  id: string;
  kind: 'legacy';
  role: 'user' | 'assistant';
  content: string;
  createdAt?: string | null;
  references?: ReferenceItem[];
  organizePreview?: OrganizePreviewResponse;
  /** 仅用于回放旧版 agent/query 模式历史消息，保持展示完整度。 */
  agentSteps?: AgentStep[];
  analysis?: AgentAnalysis;
  reviewPreview?: ReviewPreviewResponse;
}

export interface StreamChatMessage {
  id: string;
  kind: 'stream';
  role: 'user' | 'assistant';
  content: string;
  createdAt?: string | null;
  references?: StreamReferenceItem[];
  pending?: boolean;
  requestId?: string;
}

export interface SourceCandidateMessage {
  id: string;
  kind: 'source_candidates';
  role: 'assistant';
  message: string;
  candidates: StreamCandidateItem[];
  proposalId: number;
  baseVersion: number;
  phase: 'awaiting_source_confirm' | 'awaiting_expansion_confirm';
  /** 与此候选绑定的待处理用户消息 id，取消时一并从视图移除。 */
  userMessageId: string;
  /** Canonical request_id for post-confirm resume (must be reused, never regenerated). */
  requestId?: string;
  /** 补充来源主题（仅 expansion 阶段；安全展示文本）。 */
  expansionTopic?: string;
  resolution?: 'confirmed' | 'cancelled';
  actionPending?: boolean;
  actionError?: string;
}

export type PersonaMessage = LegacyMessage | StreamChatMessage | SourceCandidateMessage;

export interface SessionMetaView {
  sessionId: string;
  persona: Persona | null;
  title?: string | null;
  legacy: boolean;
  continuable: boolean;
  currentSourceSetVersion?: number | null;
  sourceSetStatus?: string | null;
  stale?: boolean;
}

type ViewMode = 'personaSelect' | 'chat';

interface OrganizerExtras {
  days?: OrganizeDays;
  labelCodes?: string[];
  entryIds?: number[];
  allowedActions?: string[];
}

const ORGANIZE_STAGE_SEQUENCE = ['scope', 'extract', 'summarize', 'validate'] as const;

const NEAR_BOTTOM_THRESHOLD_PX = 96;

export function usePersonaChat() {
  const [view, setView] = useState<ViewMode>('personaSelect');
  const [persona, setPersona] = useState<Persona | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionMeta, setSessionMeta] = useState<SessionMetaView | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [messages, setMessages] = useState<PersonaMessage[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [activeReferencePreview, setActiveReferencePreview] = useState<StreamReferenceItem | null>(null);
  const [sessionCreating, setSessionCreating] = useState(false);
  const [lockedSources, setLockedSources] = useState<CurrentLockedSourcesResponse | null>(null);
  const [lockedSourcesLoading, setLockedSourcesLoading] = useState(false);
  const [lockedSourcesError, setLockedSourcesError] = useState('');
  const [expandingSources, setExpandingSources] = useState(false);
  const [expandTopicOpen, setExpandTopicOpen] = useState(false);
  const [expandTopicDraft, setExpandTopicDraft] = useState('');
  const [expandTopicError, setExpandTopicError] = useState('');
  const [organizeStage, setOrganizeStage] = useState<string | null>(null);
  const [lastFailedSend, setLastFailedSend] = useState<{
    text: string;
    extras: OrganizerExtras;
  } | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const organizeStageTimerRef = useRef<number | null>(null);
  const isNearBottomRef = useRef(true);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const initialisedRef = useRef(false);

  const clearLockedSources = useCallback(() => {
    setLockedSources(null);
    setLockedSourcesError('');
    setLockedSourcesLoading(false);
    setExpandingSources(false);
  }, []);

  const refreshLockedSources = useCallback(async (sid?: string | null) => {
    const target = sid ?? sessionId;
    if (!target) {
      clearLockedSources();
      return;
    }
    setLockedSourcesLoading(true);
    setLockedSourcesError('');
    try {
      const data = await getCurrentLockedSources(target);
      setLockedSources(data);
      if (data.stale) {
        setSessionMeta((prev) => (prev ? { ...prev, stale: true } : prev));
      }
    } catch (err) {
      setLockedSources(null);
      setLockedSourcesError(toSafeUserError(err instanceof Error ? err.message : '加载固定来源失败'));
    } finally {
      setLockedSourcesLoading(false);
    }
  }, [clearLockedSources, sessionId]);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = messagesScrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  const onMessagesScroll = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    isNearBottomRef.current = distance < NEAR_BOTTOM_THRESHOLD_PX;
  }, []);

  useEffect(() => {
    if (isNearBottomRef.current) scrollToBottom();
  }, [messages, isStreaming, streamingStatus, scrollToBottom]);

  const refreshSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const data = await listPersonaSessions();
      setSessions(data.sessions || []);
      return data.sessions || [];
    } catch {
      return [] as ChatSession[];
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const loadHistory = useCallback(async (sid: string, meta: SessionMetaView) => {
    setHistoryLoading(true);
    try {
      const data = await getChatHistory(sid);
      const raw = data.messages || [];
      const mapped: PersonaMessage[] = raw.map((msg) => mapHistoryMessage(msg, meta));

      // 断线恢复：若仍有待确认 proposal，补回候选卡（避免隐藏 pending）。
      if (meta.persona === 'explainer') {
        try {
          const pending = await getPendingProposal(sid);
          if (pending.pending && pending.proposal_id && Array.isArray(pending.candidates)) {
            const userMsgId = nextId('u');
            const restored: PersonaMessage[] = [...mapped];
            if (pending.pending_user_content) {
              const already = restored.some(
                (m) => m.kind === 'stream' && m.role === 'user' && m.content === pending.pending_user_content,
              );
              if (!already) {
                restored.push({
                  id: userMsgId,
                  kind: 'stream',
                  role: 'user',
                  content: pending.pending_user_content,
                  requestId: pending.request_id || undefined,
                });
              }
            }
            const hasCard = restored.some(
              (m) => m.kind === 'source_candidates' && m.proposalId === pending.proposal_id,
            );
            if (!hasCard) {
              restored.push({
                id: nextId('c'),
                kind: 'source_candidates',
                role: 'assistant',
                message: pending.message || '',
                candidates: pending.candidates as StreamCandidateItem[],
                proposalId: Number(pending.proposal_id),
                baseVersion: Number(pending.base_version || 0),
                phase: (pending.phase as SourceCandidateMessage['phase']) || 'awaiting_source_confirm',
                userMessageId: userMsgId,
                requestId: pending.request_id || undefined,
                expansionTopic: pending.expansion_topic || undefined,
              });
            }
            setMessages(restored);
            void refreshLockedSources(sid);
            return;
          }
        } catch {
          /* ignore pending recovery failure; history still shown */
        }
        void refreshLockedSources(sid);
      } else {
        clearLockedSources();
      }
      setMessages(mapped);
    } catch {
      setMessages([]);
    } finally {
      setHistoryLoading(false);
    }
  }, [clearLockedSources, refreshLockedSources]);

  const metaFromSession = useCallback((s: ChatSession): SessionMetaView => ({
    sessionId: s.session_id,
    persona: (s.persona as Persona | null) ?? null,
    title: s.title ?? null,
    legacy: Boolean(s.legacy) || s.persona == null,
    continuable: s.persona == null ? false : s.continuable !== false,
    currentSourceSetVersion: s.current_source_set_version ?? null,
    sourceSetStatus: s.source_set_status ?? null,
    stale: s.source_set_status === 'stale',
  }), []);

  const selectSession = useCallback((sid: string, list: ChatSession[] = sessions) => {
    const found = list.find((s) => s.session_id === sid);
    const meta: SessionMetaView = found
      ? metaFromSession(found)
      : { sessionId: sid, persona: null, legacy: true, continuable: false };
    setLastAiSessionId(sid);
    setSessionId(sid);
    setPersona(meta.persona);
    setSessionMeta(meta);
    setView('chat');
    setError('');
    setNotice('');
    setActiveReferencePreview(null);
    if (meta.persona !== 'explainer') clearLockedSources();
    void loadHistory(sid, meta);
  }, [clearLockedSources, loadHistory, metaFromSession, sessions]);

  useEffect(() => {
    if (initialisedRef.current) return;
    initialisedRef.current = true;
    (async () => {
      const list = await refreshSessions();
      const resumeId = pickResumeSessionId(list);
      if (resumeId) {
        selectSession(resumeId, list);
      } else {
        setView('personaSelect');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const startNewChat = useCallback(() => {
    markExplicitNewAiChat();
    stopStreamingInternal();
    setSessionId(null);
    setSessionMeta(null);
    setPersona(null);
    setMessages([]);
    setError('');
    setNotice('');
    setActiveReferencePreview(null);
    clearLockedSources();
    setView('personaSelect');
  }, [clearLockedSources]);

  const createSessionForPersona = useCallback(async (nextPersona: Persona, title?: string) => {
    if (!isPersonaAvailable(nextPersona)) {
      setError(ORGANIZER_MAINTENANCE_MESSAGE);
      return;
    }
    setSessionCreating(true);
    setError('');
    try {
      const created = await createPersonaSession(nextPersona, title);
      const meta: SessionMetaView = {
        sessionId: created.session_id,
        persona: created.persona,
        title: created.title ?? null,
        legacy: false,
        continuable: true,
        currentSourceSetVersion: null,
        sourceSetStatus: null,
      };
      setLastAiSessionId(created.session_id);
      setSessionId(created.session_id);
      setSessionMeta(meta);
      setPersona(created.persona);
      setMessages([]);
      clearLockedSources();
      setView('chat');
      void refreshSessions();
    } catch (err) {
      setError(toSafeUserError(err instanceof Error ? err.message : '创建会话失败'));
    } finally {
      setSessionCreating(false);
    }
  }, [clearLockedSources, refreshSessions]);

  const deleteSession = useCallback(async (sid: string) => {
    await deleteChatSession(sid);
    if (sessionId === sid) {
      startNewChat();
    }
    await refreshSessions();
  }, [refreshSessions, sessionId, startNewChat]);

  function stopStreamingInternal() {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setIsStreaming(false);
    setStreamingStatus(null);
  }

  const stopStreaming = useCallback(() => {
    stopStreamingInternal();
  }, []);

  const removeMessage = useCallback((id: string) => {
    setMessages((prev) => prev.filter((m) => m.id !== id));
  }, []);

  const upsertMessage = useCallback((id: string, updater: (prev: PersonaMessage | undefined) => PersonaMessage) => {
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.id === id);
      if (idx === -1) return [...prev, updater(undefined)];
      const next = [...prev];
      next[idx] = updater(prev[idx]);
      return next;
    });
  }, []);

  const handleOrganizeSaved = useCallback((count: number) => {
    setNotice(`已保存 ${count} 条整合结果`);
  }, []);

  const clearError = useCallback(() => setError(''), []);
  const clearNotice = useCallback(() => setNotice(''), []);

  // ---------------------------------------------------------------------
  // 检索员 / 讲解员：共享 streaming 发送逻辑
  // ---------------------------------------------------------------------
  const sendStreamingMessage = useCallback(async (
    activePersona: 'retriever' | 'explainer',
    sid: string,
    text: string,
    reuseRequestId?: string,
  ) => {
    const userMsgId = nextId('u');
    setMessages((prev) => [
      ...prev,
      { id: userMsgId, kind: 'stream', role: 'user', content: text, createdAt: new Date().toISOString() },
    ]);

    const assistantMsgId = nextId('a');
    const controller = new AbortController();
    abortRef.current = controller;
    setIsStreaming(true);
    setStreamingStatus(null);
    setError('');
    setLastFailedSend(null);

    // 网络重试必须复用原 request_id，禁止静默生成新 ID。
    const requestId = reuseRequestId || generateRequestId();
    let userMessagePersisted = false;
    let sawAnyDelta = false;
    let sawDone = false;
    let sawError = false;
    const candidateAccum: StreamCandidateItem[] = [];
    const collectedRefs: StreamReferenceItem[] = [];

    const generator = activePersona === 'retriever'
      ? streamRetrieverTurn(sid, text, { requestId, signal: controller.signal })
      : streamExplainerTurn(sid, text, { requestId, signal: controller.signal });

    try {
      for await (const evt of generator as AsyncGenerator<PersonaStreamEvent>) {
        if (controller.signal.aborted) break;
        applyStreamEvent(evt);
      }
    } catch {
      if (!controller.signal.aborted) {
        setError('AI 服务暂时不可用，请稍后重试');
        setLastFailedSend({ text, extras: {} });
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setIsStreaming(false);
      setStreamingStatus(null);
      if (!sawDone) {
        removeMessage(assistantMsgId);
        if (sawAnyDelta && !sawError) {
          setNotice('已停止，未保存');
        }
        if (!userMessagePersisted) removeMessage(userMsgId);
      }
    }

    function applyStreamEvent(evt: PersonaStreamEvent) {
      switch (evt.type) {
        case 'meta':
          break;
        case 'status':
          setStreamingStatus(statusLabel(evt.status));
          break;
        case 'source_candidate':
          userMessagePersisted = true;
          candidateAccum.push({
            display_index: evt.display_index,
            ref_token: evt.ref_token,
            source_type: evt.source_type,
            title: evt.title,
            snippet: evt.snippet,
          });
          break;
        case 'source_set':
          break;
        case 'reference':
          collectedRefs.push({
            display_index: evt.display_index,
            ref_token: evt.ref_token,
            source_type: evt.source_type,
            title: evt.title,
            snippet: evt.snippet,
            created_at: evt.created_at,
          });
          break;
        case 'text_delta':
          sawAnyDelta = true;
          upsertMessage(assistantMsgId, (prevMsg) => {
            const prevContent = prevMsg && prevMsg.kind === 'stream' ? prevMsg.content : '';
            return {
              id: assistantMsgId,
              kind: 'stream',
              role: 'assistant',
              content: prevContent + evt.text,
              pending: true,
              requestId,
            };
          });
          break;
        case 'done':
          sawDone = true;
          userMessagePersisted = true;
          if (Array.isArray(evt.candidates)) {
            removeMessage(assistantMsgId);
            setMessages((prev) => [
              ...prev,
              {
                id: nextId('c'),
                kind: 'source_candidates',
                role: 'assistant',
                message: evt.message || '',
                candidates: candidateAccum.length > 0 ? candidateAccum : (evt.candidates as StreamCandidateItem[]),
                proposalId: Number(evt.proposal_id),
                baseVersion: Number(evt.base_version || 0),
                phase: (evt.phase as SourceCandidateMessage['phase']) || 'awaiting_source_confirm',
                userMessageId: userMsgId,
                requestId,
                expansionTopic: evt.expansion_topic || undefined,
              } as SourceCandidateMessage,
            ]);
          } else {
            upsertMessage(assistantMsgId, () => ({
              id: assistantMsgId,
              kind: 'stream',
              role: 'assistant',
              content: String(evt.message || ''),
              references: (evt.references && evt.references.length > 0) ? evt.references : collectedRefs,
              pending: false,
              requestId,
            }));
            if (evt.source_set_version != null) {
              setSessionMeta((prev) => (prev ? { ...prev, currentSourceSetVersion: evt.source_set_version, stale: false } : prev));
              void refreshLockedSources(sid);
            }
          }
          break;
        case 'error':
          sawError = true;
          setError(toSafeUserError(evt.message, evt.code));
          setLastFailedSend({ text, extras: {} });
          if (evt.code === 'SOURCE_SET_STALE') {
            setSessionMeta((prev) => (prev ? { ...prev, stale: true } : prev));
            void refreshLockedSources(sid);
          }
          break;
        default:
          break;
      }
    }
  }, [refreshLockedSources, removeMessage, upsertMessage]);

  // ---------------------------------------------------------------------
  // 整理师：沿用既有 preview → diff → confirm 非流式语义
  // ---------------------------------------------------------------------
  const clearOrganizeStageTimer = useCallback(() => {
    if (organizeStageTimerRef.current != null) {
      window.clearInterval(organizeStageTimerRef.current);
      organizeStageTimerRef.current = null;
    }
  }, []);

  const sendOrganizerMessage = useCallback(async (sid: string, text: string, extras: OrganizerExtras) => {
    const userMsgId = nextId('u');
    setMessages((prev) => [
      ...prev,
      { id: userMsgId, kind: 'legacy', role: 'user', content: text, createdAt: new Date().toISOString() },
    ]);
    setIsStreaming(true);
    setError('');
    setLastFailedSend(null);
    setOrganizeStage('scope');
    clearOrganizeStageTimer();
    let stageIdx = 0;
    organizeStageTimerRef.current = window.setInterval(() => {
      stageIdx = Math.min(stageIdx + 1, ORGANIZE_STAGE_SEQUENCE.length - 1);
      setOrganizeStage(ORGANIZE_STAGE_SEQUENCE[stageIdx]);
    }, 1400);
    try {
      const data = await sendChatMessage(sid, text, 'organize', {
        days: extras.days,
        label_codes: extras.labelCodes,
        entry_ids: extras.entryIds && extras.entryIds.length > 0 ? extras.entryIds : null,
        allowed_actions: extras.allowedActions && extras.allowedActions.length > 0
          ? extras.allowedActions
          : null,
        goal: text,
      });
      setMessages((prev) => [
        ...prev,
        {
          id: nextId('a'),
          kind: 'legacy',
          role: 'assistant',
          content: data.message,
          createdAt: new Date().toISOString(),
          references: data.references || [],
          organizePreview: data.organize_preview,
        },
      ]);
      void refreshSessions();
    } catch (err) {
      const code = err && typeof err === 'object' && 'code' in err
        ? String((err as { code?: string }).code || '')
        : '';
      setError(toSafeUserError(err instanceof Error ? err.message : 'AI 服务暂时不可用，请稍后重试', code));
      setLastFailedSend({ text, extras });
      removeMessage(userMsgId);
    } finally {
      clearOrganizeStageTimer();
      setOrganizeStage(null);
      setIsStreaming(false);
    }
  }, [clearOrganizeStageTimer, refreshSessions, removeMessage]);

  const sendMessage = useCallback(async (text: string, organizerExtras: OrganizerExtras = {}) => {
    const trimmed = text.trim();
    if (!trimmed || !sessionId || !persona || isStreaming) return;
    if (sessionMeta?.legacy) return;
    if (!isPersonaAvailable(persona)) {
      setError(ORGANIZER_MAINTENANCE_MESSAGE);
      return;
    }
    if (persona === 'organizer') {
      await sendOrganizerMessage(sessionId, trimmed, organizerExtras);
    } else {
      await sendStreamingMessage(persona, sessionId, trimmed);
    }
  }, [isStreaming, persona, sendOrganizerMessage, sendStreamingMessage, sessionId, sessionMeta]);

  const retryLastFailedSend = useCallback(async () => {
    if (!lastFailedSend || !sessionId || !persona || isStreaming) return;
    const { text, extras } = lastFailedSend;
    setError('');
    await sendMessage(text, extras);
  }, [isStreaming, lastFailedSend, persona, sendMessage, sessionId]);

  // ---------------------------------------------------------------------
  // 讲解员来源候选：确认 / 取消
  // ---------------------------------------------------------------------
  const confirmCandidates = useCallback(async (
    candidateMsg: SourceCandidateMessage,
    selectedRefTokens?: string[],
  ) => {
    if (!sessionId) return;
    upsertMessage(candidateMsg.id, () => ({ ...candidateMsg, actionPending: true, actionError: undefined }));
    try {
      const tokens =
        selectedRefTokens
        && selectedRefTokens.length > 0
          ? selectedRefTokens
          : candidateMsg.candidates.map((c) => c.ref_token);
      const confirmed = await confirmSourceSetProposal(sessionId, {
        proposalId: candidateMsg.proposalId,
        baseVersion: candidateMsg.baseVersion,
        selectedRefTokens: tokens,
      });
      upsertMessage(candidateMsg.id, () => ({
        ...candidateMsg,
        resolution: 'confirmed',
        actionPending: false,
      }));
      setSessionMeta((prev) => (prev ? { ...prev, currentSourceSetVersion: confirmed.version, stale: false } : prev));

      // 确认后必须走流式 resume；旧非流式 resumeExplainerProposal 不再调用。
      const assistantMsgId = nextId('a');
      const controller = new AbortController();
      abortRef.current = controller;
      setIsStreaming(true);
      setStreamingStatus(statusLabel('generating'));
      setError('');
      // Must reuse pending user / candidate canonical request_id (R11.3-Fix2).
      const requestId = candidateMsg.requestId || generateRequestId();
      if (!candidateMsg.requestId) {
        upsertMessage(candidateMsg.id, () => ({ ...candidateMsg, requestId, actionPending: false, resolution: 'confirmed' }));
      }
      let sawDone = false;
      let sawAnyDelta = false;
      let sawError = false;
      const collectedRefs: StreamReferenceItem[] = [];

      try {
        for await (const evt of streamExplainerResume(sessionId, candidateMsg.proposalId, {
          requestId,
          signal: controller.signal,
        })) {
          if (controller.signal.aborted) break;
          if (evt.type === 'status') {
            setStreamingStatus(statusLabel(evt.status));
          } else if (evt.type === 'text_delta') {
            sawAnyDelta = true;
            upsertMessage(assistantMsgId, (prevMsg) => {
              const prevContent = prevMsg && prevMsg.kind === 'stream' ? prevMsg.content : '';
              return {
                id: assistantMsgId,
                kind: 'stream',
                role: 'assistant',
                content: prevContent + evt.text,
                pending: true,
                requestId,
              };
            });
          } else if (evt.type === 'reference') {
            collectedRefs.push({
              display_index: evt.display_index,
              ref_token: evt.ref_token,
              source_type: evt.source_type,
              title: evt.title,
              snippet: evt.snippet,
              created_at: evt.created_at,
            });
          } else if (evt.type === 'done') {
            sawDone = true;
            upsertMessage(assistantMsgId, () => ({
              id: assistantMsgId,
              kind: 'stream',
              role: 'assistant',
              content: String(evt.message || ''),
              references: (evt.references && evt.references.length > 0) ? evt.references : collectedRefs,
              pending: false,
              requestId,
            }));
            if (evt.source_set_version != null) {
              setSessionMeta((prev) => (
                prev ? { ...prev, currentSourceSetVersion: evt.source_set_version, stale: false } : prev
              ));
            }
            void refreshLockedSources(sessionId);
          } else if (evt.type === 'error') {
            sawError = true;
            setError(toSafeUserError(evt.message));
          }
        }
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        setIsStreaming(false);
        setStreamingStatus(null);
        if (!sawDone) {
          removeMessage(assistantMsgId);
          if (sawAnyDelta && !sawError) setNotice('已停止，未保存');
        }
      }
      void refreshSessions();
      void refreshLockedSources(sessionId);
    } catch (err) {
      upsertMessage(candidateMsg.id, () => ({
        ...candidateMsg,
        actionPending: false,
        actionError: toSafeUserError(err instanceof Error ? err.message : '确认来源失败'),
      }));
    }
  }, [refreshLockedSources, refreshSessions, removeMessage, sessionId, upsertMessage]);

  const cancelCandidates = useCallback(async (candidateMsg: SourceCandidateMessage) => {
    if (!sessionId) return;
    upsertMessage(candidateMsg.id, () => ({ ...candidateMsg, actionPending: true, actionError: undefined }));
    try {
      await cancelSourceSetProposal(sessionId, candidateMsg.proposalId);
      setMessages((prev) => prev.filter((m) => m.id !== candidateMsg.id && m.id !== candidateMsg.userMessageId));
      setNotice('已取消来源候选，可以重新提问');
    } catch (err) {
      upsertMessage(candidateMsg.id, () => ({
        ...candidateMsg,
        actionPending: false,
        actionError: toSafeUserError(err instanceof Error ? err.message : '取消来源候选失败'),
      }));
    }
  }, [sessionId, upsertMessage]);

  const applyPendingProposal = useCallback((pending: PendingProposalResponse) => {
    if (!pending.pending || !pending.proposal_id || !Array.isArray(pending.candidates)) return;
    const userMsgId = nextId('u');
    setMessages((prev) => {
      const next = [...prev];
      let boundUserId = userMsgId;
      if (pending.pending_user_content) {
        const existingUser = [...next].reverse().find(
          (m) => m.kind === 'stream' && m.role === 'user' && m.content === pending.pending_user_content,
        );
        if (existingUser) {
          boundUserId = existingUser.id;
        } else {
          next.push({
            id: userMsgId,
            kind: 'stream',
            role: 'user',
            content: pending.pending_user_content,
            requestId: pending.request_id || undefined,
          });
        }
      }
      const hasCard = next.some(
        (m) => m.kind === 'source_candidates' && m.proposalId === pending.proposal_id && !m.resolution,
      );
      if (!hasCard) {
        next.push({
          id: nextId('c'),
          kind: 'source_candidates',
          role: 'assistant',
          message: pending.message || '',
          candidates: pending.candidates as StreamCandidateItem[],
          proposalId: Number(pending.proposal_id),
          baseVersion: Number(pending.base_version || 0),
          phase: (pending.phase as SourceCandidateMessage['phase']) || 'awaiting_expansion_confirm',
          userMessageId: boundUserId,
          requestId: pending.request_id || undefined,
          expansionTopic: pending.expansion_topic || undefined,
        });
      }
      return next;
    });
  }, []);

  const openExpandTopicDialog = useCallback((draft = '') => {
    if (!sessionId || expandingSources || isStreaming) return;
    if (messages.some((m) => m.kind === 'source_candidates' && !m.resolution)) {
      setNotice('请先处理当前来源候选');
      return;
    }
    setExpandTopicDraft(draft);
    setExpandTopicError('');
    setExpandTopicOpen(true);
  }, [expandingSources, isStreaming, messages, sessionId]);

  const closeExpandTopicDialog = useCallback(() => {
    if (expandingSources) return;
    setExpandTopicOpen(false);
  }, [expandingSources]);

  /** 主题确认后走显式 expansion proposal；空主题不发请求。 */
  const submitExpandTopic = useCallback(async (topic: string) => {
    const trimmed = topic.trim();
    if (!sessionId || expandingSources || isStreaming) return;
    if (trimmed.length < 2 || trimmed.length > 200) {
      setExpandTopicError('请输入 2～200 字的补充主题');
      return;
    }
    if (messages.some((m) => m.kind === 'source_candidates' && !m.resolution)) {
      setExpandTopicError('请先处理当前来源候选');
      return;
    }
    setExpandingSources(true);
    setExpandTopicDraft(trimmed);
    setExpandTopicError('');
    setLockedSourcesError('');
    setError('');
    try {
      const pending = await expandSourceSet(sessionId, trimmed);
      applyPendingProposal(pending);
      setExpandTopicOpen(false);
      setNotice('已生成补充来源候选，请确认后继续');
    } catch (err) {
      const msg = toSafeUserError(err instanceof Error ? err.message : '补充来源失败');
      // Keep draft for retry (empty candidates / network / validation).
      setExpandTopicError(msg.includes('没有找到') ? '没有找到新的相关来源' : msg);
    } finally {
      setExpandingSources(false);
    }
  }, [applyPendingProposal, expandingSources, isStreaming, messages, sessionId]);

  // ---------------------------------------------------------------------
  // 正文内〔n〕引用点击 → 打开安全预览（仅 title/snippet/source_type/created_at）
  // ---------------------------------------------------------------------
  const openCitation = useCallback((displayIndex: number, references: StreamReferenceItem[] | undefined) => {
    const found = (references || []).find((r) => r.display_index === displayIndex);
    if (found) setActiveReferencePreview(found);
  }, []);

  const openReferencePreview = useCallback((reference: StreamReferenceItem) => {
    setActiveReferencePreview(reference);
  }, []);

  const closeReferencePreview = useCallback(() => setActiveReferencePreview(null), []);

  const handleCompactSession = useCallback(async () => {
    if (!sessionId) return;
    try {
      const result = await compactChatSession(sessionId);
      setNotice(`会话摘要已更新，覆盖 ${result.message_count} 条消息`);
    } catch (err) {
      setError(toSafeUserError(err instanceof Error ? err.message : '压缩会话失败'));
    }
  }, [sessionId]);

  const personaUnavailable = Boolean(persona && !isPersonaAvailable(persona));

  const canSend = useMemo(() => {
    if (!sessionId || !persona || isStreaming || sessionCreating) return false;
    if (personaUnavailable) return false;
    if (sessionMeta?.legacy) return false;
    if (sessionMeta?.stale) return false;
    const hasPendingCandidate = messages.some((m) => m.kind === 'source_candidates' && !m.resolution);
    return !hasPendingCandidate;
  }, [isStreaming, messages, persona, personaUnavailable, sessionCreating, sessionId, sessionMeta]);

  return {
    view,
    persona,
    sessionId,
    sessionMeta,
    sessions,
    sessionsLoading,
    sessionCreating,
    messages,
    historyLoading,
    isStreaming,
    streamingStatus,
    organizeStage,
    error,
    notice,
    clearError,
    clearNotice,
    canSend,
    personaUnavailable,
    canRetry: Boolean(lastFailedSend) && !isStreaming && !personaUnavailable,
    retryLastFailedSend,
    activeReferencePreview,
    lockedSources,
    lockedSourcesLoading,
    lockedSourcesError,
    expandingSources,
    expandTopicOpen,
    expandTopicDraft,
    expandTopicError,

    startNewChat,
    createSessionForPersona,
    selectSession,
    deleteSession,
    refreshSessions,
    handleCompactSession,

    sendMessage,
    stopStreaming,

    confirmCandidates,
    cancelCandidates,
    openExpandTopicDialog,
    closeExpandTopicDialog,
    submitExpandTopic,
    refreshLockedSources,

    openCitation,
    openReferencePreview,
    closeReferencePreview,
    handleOrganizeSaved,

    messagesScrollRef,
    onMessagesScroll,
  };
}

/** 历史消息回放：区分整理师/历史会话（经典 ReferenceItem + organize_preview dict）与检索员/讲解员（StreamReferenceItem）。 */
function parseHistoryReferences(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function mapHistoryMessage(msg: ChatMessage, meta: SessionMetaView): PersonaMessage {
  const role: 'user' | 'assistant' = msg.role === 'user' ? 'user' : 'assistant';
  const parsedRaw = parseHistoryReferences(msg.references);
  if (meta.persona === 'retriever' || meta.persona === 'explainer') {
    const refs = Array.isArray(parsedRaw) ? (parsedRaw as StreamReferenceItem[]) : [];
    return {
      id: nextId('h'),
      kind: 'stream',
      role,
      content: msg.content,
      createdAt: msg.created_at,
      references: refs,
    };
  }

  let references: ReferenceItem[] = [];
  let organizePreview: OrganizePreviewResponse | undefined;
  const raw = parsedRaw;
  if (Array.isArray(raw)) {
    references = raw as ReferenceItem[];
  } else if (raw && typeof raw === 'object') {
    const dict = raw as Record<string, unknown>;
    if (dict.kind === 'organize' && dict.organize_preview) {
      organizePreview = dict.organize_preview as OrganizePreviewResponse;
    }
    if (Array.isArray(dict.valid_references)) {
      references = dict.valid_references as ReferenceItem[];
    }
  }
  return {
    id: nextId('h'),
    kind: 'legacy',
    role,
    content: msg.content,
    createdAt: msg.created_at,
    references,
    organizePreview,
    agentSteps: msg.agent_steps,
    analysis: msg.analysis,
    reviewPreview: msg.review_preview,
  };
}

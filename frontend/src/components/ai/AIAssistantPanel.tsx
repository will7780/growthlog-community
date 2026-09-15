/**
 * AI 助手右侧对话面板（R11.3 三角色版）
 * 从右侧滑出，不影响主页面；持久化流式业务逻辑全部委托给 usePersonaChat。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Bot, X, ArrowLeft, Send, Plus, Trash2, Square } from 'lucide-react';
import IconButton from '../common/IconButton';
import {
  getAgentPendingActions,
  confirmAgentPendingAction,
  rejectAgentPendingAction,
  previewEntryAnnotation,
} from '../../api/ai';
import type { AgentPendingActionItem, ReferenceItem } from '../../api/ai';
import type { EntryWithChildrenResponse } from '../../types/api';
import { createEntry, getEntry } from '../../api/entries';
import AnalysisEvidenceBlock from './AnalysisEvidenceBlock';
import AnalysisDetailPanel, { hasAnalysisDetailContent } from './AnalysisDetailPanel';
import AgentMemoryPanel from './AgentMemoryPanel';
import { getReferenceCardKey } from './referenceDisplay';
import ReferenceList from './ReferenceList';
import ReferenceDetail from './ReferenceDetail';
import ConfirmDialog from '../common/ConfirmDialog';
import { useIsMobile } from '../../hooks/useResponsiveMode';
import { OrganizeDiffPanel, OrganizeScopeBar, type OrganizeScopeState } from './OrganizeControls';
import { usePersonaChat, type LegacyMessage, type StreamChatMessage } from '../../hooks/usePersonaChat';
import PersonaPicker from './PersonaPicker';
import PersonaAvatar from './PersonaAvatar';
import { personaMeta } from './personaMeta';
import { ORGANIZER_MAINTENANCE_MESSAGE } from '../../config/aiPersonaAvailability';
import StreamMessageText from './StreamMessageText';
import StreamReferenceChips from './StreamReferenceChips';
import StreamReferencePreview from './StreamReferencePreview';
import SourceCandidateCard from './SourceCandidateCard';
import LockedSourcesBar from './LockedSourcesBar';
import ExpandTopicDialog from './ExpandTopicDialog';

interface AIAssistantPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

function sessionListLabel(sessionTitle: string | null | undefined, lastMessage: string): string {
  if (sessionTitle && sessionTitle.trim()) return sessionTitle.trim();
  return lastMessage || '新对话';
}

export default function AIAssistantPanel({ isOpen, onClose }: AIAssistantPanelProps) {
  const isMobile = useIsMobile();
  const navigate = useNavigate();
  const chat = usePersonaChat();
  const expandBtnRef = useRef<HTMLButtonElement | null>(null) as React.MutableRefObject<HTMLButtonElement | null>;
  const organizePreviewSessionRef = useRef<string | null>(null);

  const [input, setInput] = useState('');
  const [pendingActions, setPendingActions] = useState<AgentPendingActionItem[]>([]);
  const [analysisDetailIndex, setAnalysisDetailIndex] = useState<number | null>(null);
  const [memoryPanelOpen, setMemoryPanelOpen] = useState(false);
  const [deleteSessionId, setDeleteSessionId] = useState<string | null>(null);
  const [deleteSessionLoading, setDeleteSessionLoading] = useState(false);
  const [deleteSessionError, setDeleteSessionError] = useState('');
  const [organizeScope, setOrganizeScope] = useState<OrganizeScopeState>({
    days: 30,
    labelCodes: [],
    entryIds: [],
    allowedActions: ['create_summary', 'suggest_tag', 'create_action_plan', 'rewrite_content', 'memory_note'],
  });

  // 历史（整理师/旧会话）引用详情——需要拉完整 entry，与流式预览分开管理
  const [selectedReferenceKey, setSelectedReferenceKey] = useState<string | null>(null);
  const [referenceEntry, setReferenceEntry] = useState<EntryWithChildrenResponse | null>(null);
  const [referenceLoading, setReferenceLoading] = useState(false);
  const [referenceError, setReferenceError] = useState('');
  const [annotationText, setAnnotationText] = useState('');
  const [annotationLoading, setAnnotationLoading] = useState(false);
  const [appendText, setAppendText] = useState('');
  const [appendLoading, setAppendLoading] = useState(false);

  const legacyReferences = useMemo(() => {
    for (let i = chat.messages.length - 1; i >= 0; i -= 1) {
      const msg = chat.messages[i];
      if (msg.kind === 'legacy' && msg.role === 'assistant' && msg.references && msg.references.length > 0) {
        return msg.references;
      }
    }
    return [] as ReferenceItem[];
  }, [chat.messages]);

  const selectedReference = useMemo(() => {
    if (!selectedReferenceKey) return null;
    return legacyReferences.find((ref, index) => getReferenceCardKey(ref, index) === selectedReferenceKey) || null;
  }, [legacyReferences, selectedReferenceKey]);

  const closeLegacyReference = () => {
    setSelectedReferenceKey(null);
    setReferenceEntry(null);
    setReferenceError('');
    setReferenceLoading(false);
    setAnnotationText('');
    setAppendText('');
  };

  const handleOrganizePreviewSource = useCallback((refToken: string, previewSessionId?: string) => {
    const sessionId = previewSessionId || chat.sessionId;
    if (!sessionId) return;
    organizePreviewSessionRef.current = previewSessionId || null;
    chat.openReferencePreview({
      display_index: 0,
      ref_token: refToken,
      source_type: 'entry',
      title: '',
      snippet: '',
    });
  }, [chat]);

  const closeStreamReferencePreview = useCallback(() => {
    organizePreviewSessionRef.current = null;
    chat.closeReferencePreview();
  }, [chat]);

  useEffect(() => {
    closeLegacyReference();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.sessionId]);

  useEffect(() => {
    if (!isOpen) return;
    const sid = chat.sessionId || undefined;
    getAgentPendingActions({ session_id: sid, status: 'pending' })
      .then(setPendingActions)
      .catch(() => setPendingActions([]));
  }, [isOpen, chat.sessionId]);

  const handleOpenLegacyReference = async (reference: ReferenceItem, index: number) => {
    setSelectedReferenceKey(getReferenceCardKey(reference, index));
    setReferenceEntry(null);
    setReferenceError('');
    setReferenceLoading(true);
    setAnnotationText('');
    setAppendText('');
    try {
      const targetId = reference.root_entry_id || reference.entry_id;
      if (!targetId) throw new Error('该来源请在检索鼠或讲解鼠的引用预览中查看');
      setReferenceEntry(await getEntry(targetId));
    } catch (err) {
      setReferenceError(err instanceof Error ? err.message : '完整记录加载失败');
    } finally {
      setReferenceLoading(false);
    }
  };

  const handleGenerateAnnotation = async () => {
    if (!selectedReference?.entry_id || annotationLoading) return;
    setAnnotationLoading(true);
    try {
      const result = await previewEntryAnnotation(selectedReference.entry_id);
      const nextText = [result.summary, result.action_hint ? `建议：${result.action_hint}` : '']
        .filter(Boolean)
        .join('\n\n');
      setAnnotationText(nextText || '暂时没有生成批注。');
    } catch {
      setAnnotationText('生成 AI 批注失败');
    } finally {
      setAnnotationLoading(false);
    }
  };

  const handleAppendToReference = async () => {
    if (!selectedReference?.entry_id || !referenceEntry || !appendText.trim() || appendLoading) return;
    setAppendLoading(true);
    try {
      await createEntry({
        label_code: referenceEntry.label_code,
        content: appendText.trim(),
        parent_id: selectedReference.entry_id,
      });
      setAppendText('');
    } finally {
      setAppendLoading(false);
    }
  };

  const requestDeleteSession = (sid: string, e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleteSessionError('');
    setDeleteSessionId(sid);
  };

  const handleDeleteSession = async () => {
    if (!deleteSessionId || deleteSessionLoading) return;
    setDeleteSessionLoading(true);
    setDeleteSessionError('');
    try {
      await chat.deleteSession(deleteSessionId);
      setDeleteSessionId(null);
    } catch (err) {
      setDeleteSessionError(err instanceof Error ? err.message : '删除会话失败');
    } finally {
      setDeleteSessionLoading(false);
    }
  };

  const handleConfirmPendingAction = async (actionId: number) => {
    try {
      await confirmAgentPendingAction(actionId);
      setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
    } catch {
      /* 静默处理，与旧面板保持一致 */
    }
  };

  const handleRejectPendingAction = async (actionId: number) => {
    try {
      await rejectAgentPendingAction(actionId);
      setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
    } catch {
      /* 静默处理 */
    }
  };

  const pendingActionTitle = (action: AgentPendingActionItem) => {
    if (action.action_type === 'create_todo') return '创建小要事';
    if (action.action_type === 'create_memory') return '保存个性设置';
    if (action.action_type === 'update_memory') return '更新个性设置';
    return '保存总结记录';
  };

  const pendingActionSummary = (action: AgentPendingActionItem) => {
    const payload = action.payload_json || {};
    const content = payload.content || payload.summary || payload.title || '';
    if (typeof content === 'string' && content.trim()) return content.trim();
    return JSON.stringify(payload).slice(0, 160);
  };

  const handleJumpToEntry = (entryId: number) => {
    onClose();
    navigate(`/app?entry_id=${entryId}`);
  };

  const handleSend = () => {
    const text = input;
    if (!text.trim() || !chat.canSend) return;
    setInput('');
    void chat.sendMessage(text, {
      days: organizeScope.days,
      labelCodes: organizeScope.labelCodes,
      entryIds: organizeScope.entryIds,
      allowedActions: organizeScope.allowedActions,
    });
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const legacyAnalysisMessages = chat.messages.filter((m): m is LegacyMessage => m.kind === 'legacy');

  if (!isOpen) return null;

  const meta = personaMeta(chat.persona);
  const legacyBanner = chat.sessionMeta?.legacy;

  return (
    <>
      <div className="growth-ai-overlay" onClick={onClose} />

      {!isMobile && selectedReference && (
        <ReferenceDetail
          reference={selectedReference}
          entry={referenceEntry}
          loading={referenceLoading}
          error={referenceError}
          onClose={closeLegacyReference}
          onOpenEntry={handleJumpToEntry}
          annotationText={annotationText}
          annotationLoading={annotationLoading}
          onGenerateAnnotation={handleGenerateAnnotation}
          appendText={appendText}
          appendLoading={appendLoading}
          onAppendTextChange={setAppendText}
          onAppend={handleAppendToReference}
        />
      )}

      {!isMobile && !selectedReference && chat.activeReferencePreview && (organizePreviewSessionRef.current || chat.sessionId) && (
        <StreamReferencePreview
          reference={chat.activeReferencePreview}
          sessionId={organizePreviewSessionRef.current || chat.sessionId || ''}
          onClose={closeStreamReferencePreview}
          variant="desktop"
        />
      )}

      <div
        className={`growth-ai-panel ${isMobile ? 'mobile-ai-panel' : ''} ${
          !isMobile && (selectedReference || chat.activeReferencePreview) ? 'growth-ai-panel--reference-open' : ''
        }`}
      >
        <header className="growth-ai-header">
          <div className="growth-ai-header-main">
            {isMobile && <IconButton icon={ArrowLeft} label="返回" onClick={onClose} variant="ghost" />}
            {meta ? (
              <PersonaAvatar persona={chat.persona} size={28} className="growth-ai-header-avatar" />
            ) : (
              <Bot size={20} aria-hidden="true" className="growth-ai-header-icon" />
            )}
            <div className="growth-ai-header-text">
              <div className="growth-ai-header-title">
                {meta ? meta.label : 'AI 助手'}
                {chat.sessionMeta?.title ? <span> · {chat.sessionMeta.title}</span> : null}
                {legacyBanner ? <span className="growth-ai-legacy-badge">只读</span> : null}
              </div>
              <div className="growth-ai-header-subtitle">
                {meta ? meta.tagline : 'Growth Log AI 工作助手'}
              </div>
            </div>
          </div>
          {!isMobile && <IconButton icon={X} label="关闭 AI 助手" onClick={onClose} variant="ghost" />}
          {isMobile && (
            <button type="button" onClick={() => setMemoryPanelOpen(true)} className="growth-ai-memory-entry">
              个性设置
            </button>
          )}
        </header>

        <div className="growth-ai-body">
          {!isMobile && (
            <aside className="growth-ai-sidebar">
              <div className="growth-ai-sidebar-head">
                <div className="growth-ai-sidebar-label">历史会话</div>
                <button type="button" onClick={chat.startNewChat} className="growth-ai-new-chat">
                  <Plus size={14} aria-hidden="true" style={{ display: 'inline', verticalAlign: '-2px', marginRight: 4 }} />
                  新对话
                </button>
              </div>
              <div className="growth-ai-compact-row">
                <button type="button" onClick={() => setMemoryPanelOpen(true)}>
                  个性设置
                </button>
              </div>

              <div className="growth-ai-session-list">
                {chat.sessions.map((session) => {
                  const isLegacy = Boolean(session.legacy) || session.persona == null;
                  return (
                    <div
                      key={session.session_id}
                      className={`growth-ai-session-item ${
                        chat.sessionId === session.session_id ? 'growth-ai-session-item--active' : ''
                      }`}
                      onClick={() => chat.selectSession(session.session_id)}
                    >
                      <div className="growth-ai-session-row">
                        <PersonaAvatar persona={session.persona ?? null} size={20} className="growth-ai-session-avatar" />
                        <div className="growth-ai-session-title">
                          {sessionListLabel(session.title, session.last_message)}
                        </div>
                      </div>
                      <div className="growth-ai-session-meta">
                        <span>
                          {session.created_at?.slice(0, 10)}
                          {isLegacy ? ' · 只读' : ''}
                        </span>
                        <IconButton
                          icon={Trash2}
                          label="删除会话"
                          size="sm"
                          variant="ghost"
                          onClick={(e) => requestDeleteSession(session.session_id, e)}
                          className="growth-ai-session-delete"
                        />
                      </div>
                    </div>
                  );
                })}
                {chat.sessions.length === 0 && !chat.sessionsLoading && (
                  <div className="growth-ai-empty-copy" style={{ padding: '1rem' }}>暂无会话</div>
                )}
              </div>
            </aside>
          )}

          <section className="growth-ai-chat">
            {chat.view === 'personaSelect' ? (
              <div className="growth-ai-messages growth-ai-messages--persona-select">
                <PersonaPicker onPick={(persona) => void chat.createSessionForPersona(persona)} disabled={chat.sessionCreating} />
              </div>
            ) : (
              <div className="growth-ai-messages" ref={chat.messagesScrollRef} onScroll={chat.onMessagesScroll}>
                {legacyBanner && (
                  <div className="growth-ai-legacy-notice">这是一段历史会话，没有角色信息，仅供查看，不能继续对话。</div>
                )}
                {chat.personaUnavailable && (
                  <div className="growth-ai-maintenance-notice" role="status">
                    {ORGANIZER_MAINTENANCE_MESSAGE}
                  </div>
                )}

                {chat.historyLoading && <div className="growth-ai-empty-copy">正在加载历史消息...</div>}

                {!chat.historyLoading && chat.messages.length === 0 && meta && (
                  <div className="growth-ai-empty">
                    <div className="growth-ai-empty-icon">
                      <PersonaAvatar persona={chat.persona} size={40} />
                    </div>
                    <div className="growth-ai-empty-title">{meta.tagline}</div>
                    <div className="growth-ai-empty-copy">{meta.emptyHint}</div>
                  </div>
                )}

                {chat.messages.map((msg) => {
                  if (msg.kind === 'source_candidates') {
                    return (
                      <div key={msg.id} className="growth-ai-msg-row growth-ai-msg-row--assistant">
                        <SourceCandidateCard
                          candidateMsg={msg}
                          onConfirm={chat.confirmCandidates}
                          onCancel={chat.cancelCandidates}
                        />
                      </div>
                    );
                  }

                  if (msg.kind === 'stream') {
                    const streamMsg = msg as StreamChatMessage;
                    return (
                      <div
                        key={msg.id}
                        className={`growth-ai-msg-row ${
                          msg.role === 'user' ? 'growth-ai-msg-row--user' : 'growth-ai-msg-row--assistant'
                        }`}
                      >
                        <div className={`growth-ai-bubble ${msg.role === 'user' ? 'growth-ai-bubble--user' : 'growth-ai-bubble--assistant'}`}>
                          <StreamMessageText
                            text={streamMsg.content}
                            sessionId={chat.sessionId || undefined}
                            references={streamMsg.references}
                            onCitationClick={(idx) => chat.openCitation(idx, streamMsg.references)}
                            hasReference={(idx) => (streamMsg.references || []).some((r) => r.display_index === idx)}
                          />
                          {streamMsg.pending && !streamMsg.content && (
                            <span className="growth-ai-loading-dots">
                              <span className="growth-ai-loading-dot" />
                              <span className="growth-ai-loading-dot" style={{ animationDelay: '150ms' }} />
                              <span className="growth-ai-loading-dot" style={{ animationDelay: '300ms' }} />
                            </span>
                          )}
                        </div>
                        {msg.role === 'assistant' && streamMsg.references && streamMsg.references.length > 0 && (
                          <StreamReferenceChips
                            sessionId={chat.sessionId || ''}
                            references={streamMsg.references}
                            activeToken={chat.activeReferencePreview?.ref_token ?? null}
                            onSelect={(ref) => chat.openCitation(ref.display_index, streamMsg.references)}
                            variant="desktop"
                          />
                        )}
                      </div>
                    );
                  }

                  const legacyMsg = msg as LegacyMessage;
                  const legacyIndex = legacyAnalysisMessages.indexOf(legacyMsg);
                  return (
                    <div
                      key={msg.id}
                      className={`growth-ai-msg-row ${
                        msg.role === 'user' ? 'growth-ai-msg-row--user' : 'growth-ai-msg-row--assistant'
                      }`}
                    >
                      <div className={`growth-ai-bubble ${msg.role === 'user' ? 'growth-ai-bubble--user' : 'growth-ai-bubble--assistant'}`}>
                        <div>{legacyMsg.content}</div>

                        {msg.role === 'assistant' && legacyMsg.agentSteps && legacyMsg.agentSteps.length > 0 && (
                          <div className="growth-ai-agent-steps">
                            <div className="growth-ai-agent-steps-title">Agent 工具轨迹</div>
                            {legacyMsg.agentSteps.map((step, stepIdx) => (
                              <div key={stepIdx} className="growth-ai-agent-step">
                                <span style={{ fontWeight: 800 }}>{step.title}</span>
                                <span>{step.summary}</span>
                              </div>
                            ))}
                          </div>
                        )}

                        {msg.role === 'assistant' && legacyMsg.analysis && (
                          <>
                            <AnalysisEvidenceBlock analysis={legacyMsg.analysis} onEntryClick={handleJumpToEntry} />
                            {hasAnalysisDetailContent(legacyMsg.analysis) && (
                              <button
                                type="button"
                                className="growth-ai-analysis-detail-toggle"
                                onClick={() => setAnalysisDetailIndex(legacyIndex)}
                              >
                                查看分析详情
                              </button>
                            )}
                          </>
                        )}

                        {msg.role === 'assistant' && legacyMsg.organizePreview && (
                          <div className="growth-ai-preview-card">
                            <div className="growth-ai-preview-title">整合建议</div>
                            <div className="growth-ai-preview-copy">{legacyMsg.organizePreview.message}</div>
                            <OrganizeDiffPanel
                              preview={legacyMsg.organizePreview}
                              onSaved={chat.handleOrganizeSaved}
                              onPreviewSource={handleOrganizePreviewSource}
                            />
                          </div>
                        )}
                      </div>

                      {msg.role === 'assistant' && legacyMsg.references && legacyMsg.references.length > 0 && (
                        <ReferenceList
                          references={legacyMsg.references}
                          variant="desktop"
                          activeKey={selectedReferenceKey}
                          onSelect={handleOpenLegacyReference}
                        />
                      )}
                    </div>
                  );
                })}

                {chat.isStreaming && (
                  <div className="growth-ai-msg-row growth-ai-msg-row--assistant">
                    <div className="growth-ai-bubble growth-ai-bubble--assistant">
                      <div className="growth-ai-loading">
                        <div className="growth-ai-loading-dots">
                          <span className="growth-ai-loading-dot" style={{ animationDelay: '0ms' }} />
                          <span className="growth-ai-loading-dot" style={{ animationDelay: '150ms' }} />
                          <span className="growth-ai-loading-dot" style={{ animationDelay: '300ms' }} />
                        </div>
                        <span>{chat.streamingStatus || '处理中…'}</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {chat.error && (
              <div className="growth-ai-error">
                <span>{chat.error}</span>
                {chat.canRetry && (
                  <button type="button" className="growth-organize-btn" onClick={() => void chat.retryLastFailedSend()}>
                    重试
                  </button>
                )}
                <button type="button" className="growth-ai-dismiss" onClick={chat.clearError} aria-label="关闭错误提示">×</button>
              </div>
            )}
            {chat.notice && (
              <div className="growth-ai-notice">
                {chat.notice}
                <button type="button" className="growth-ai-dismiss" onClick={chat.clearNotice} aria-label="关闭提示">×</button>
              </div>
            )}

            {pendingActions.length > 0 && (
              <div className="growth-ai-pending-actions">
                <div className="growth-ai-pending-title">待确认动作</div>
                {pendingActions.map((action) => (
                  <div key={action.id} className="growth-ai-pending-card">
                    <div className="growth-ai-pending-main">
                      <div className="growth-ai-pending-name">{pendingActionTitle(action)}</div>
                      <div className="growth-ai-pending-copy">{pendingActionSummary(action)}</div>
                    </div>
                    <div className="growth-ai-pending-buttons">
                      <button type="button" onClick={() => handleRejectPendingAction(action.id)} className="growth-ai-pending-btn">
                        拒绝
                      </button>
                      <button type="button" onClick={() => handleConfirmPendingAction(action.id)} className="growth-ai-pending-btn growth-ai-pending-btn--primary">
                        确认
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {chat.view === 'chat' && !legacyBanner && (
              <footer className="growth-ai-footer">
                {chat.persona === 'organizer' && !chat.personaUnavailable && (
                  <OrganizeScopeBar
                    value={organizeScope}
                    onChange={setOrganizeScope}
                    stageLabel={chat.organizeStage}
                  />
                )}
                {chat.persona === 'explainer' && chat.sessionId && (
                  <LockedSourcesBar
                    sources={chat.lockedSources}
                    loading={chat.lockedSourcesLoading}
                    expanding={chat.expandingSources}
                    error={chat.lockedSourcesError}
                    disabled={!chat.canSend || chat.isStreaming}
                    onExpand={() => chat.openExpandTopicDialog()}
                    onPreview={(member) => chat.openReferencePreview(member)}
                    variant="desktop"
                    expandButtonRef={expandBtnRef}
                  />
                )}
                {chat.sessionMeta?.stale && (
                  <div className="growth-ai-stale-notice">来源已过期，请重新提问以生成新的来源版本。</div>
                )}

                <div className="growth-ai-input-row">
                  <textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyPress={handleKeyPress}
                    placeholder={meta?.placeholder || '输入消息……'}
                    className="growth-ai-input"
                    rows={1}
                    disabled={!chat.canSend && !chat.isStreaming}
                  />
                  {chat.isStreaming ? (
                    <IconButton icon={Square} label="停止生成" variant="accent" onClick={chat.stopStreaming} className="growth-ai-send" />
                  ) : (
                    <IconButton
                      icon={Send}
                      label="发送消息"
                      variant="accent"
                      onClick={handleSend}
                      disabled={!input.trim() || !chat.canSend}
                      className="growth-ai-send"
                    />
                  )}
                </div>
              </footer>
            )}
          </section>
        </div>
      </div>

      <AnalysisDetailPanel
        analysis={analysisDetailIndex != null ? legacyAnalysisMessages[analysisDetailIndex]?.analysis : null}
        open={analysisDetailIndex != null}
        onClose={() => setAnalysisDetailIndex(null)}
        variant={isMobile ? 'mobile' : 'desktop'}
        onEntryClick={handleJumpToEntry}
      />
      <AgentMemoryPanel open={memoryPanelOpen} onClose={() => setMemoryPanelOpen(false)} variant={isMobile ? 'mobile' : 'desktop'} />
      <ExpandTopicDialog
        open={chat.expandTopicOpen}
        variant={isMobile ? 'mobile' : 'desktop'}
        loading={chat.expandingSources}
        error={chat.expandTopicError}
        initialTopic={chat.expandTopicDraft}
        onClose={chat.closeExpandTopicDialog}
        onSubmit={chat.submitExpandTopic}
        returnFocusRef={expandBtnRef}
      />
      <ConfirmDialog
        open={deleteSessionId != null}
        title="删除 AI 会话"
        message="确认删除这段会话吗？删除后无法恢复。"
        confirmLabel="删除"
        destructive
        loading={deleteSessionLoading}
        error={deleteSessionError}
        onClose={() => {
          if (!deleteSessionLoading) setDeleteSessionId(null);
        }}
        onConfirm={handleDeleteSession}
      />
    </>
  );
}

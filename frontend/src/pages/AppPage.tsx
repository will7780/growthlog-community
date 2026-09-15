/**
 * 主应用页
 */
import { useState, useEffect, useRef, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import { getLabels } from '../api/labels';
import { getEntries, getEntry, createEntry } from '../api/entries';
import { uploadEntryAttachment } from '../api/attachments';
import Header from '../components/layout/Header';
import LabelFilter from '../components/entries/LabelFilter';
import EntryForm from '../components/entries/EntryForm';
import EntryCard from '../components/entries/EntryCard';
import { EntryListSkeleton } from '../components/common/Skeleton';
import { TodosSection } from '../components/todos';
import { AIAssistantPanel } from '../components/ai';
import AIMPContent from '../components/ai/AIMPContent';
import { InsightsWorkspace } from '../components/insights';
import BottomTabBar, { TabType } from '../components/layout/BottomTabBar';
import EntryFormModal from '../components/entries/EntryFormModal';
import { Sparkles } from 'lucide-react';
import { useIsMobile } from '../hooks/useResponsiveMode';
import { useAuth } from '../contexts/AuthContext';
import type {
  AttachmentResponse,
  EntryResponse,
  LabelResponse,
  EntryWithChildrenResponse,
} from '../types/api';
import { parseContent } from '../utils/parseContent';

const PAGE_SIZE = 10;

interface SubmitOptions {
  parentId?: number | null;
  files?: File[];
}

export default function AppPage() {
  const { user, init } = useAuth();
  const canEditDeleteOwnEntries = user?.can_edit_delete_own_entries === true;
  const [labels, setLabels] = useState<LabelResponse[]>([]);
  const [entries, setEntries] = useState<EntryWithChildrenResponse[]>([]);
  const [appendTarget, setAppendTarget] = useState<{
    entryId: number;
    labelCode: string;
    title: string;
  } | null>(null);
  const [selectedLabelCode, setSelectedLabelCode] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [aiPanelOpen, setAiPanelOpen] = useState(false);
  const [highlightEntryId, setHighlightEntryId] = useState<number | null>(null);
  const formRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const entryRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [searchParams, setSearchParams] = useSearchParams();
  const listRequestEpochRef = useRef(0);
  const inFlightPageKeysRef = useRef<Set<string>>(new Set());
  const loadedRootCountRef = useRef(0);

  // 移动端状态
  const [activeTab, setActiveTab] = useState<TabType>('entries');
  const [desktopWorkspace, setDesktopWorkspace] = useState<'entries' | 'todos' | 'insights'>('entries');
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const isMobile = useIsMobile();

  // 切到桌面时关闭移动新建 sheet，避免双主界面残留
  useEffect(() => {
    if (!isMobile) {
      setCreateModalOpen(false);
      if (activeTab === 'ai') {
        // 移动 AI tab 在桌面改为侧栏，不强制改 tab，保留 entries 语义
      }
    } else {
      setAiPanelOpen(false);
    }
  }, [isMobile]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const loadLabels = async () => {
      const token = localStorage.getItem('token');
      if (!token || !token.trim()) {
        setError('未登录或缺少 token，请先登录');
        return;
      }
      try {
        const labelsData = await getLabels();
        setLabels(labelsData);
      } catch (err) {
        const errorMsg = err instanceof Error ? err.message : '标签加载失败，请刷新页面重试';
        setError(errorMsg);
      }
    };
    loadLabels();
  }, []);

  const loadEntries = useCallback(async (
    offset: number,
    append: boolean,
    labelCode: string | null,
    requestEpoch: number,
  ) => {
    const token = localStorage.getItem('token');
    if (!token || !token.trim()) {
      if (requestEpoch === listRequestEpochRef.current) {
        setError('未登录或缺少 token，请先登录');
      }
      return;
    }

    const pageKey = `${requestEpoch}:${labelCode ?? 'all'}:${offset}`;
    if (inFlightPageKeysRef.current.has(pageKey)) return;
    inFlightPageKeysRef.current.add(pageKey);
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
    }
    setError('');

    try {
      const response = await getEntries({
        limit: PAGE_SIZE,
        offset,
        label_code: labelCode || undefined,
      });
      if (requestEpoch !== listRequestEpochRef.current) return;

      setEntries((previous) => {
        if (!append) return response.items;
        const seenIds = new Set(previous.map((entry) => entry.id));
        const additions = response.items.filter((entry) => !seenIds.has(entry.id));
        return [...previous, ...additions];
      });
      loadedRootCountRef.current = append
        ? offset + response.items.length
        : response.items.length;
      setTotal(response.total);
      setHasMore(offset + response.items.length < response.total);
    } catch (err) {
      if (requestEpoch !== listRequestEpochRef.current) return;
      const errorMsg = err instanceof Error ? err.message : '加载记录失败，请刷新页面重试';
      setError(errorMsg);
    } finally {
      inFlightPageKeysRef.current.delete(pageKey);
      if (requestEpoch === listRequestEpochRef.current) {
        if (append) setLoadingMore(false);
        else setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const requestEpoch = listRequestEpochRef.current + 1;
    listRequestEpochRef.current = requestEpoch;
    loadedRootCountRef.current = 0;
    setEntries([]);
    setHasMore(true);
    void loadEntries(0, false, selectedLabelCode, requestEpoch);
  }, [loadEntries, selectedLabelCode]);

  useEffect(() => {
    const targetId = searchParams.get('entry_id');
    if (!targetId) return;

    const entryId = parseInt(targetId, 10);
    if (isNaN(entryId)) return;

    setSearchParams({}, { replace: true });

    // 移动端：如果当前是 AI Tab，切换到记录 Tab
    if (isMobile && activeTab === 'ai') {
      setActiveTab('entries');
      setDesktopWorkspace('entries');
    }

    const existing = entries.find(e => e.id === entryId);
    if (existing) {
      scrollAndHighlight(entryId);
    } else {
      getEntry(entryId).then(entry => {
        setSelectedLabelCode(null);
        setEntries(prev => {
          if (prev.find(e => e.id === entryId)) return prev;
          return [entry, ...prev];
        });
        setTimeout(() => scrollAndHighlight(entryId), 100);
      }).catch(() => { /* entry may not exist */ });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const scrollAndHighlight = (entryId: number) => {
    setHighlightEntryId(entryId);
    setTimeout(() => {
      const el = entryRefs.current.get(entryId);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 50);
    setTimeout(() => setHighlightEntryId(null), 3000);
  };

  const setEntryRef = useCallback((id: number, el: HTMLDivElement | null) => {
    if (el) {
      entryRefs.current.set(id, el);
    } else {
      entryRefs.current.delete(id);
    }
  }, []);

  const loadMore = useCallback(() => {
    if (loading || loadingMore || !hasMore) return;
    void loadEntries(
      loadedRootCountRef.current,
      true,
      selectedLabelCode,
      listRequestEpochRef.current,
    );
  }, [hasMore, loadEntries, loading, loadingMore, selectedLabelCode]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          loadMore();
        }
      },
      { rootMargin: '200px' }
    );

    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loadMore]);

  const removeAttachmentFromEntry = (entryId: number, attachmentId: number) => {
    setEntries(prev => prev.map(entry => {
      if (entry.id === entryId) {
        return {
          ...entry,
          attachments: entry.attachments.filter(attachment => attachment.id !== attachmentId),
        };
      }
      return {
        ...entry,
        children: entry.children.map(child => (
          child.id === entryId
            ? { ...child, attachments: child.attachments.filter(attachment => attachment.id !== attachmentId) }
            : child
        )),
      };
    }));
  };

  const addAttachmentsToEntry = (entryId: number, attachments: AttachmentResponse[]) => {
    if (!attachments.length) return;
    setEntries(prev => prev.map(entry => {
      if (entry.id === entryId) {
        return {
          ...entry,
          attachments: [...(entry.attachments || []), ...attachments],
        };
      }
      return {
        ...entry,
        children: entry.children.map(child => (
          child.id === entryId
            ? { ...child, attachments: [...(child.attachments || []), ...attachments] }
            : child
        )),
      };
    }));
  };

  const handleCreateEntry = async (labelCode: string, content: string, options?: SubmitOptions) => {
    setSubmitting(true);
    try {
      const newEntry = await createEntry({
        label_code: labelCode,
        content,
        parent_id: options?.parentId,
      });

      const uploadedAttachments = [];
      if (options?.files && options.files.length > 0) {
        for (const file of options.files) {
          uploadedAttachments.push(await uploadEntryAttachment(newEntry.id, file));
        }
      }

      if (options?.parentId) {
        setEntries([]);
        setHasMore(true);
        const requestEpoch = listRequestEpochRef.current + 1;
        listRequestEpochRef.current = requestEpoch;
        loadedRootCountRef.current = 0;
        await loadEntries(0, false, selectedLabelCode, requestEpoch);
      } else {
        const label = labels.find(l => l.code === labelCode);
        const optimisticEntry: EntryWithChildrenResponse = {
          ...newEntry,
          label_name: label?.name || labelCode,
          attachments: [...(newEntry.attachments || []), ...uploadedAttachments],
          children: [],
        };
        const belongsToCurrentFilter = !selectedLabelCode || selectedLabelCode === labelCode;
        if (belongsToCurrentFilter) {
          setEntries(prev => [optimisticEntry, ...prev]);
          loadedRootCountRef.current += 1;
          setTotal(prev => prev + 1);
        }
      }
    } catch (err) {
      // 向上抛出，供 EntryFormModal 保留输入并展示错误
      throw err;
    } finally {
      setSubmitting(false);
    }
  };

  const handleAppendBelow = (entry: EntryWithChildrenResponse) => {
    const { title } = parseContent(entry.content);
    const target = {
      entryId: entry.id,
      labelCode: entry.label_code,
      title,
    };
    setAppendTarget(target);
    if (isMobile) {
      setActiveTab('entries');
      setCreateModalOpen(true);
    } else {
      formRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  };

  const handleEntryUpdated = (updated: EntryResponse) => {
    setEntries((prev) =>
      prev.map((entry) => {
        if (entry.id === updated.id) {
          return {
            ...entry,
            content: updated.content,
            updated_at: updated.updated_at,
            attachments: updated.attachments ?? entry.attachments,
          };
        }
        return {
          ...entry,
          children: entry.children.map((child) =>
            child.id === updated.id
              ? {
                  ...child,
                  content: updated.content,
                  updated_at: updated.updated_at,
                  attachments: updated.attachments ?? child.attachments,
                }
              : child
          ),
        };
      })
    );
  };

  const handleEntryDeleted = (entryId: number) => {
    setEntries((prev) => {
      const wasRoot = prev.some((entry) => entry.id === entryId);
      const next = prev
        .filter((entry) => entry.id !== entryId)
        .map((entry) => ({
          ...entry,
          children: entry.children.filter((child) => child.id !== entryId),
        }));
      if (wasRoot) {
        loadedRootCountRef.current = Math.max(0, loadedRootCountRef.current - 1);
        setTotal((t) => Math.max(0, t - 1));
      }
      return next;
    });
  };

  const handleMutationForbidden = async () => {
    setError('未获得修改或删除原记录的权限');
    try {
      await init();
    } catch {
      /* ignore refresh errors */
    }
  };

  const handleTabChange = (tab: TabType) => {
    if (tab === 'ai') {
      // 移动端：AI 作为 Tab 内容显示；桌面端：打开覆盖层
      if (isMobile) {
        setActiveTab('ai');
      } else {
        setAiPanelOpen(true);
      }
    } else {
      setActiveTab(tab);
      if (tab === 'todos') {
        setDesktopWorkspace('todos');
      } else {
        if (tab === 'entries') {
          setDesktopWorkspace('entries');
        } else if (tab === 'insights') {
          setDesktopWorkspace('insights');
        }
      }
    }
  };

  const handleDesktopWorkspaceChange = (workspace: 'entries' | 'todos' | 'insights') => {
    setDesktopWorkspace(workspace);
    if (workspace !== 'entries') {
      setSelectedLabelCode(null);
    }
  };

  const handleOpenInsightEntry = (entryId: number) => {
    setDesktopWorkspace('entries');
    setActiveTab('entries');
    setSearchParams({ entry_id: String(entryId) });
  };

  const handleCreateFromModal = async (labelCode: string, content: string, files?: File[]) => {
    await handleCreateEntry(labelCode, content, {
      files,
      parentId: appendTarget?.entryId ?? null,
    });
    setAppendTarget(null);
  };

  const handleMobileLabelSelect = (code: string | null) => {
    if (code === 'todo') {
      setActiveTab('todos');
      setDesktopWorkspace('todos');
      return;
    }
    setSelectedLabelCode(code);
  };

  const renderEntryList = () => (
    <section className="growth-records" aria-label="记录列表">
      <h2 className="growth-section-title">
        <span>记录列表</span>
        {total > 0 && <span className="growth-section-count">共 {total} 条</span>}
      </h2>
      {loading && entries.length === 0 ? (
        <EntryListSkeleton count={3} />
      ) : error && entries.length === 0 ? (
        <div className="growth-state growth-state--error">{error}</div>
      ) : entries.length === 0 ? (
        <div className="growth-state growth-state--empty">暂无记录</div>
      ) : (
        <>
          {entries.map((entry) => (
            <div
              key={entry.id}
              ref={(el) => setEntryRef(entry.id, el)}
              className={`growth-entry-wrap${
                highlightEntryId === entry.id ? ' growth-entry-wrap--highlight' : ''
              }`}
            >
              <EntryCard
                entry={entry}
                canEditDeleteOwnEntries={canEditDeleteOwnEntries}
                onAppendBelow={handleAppendBelow}
                onAttachmentDeleted={removeAttachmentFromEntry}
                onAttachmentsAdded={addAttachmentsToEntry}
                onEntryUpdated={handleEntryUpdated}
                onEntryDeleted={handleEntryDeleted}
                onMutationForbidden={handleMutationForbidden}
              />
            </div>
          ))}
          {loadingMore && <EntryListSkeleton count={2} />}
          <div ref={sentinelRef} className="h-4" />
          {!hasMore && entries.length > 0 && (
            <div className="growth-list-end">已加载全部 {total} 条记录</div>
          )}
        </>
      )}
    </section>
  );

  return (
    <div
      className={`growth-app-shell${isMobile ? ' growth-app-shell--mobile' : ''}${
        isMobile && activeTab === 'ai' ? ' growth-app-shell--ai-tab' : ''
      }`}
    >
      <Header />

      {/* 桌面端布局 — 与移动互斥，禁止双主界面 */}
      {!isMobile && (
        <main className="growth-main">
          <section className="growth-page-toolbar" aria-label="页面工具栏">
            <div>
              <h1 className="growth-page-toolbar-title">工作台</h1>
              <p className="growth-page-toolbar-subtitle">记录、检索与复盘</p>
            </div>
            <button
              type="button"
              onClick={() => setAiPanelOpen(true)}
              className="growth-ai-button"
              aria-label="打开 AI 助手"
              title="AI 助手"
            >
              <Sparkles size={16} aria-hidden="true" />
              <span>AI 助手</span>
            </button>
          </section>

          <section className="growth-workspace-tabs" aria-label="工作区切换">
            <button
              type="button"
              className={`growth-workspace-tab ${desktopWorkspace === 'entries' ? 'growth-workspace-tab--active' : ''}`}
              onClick={() => handleDesktopWorkspaceChange('entries')}
            >
              记录
            </button>
            <button
              type="button"
              className={`growth-workspace-tab ${desktopWorkspace === 'todos' ? 'growth-workspace-tab--active' : ''}`}
              onClick={() => handleDesktopWorkspaceChange('todos')}
            >
              小要事
            </button>
            <button
              type="button"
              className={`growth-workspace-tab ${desktopWorkspace === 'insights' ? 'growth-workspace-tab--active' : ''}`}
              onClick={() => handleDesktopWorkspaceChange('insights')}
            >
              洞察
            </button>
          </section>

          {desktopWorkspace === 'todos' ? (
            <TodosSection />
          ) : desktopWorkspace === 'insights' ? (
            <InsightsWorkspace onEntryOpen={handleOpenInsightEntry} />
          ) : (
            <>
              <section className="growth-label-panel" aria-label="标签筛选">
                {labels.length > 0 ? (
                  <LabelFilter
                    labels={labels}
                    selectedCode={selectedLabelCode}
                    onSelect={(code) => {
                      if (code === 'todo') {
                        handleDesktopWorkspaceChange('todos');
                      } else {
                        setSelectedLabelCode(code);
                      }
                    }}
                  />
                ) : (
                  <div className="growth-muted-text">
                    {error ? error : '标签加载中...'}
                  </div>
                )}
              </section>

              <div ref={formRef}>
                <EntryForm
                  labels={labels}
                  onSubmit={handleCreateEntry}
                  appendTarget={appendTarget}
                  onCancelAppend={() => setAppendTarget(null)}
                  loading={submitting}
                />
              </div>

              {renderEntryList()}
            </>
          )}
        </main>
      )}

      {/* 移动端内容区域 — 与桌面互斥 */}
      {isMobile && (
        <div className="mobile-content">
          {activeTab === 'entries' && (
            <div className="mobile-entries">
              <section className="mobile-toolbar" aria-label="记录工具栏">
                <h1 className="mobile-toolbar-title">记录</h1>
                <p className="mobile-toolbar-subtitle">筛选与浏览</p>
              </section>
              <section className="growth-label-panel mobile-label-panel" aria-label="标签筛选">
                {labels.length > 0 ? (
                  <LabelFilter
                    labels={labels}
                    selectedCode={selectedLabelCode}
                    onSelect={handleMobileLabelSelect}
                  />
                ) : (
                  <div className="growth-muted-text">{error ? error : '标签加载中...'}</div>
                )}
              </section>
              {renderEntryList()}
            </div>
          )}

          {activeTab === 'todos' && (
            <div className="mobile-pane">
              <TodosSection />
            </div>
          )}

          {activeTab === 'ai' && (
            <div className="mobile-ai-workspace">
              <AIMPContent />
            </div>
          )}

          {activeTab === 'insights' && (
            <div className="mobile-pane">
              <InsightsWorkspace onEntryOpen={handleOpenInsightEntry} />
            </div>
          )}
        </div>
      )}

      {isMobile && (
        <BottomTabBar
          activeTab={activeTab}
          onTabChange={handleTabChange}
          onCreateClick={() => setCreateModalOpen(true)}
        />
      )}

      {isMobile && (
        <EntryFormModal
          isOpen={createModalOpen}
          onClose={() => {
            setCreateModalOpen(false);
            setAppendTarget(null);
          }}
          onSubmit={handleCreateFromModal}
          labels={labels}
          appendTarget={appendTarget}
          onCancelAppend={() => setAppendTarget(null)}
        />
      )}

      {/* 桌面 AI 侧栏；移动端用 Tab → AIMPContent，禁止双 AI */}
      {!isMobile && (
        <AIAssistantPanel isOpen={aiPanelOpen} onClose={() => setAiPanelOpen(false)} />
      )}
    </div>
  );
}

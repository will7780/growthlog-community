"""
AI 路由
POST /api/ai/search - RAG 搜索（单次）
POST /api/ai/chat - AI 对话（检索模式）
POST /api/ai/query/chat - AI 对话（查询模式，原 Agent）
POST /api/ai/agent/chat - 兼容旧查询模式 endpoint
POST /api/ai/hermes/chat - AI 对话（Hermes Agent 模式）
GET /api/ai/chat/history - 获取对话历史
GET /api/ai/chat/sessions - 获取会话列表
DELETE /api/ai/chat/sessions/{session_id} - 删除会话
POST /api/ai/embeddings/generate - 为记录生成向量
"""
from fastapi import APIRouter, Depends, HTTPException, Response, status, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Dict
from datetime import datetime

from app.timeutil import now_local

from app.database import get_db
from app.auth import get_current_user, User
from app.services.embedding import generate_embedding, generate_title_content_embedding
from app.services.vector_search import search_similar_entries, get_entry_with_label
from app.services.ai_summary import generate_summary, generate_structured_summary
from app.services.agent_tools import build_agent_observations, generate_query_answer
from app.services.agent_compaction import (
    compact_conversation_summary,
    compact_conversation_summary_llm,
    refresh_conversation_summary,
)
from app.services.agent_query_engine import (
    build_agent_observations_v2_async,
    normalize_agent_planner,
    normalize_agent_tool_rounds,
)
from app.services.hermes_bridge import build_hermes_prompt, hermes_error_message, invoke_hermes
from app.services.rag_pipeline import expand_references_for_generation, retrieve_rag_context
from app.services.ai_chat import (
    save_message,
    get_session_history,
    get_user_sessions,
    create_new_session,
    delete_session,
    SessionLimitReachedError,
)
from app.services.ai_session import (
    create_persona_session,
    list_user_sessions,
    validate_session_for_mode,
    SessionNotFoundError,
    SessionConflictError,
    get_or_create_persona_session,
)
from app.services.ai_session_constants import SessionPersonaError
from app.services.ai_source_set import (
    SourceMemberIdentity,
    SourceSetError,
    create_initial_proposal,
    create_expansion_proposal,
    confirm_proposal,
    list_source_set_versions,
    check_and_mark_stale_if_changed,
)
from app.services.review_scope import resolve_review_scope_entries
from app.services.review_service import generate_review_preview
from app.services.organize_service import (
    generate_organize_preview,
    load_recent_entries,
    load_user_entries_by_ids,
    confirm_organize_items,
    preview_organize_scope,
)
from app.services.derived_content import (
    REVIEW_CONFIRM_TYPES,
    build_derived_metadata,
    compose_action_plan_content,
    compose_tag_suggestion_content,
    create_derived_content,
    list_derived_contents,
)
from app.services.annotation_service import generate_annotation_preview, confirm_annotation
from app.services.model_permissions import (
    list_ai_models_for_user,
    resolve_model_key_for_mode,
    get_fallback_candidates,
)
from app.services.llm_gateway import any_llm_configured, ensure_model_provider_ready, LLMGatewayError
from app.config import settings

router = APIRouter()


# ========== Pydantic Models ==========

class AIBaseModel(BaseModel):
    """AI route models that may use model_* field names without Pydantic warnings."""

    model_config = ConfigDict(protected_namespaces=())


class AISearchRequest(AIBaseModel):
    """AI 搜索请求"""
    query: str = Field(..., min_length=1, max_length=500, description="用户问题")
    label_code: Optional[str] = Field(default=None, description="可选，限定标签")
    top_k: int = Field(default=15, ge=1, le=30, description="返回数量")
    model_key: Optional[str] = Field(default=None, description="LLM 模型 key")
    intent: Optional[str] = Field(
        default=None,
        description="可选：lookup_one / lookup_all / answer；缺省由服务端检测",
    )


class ReferenceItem(BaseModel):
    """有效引用记录项（对外契约：不暴露 chunk_id / reference_key 等内部字段）"""
    entry_id: Optional[int] = None
    source_id: Optional[int] = None
    title: str
    label_name: str
    created_at: Optional[str]
    snippet: str
    relevance_score: float
    confidence: Optional[float] = None
    source_reason: Optional[str] = None
    source_type: str = "entry"
    attachment_id: Optional[int] = None
    page_no: Optional[int] = None
    slide_no: Optional[int] = None
    modality: Optional[str] = None
    retrieval_method: Optional[str] = None
    retrieval_sources: List[str] = Field(default_factory=list)
    # Option B: child hit → parent/root for detail UI (does not change attachment ownership).
    parent_id: Optional[int] = None
    root_entry_id: Optional[int] = None
    parent_title: Optional[str] = None
    parent_snippet: Optional[str] = None
    metadata: Dict = Field(default_factory=dict)


class InvalidReferenceItem(BaseModel):
    """无效引用项（LLM 判断不相关）"""
    entry_id: Optional[int] = None
    reason: str


class AISearchResponse(BaseModel):
    """AI 搜索响应 - 新格式（区分有效/无效引用）"""
    answer: str
    valid_references: List[ReferenceItem]
    invalid_references: List[InvalidReferenceItem]
    # R5.4: matches = LLM-relevant full set; used_references = answer citations only.
    intent: Optional[str] = None
    found: Optional[bool] = None
    matches: List[ReferenceItem] = Field(default_factory=list)
    used_references: List[ReferenceItem] = Field(default_factory=list)
    candidate_count: Optional[int] = None
    judged_count: Optional[int] = None
    lookup_error_code: Optional[str] = None


class EmbeddingGenerateRequest(BaseModel):
    """生成向量请求"""
    entry_id: int = Field(..., ge=1, description="记录ID")


class EmbeddingGenerateResponse(BaseModel):
    """生成向量响应"""
    entry_id: int
    success: bool
    message: str


# ========== Chat Models ==========

class ChatMessageRequest(AIBaseModel):
    """发送消息请求"""
    session_id: Optional[str] = Field(default=None, description="会话ID，为空则创建新会话")
    message: str = Field(..., min_length=1, max_length=500, description="用户消息")
    model_key: Optional[str] = Field(default=None, description="LLM 模型 key")
    agent_planner: Optional[str] = Field(
        default=None,
        description="Agent 工具选择策略：null/rule=规则 planner，llm=可选 LLM planner",
    )
    agent_tool_rounds: Optional[int] = Field(
        default=None,
        ge=1,
        le=2,
        description="Agent 工具执行轮数：1=单轮（默认），2=LLM planner 下最多再补一轮只读工具",
    )


class ChatMessageResponse(AIBaseModel):
    """发送消息响应 - 新格式"""
    session_id: str
    message: str
    valid_references: List[ReferenceItem]
    invalid_references: List[InvalidReferenceItem]
    mode: str = "retrieval"
    agent_steps: List[Dict] = Field(default_factory=list)
    analysis: Optional[Dict] = None
    model_fallback: Optional["ModelFallbackInfo"] = None
    retrieval_results: List[ReferenceItem] = Field(default_factory=list)
    used_references: List[ReferenceItem] = Field(default_factory=list)
    intent: Optional[str] = None
    found: Optional[bool] = None
    candidate_count: Optional[int] = None
    judged_count: Optional[int] = None


class ModelFallbackInfo(AIBaseModel):
    requested_model_key: str
    used_model_key: str
    fallback_reason: str


def _ensure_ai_ready(db: Session, current_user: User) -> None:
    from app.models import Entry

    entry_count = db.query(Entry).filter(Entry.user_id == current_user.id).count()
    if entry_count < 5:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_ENTRIES",
                "message": "记录数量太少啦，要先记多一点噢",
            },
        )

    if not any_llm_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "LLM_NOT_CONFIGURED",
                "message": "AI 模型服务未配置，请联系管理员",
            },
        )


def _resolve_request_model(db: Session, user: User, mode: str, model_key: Optional[str]) -> str:
    return resolve_model_key_for_mode(db, user, mode, model_key)


def _fallback_candidates(db: Session, user: User, requested_model_key: str) -> List[str]:
    return get_fallback_candidates(db, user, requested_model_key)


def _model_fallback_info(raw: Optional[dict]) -> Optional[ModelFallbackInfo]:
    if not raw or not raw.get("fallback_reason"):
        return None
    return ModelFallbackInfo(
        requested_model_key=str(raw["requested_model_key"]),
        used_model_key=str(raw["used_model_key"]),
        fallback_reason=str(raw["fallback_reason"]),
    )


def _llm_unavailable_http(exc: LLMGatewayError) -> HTTPException:
    """Map typed LLM failures to a user-safe 503 (never leak stack/prompt/keys)."""
    code = getattr(exc, "code", None) or "LLM_PROVIDER_FAILED"
    if code == "LLM_NOT_CONFIGURED":
        message = "AI 模型服务未配置，请联系管理员"
    elif code == "LLM_PROVIDER_TIMEOUT":
        message = "AI 服务响应超时，请稍后重试"
    elif code == "GENERATION_CONTRACT_FAILED":
        message = "AI 回答结构校验失败，请稍后重试"
    else:
        message = "AI 服务暂时不可用，请稍后重试"
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": code, "message": message},
    )


def _session_error_http(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "SESSION_ERROR")
    message = getattr(exc, "message", str(exc))
    status_code = status.HTTP_404_NOT_FOUND if code == "SESSION_NOT_FOUND" else status.HTTP_400_BAD_REQUEST
    if code in {
        "SESSION_PERSONA_MISMATCH",
        "SESSION_PERSONA_INVALID",
        "SESSION_CONFLICT",
        "SESSION_LEGACY_READONLY",
        "SESSION_MESSAGE_LIMIT",
    }:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _source_set_error_http(exc: SourceSetError) -> HTTPException:
    code = exc.code
    status_code = status.HTTP_409_CONFLICT
    if code in {"SOURCE_NOT_FOUND", "SOURCE_SET_NOT_FOUND", "SESSION_NOT_FOUND"}:
        status_code = status.HTTP_404_NOT_FOUND
    if code in {
        "SOURCE_SET_EMPTY",
        "SOURCE_TYPE_INVALID",
        "SOURCE_SET_VERSION_INVALID",
        "SOURCE_REFERENCE_INVALID",
        "EMPTY_MESSAGE",
        "NO_PENDING_QUESTION",
        "SOURCE_EXPANSION_QUERY_REQUIRED",
    }:
        status_code = status.HTTP_400_BAD_REQUEST
    if code in {
        "SOURCE_SET_STALE",
        "SOURCE_CONFIRM_PENDING",
        "SOURCE_SET_NOT_CONFIRMED",
        "SOURCE_SET_VERSION_CONFLICT",
        "SOURCE_SET_IMMUTABLE",
        "SOURCE_SET_ALREADY_CONFIRMED",
        "NOTION_SOURCE_SYNC_PENDING",
    }:
        status_code = status.HTTP_409_CONFLICT
    return HTTPException(status_code=status_code, detail={"code": code, "message": exc.message})


def _get_or_create_session_id(db: Session, user_id: int, session_id: Optional[str]) -> str:
    """Legacy helper for non-retriever routes (organizer/query compatibility)."""
    if session_id:
        return session_id
    try:
        return create_new_session(db, user_id)
    except SessionLimitReachedError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "SESSION_LIMIT_REACHED",
                "message": "会话已达上限，请先手动删除旧会话后再新建。",
            },
        )


def _get_or_create_retriever_session_id(
    db: Session, user_id: int, session_id: Optional[str]
) -> str:
    from app.services.ai_session import get_or_create_persona_session

    try:
        row = get_or_create_persona_session(
            db, user_id, "retriever", session_id=session_id
        )
        return str(row.session_id)
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SessionConflictError as exc:
        raise _session_error_http(exc) from exc


def _normalize_created_at(created_at) -> Optional[str]:
    if created_at is None:
        return None
    if hasattr(created_at, "isoformat"):
        return created_at.isoformat()[:10]
    if isinstance(created_at, str):
        return created_at[:10] if len(created_at) >= 10 else created_at
    return str(created_at)[:10]


def _public_ref_metadata(meta: Optional[Dict]) -> Dict:
    """Strip internal retrieval identifiers from client-facing metadata."""
    if not isinstance(meta, dict):
        return {}
    blocked = {"chunk_id", "reference_key", "embedding_id", "vector_id", "origin_id", "todo_id", "notion_page_id", "root_todo_id"}
    return {k: v for k, v in meta.items() if k not in blocked}


def _enrich_reference(ref: Dict, source_reason: Optional[str] = None) -> ReferenceItem:
    score = float(ref.get("relevance_score", 0.0) or 0.0)
    metadata = _public_ref_metadata(ref.get("metadata") or {})
    attachment_id = ref.get("attachment_id")
    if attachment_id is None and metadata.get("origin_type") == "attachment":
        attachment_id = metadata.get("origin_id")
    try:
        attachment_id = int(attachment_id) if attachment_id is not None else None
    except (TypeError, ValueError):
        attachment_id = None

    def _optional_int(value) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    parent_id = _optional_int(ref.get("parent_id") or metadata.get("parent_id"))
    root_entry_id = _optional_int(ref.get("root_entry_id") or metadata.get("root_entry_id"))
    if root_entry_id is None:
        root_entry_id = parent_id if parent_id is not None else _optional_int(ref.get("entry_id"))

    source_type = str(ref.get("source_type") or "")
    is_opaque_source = source_type in {"todo", "notion_page"}
    return ReferenceItem(
        entry_id=None if is_opaque_source else ref.get("entry_id"),
        source_id=None if is_opaque_source else (ref.get("source_id") or attachment_id or ref.get("entry_id")),
        title=ref.get("title", ""),
        label_name=ref.get("label_name", ""),
        created_at=_normalize_created_at(ref.get("created_at")),
        snippet=ref.get("snippet", ""),
        relevance_score=score,
        confidence=ref.get("confidence", score),
        source_reason=ref.get("source_reason", source_reason),
        source_type=ref.get("source_type", "entry"),
        attachment_id=attachment_id,
        page_no=ref.get("page_no"),
        slide_no=ref.get("slide_no"),
        modality=ref.get("modality"),
        retrieval_method=ref.get("retrieval_method"),
        retrieval_sources=ref.get("retrieval_sources") or [],
        parent_id=parent_id,
        root_entry_id=root_entry_id,
        parent_title=ref.get("parent_title") or metadata.get("parent_title"),
        parent_snippet=ref.get("parent_snippet") or metadata.get("parent_snippet"),
        metadata=metadata,
    )

def _observations_to_valid_refs(observations: Dict) -> List[ReferenceItem]:
    return [
        _enrich_reference(ref, source_reason=ref.get("source_reason", "query_evidence"))
        for ref in observations.get("references", [])
    ]


def _load_review_scope_entries(db: Session, user_id: int, request) -> tuple:
    """解析复盘范围，返回 (entries, source_entry_ids)。"""
    entries = resolve_review_scope_entries(
        db,
        user_id,
        scope_type=getattr(request, "scope_type", None) or "recent_7d",
        entry_ids=getattr(request, "entry_ids", None),
        tag=getattr(request, "tag", None),
        start_date=getattr(request, "start_date", None),
        end_date=getattr(request, "end_date", None),
    )
    source_entry_ids = [entry.id for entry in entries]
    return entries, source_entry_ids


def _entries_to_valid_refs(db: Session, entries: List) -> List[ReferenceItem]:
    from app.models import EntryLabel

    if not entries:
        return []
    label_codes = {e.label_code for e in entries if getattr(e, "label_code", None)}
    labels = (
        db.query(EntryLabel).filter(EntryLabel.code.in_(label_codes)).all()
        if label_codes
        else []
    )
    label_map = {lb.code: lb.name for lb in labels}
    refs: List[ReferenceItem] = []
    for entry in entries[:20]:
        content = entry.content or ""
        title = content.split("\n")[0][:100] if content else "无标题"
        snippet = content[:200]
        refs.append(
            ReferenceItem(
                entry_id=int(entry.id),
                title=title,
                label_name=label_map.get(entry.label_code, entry.label_code or ""),
                created_at=_normalize_created_at(entry.created_at),
                snippet=snippet,
                relevance_score=1.0,
                confidence=1.0,
                source_reason="review_scope",
            )
        )
    return refs


class ChatHistoryResponse(BaseModel):
    """对话历史响应"""
    session_id: str
    messages: List[Dict]


class ChatSessionItem(BaseModel):
    """会话项"""
    session_id: str
    last_message: str = ""
    role: Optional[str] = None
    persona: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    legacy: bool = False
    continuable: bool = True
    source_set_status: Optional[str] = None
    current_source_set_version: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ChatSessionsResponse(BaseModel):
    """会话列表响应"""
    sessions: List[ChatSessionItem]
    total: Optional[int] = None


class CreateChatSessionRequest(AIBaseModel):
    persona: str = Field(..., min_length=1, max_length=32)
    title: Optional[str] = Field(default=None, max_length=255)
    session_id: Optional[str] = Field(default=None, max_length=64)


class CreateChatSessionResponse(AIBaseModel):
    session_id: str
    persona: str
    title: Optional[str] = None
    status: str
    legacy: bool = False
    continuable: bool = True


class SourceSetMemberInput(AIBaseModel):
    """Canonical reference_key only; ownership/family fields are derived server-side."""

    model_config = ConfigDict(protected_namespaces=(), extra="forbid")

    reference_key: str = Field(..., min_length=3, max_length=128)
    display_order: int = Field(default=0, ge=0)


class SourceSetProposalRequest(AIBaseModel):
    members: List[SourceSetMemberInput] = Field(default_factory=list)
    base_version: Optional[int] = Field(default=None, ge=0)


class SourceSetProposalResponse(AIBaseModel):
    proposal_id: int
    session_id: str
    base_version: Optional[int] = None
    status: str


class SourceSetConfirmRequest(AIBaseModel):
    proposal_id: int = Field(..., ge=1)
    base_version: int = Field(..., ge=0)
    selected_reference_keys: Optional[List[str]] = Field(
        default=None,
        description="可选：勾选的候选 reference_key 或 family root entry:source:<id>；省略则确认全部候选",
    )
    selected_ref_tokens: Optional[List[str]] = Field(
        default=None,
        description="R11.3：勾选的 opaque ref_token；服务端校验后映射为 reference_key，优先于 selected_reference_keys",
    )


class SourceSetConfirmResponse(AIBaseModel):
    session_id: str
    version: int
    content_fingerprint: str
    status: str
    member_count: int


class SourceSetVersionItem(AIBaseModel):
    id: int
    version: Optional[int] = None
    base_version: Optional[int] = None
    status: str
    content_fingerprint: Optional[str] = None
    member_count: int
    locked_at: Optional[str] = None
    created_at: Optional[str] = None


class SourceSetVersionsResponse(AIBaseModel):
    session_id: str
    current_version: Optional[int] = None
    versions: List[SourceSetVersionItem] = Field(default_factory=list)


class LockedSourceMemberItem(AIBaseModel):
    display_index: int
    ref_token: str
    source_type: str
    title: str
    snippet: str = ""


class CurrentLockedSourcesResponse(AIBaseModel):
    session_id: str
    version: Optional[int] = None
    status: Optional[str] = None
    member_count: int = 0
    stale: bool = False
    can_expand: bool = False
    members: List[LockedSourceMemberItem] = Field(default_factory=list)
    append_only_note: str = "讲解员来源只可追加；缩减范围请新建对话。"


class SourceSetExpandRequest(AIBaseModel):
    # No pydantic min/max length: empty/short/long must become HTTP 400
    # SOURCE_EXPANSION_QUERY_REQUIRED via validate_expansion_topic (not 422).
    query: Optional[str] = Field(
        default=None,
        description="补充来源主题（必填，trim 后 2～200 字）",
    )


class ExplainerCandidatesRequest(AIBaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=500)


class ExplainerCandidateItem(AIBaseModel):
    reference_key: Optional[str] = None
    ref_token: Optional[str] = None
    display_index: Optional[int] = None
    source_type: str
    title: str
    snippet: str = ""
    family_root_entry_id: Optional[int] = None
    display_order: int = 0


def _public_explainer_candidates(
    candidates: List[Dict], *, user_id: int, session_id: str
) -> List[ExplainerCandidateItem]:
    """Keep legacy record payloads compatible; Todo candidates are token-only."""
    from app.services.ai_stream_protocol import sanitize_candidate_for_stream

    public: List[ExplainerCandidateItem] = []
    for index, candidate in enumerate(candidates or [], start=1):
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("source_type") or "") == "todo":
            candidate = sanitize_candidate_for_stream(
                candidate,
                user_id=int(user_id),
                session_id=session_id,
                display_index=index,
            )
        public.append(ExplainerCandidateItem(**candidate))
    return public


def _public_explainer_expansion(
    expansion: Optional[Dict], *, user_id: int, session_id: str
) -> Optional[Dict]:
    if not isinstance(expansion, dict):
        return expansion
    public = dict(expansion)
    public["candidates"] = [
        item.model_dump(exclude_none=True)
        for item in _public_explainer_candidates(
            list(expansion.get("candidates") or []),
            user_id=user_id,
            session_id=session_id,
        )
    ]
    return public


class ExplainerCandidatesResponse(AIBaseModel):
    session_id: str
    proposal_id: int
    base_version: int = 0
    status: str
    query: str
    candidates: List[ExplainerCandidateItem] = Field(default_factory=list)
    message: str


class ExplainerChatRequest(AIBaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)
    model_key: Optional[str] = None


class ExplainerResumeRequest(AIBaseModel):
    session_id: str = Field(..., min_length=1, max_length=64)
    proposal_id: int = Field(..., ge=1)
    model_key: Optional[str] = None


class StreamTurnRequest(AIBaseModel):
    """R11.3: shared body for /retrieve/stream and /explain/stream."""
    message: str = Field(..., min_length=1, max_length=2000)
    request_id: str = Field(..., min_length=8, max_length=64)
    model_key: Optional[str] = None


class StreamResumeRequest(AIBaseModel):
    """R11.3-Fix: body for /explain/resume/stream after source confirm."""
    proposal_id: int = Field(..., ge=1)
    request_id: str = Field(..., min_length=8, max_length=64)
    model_key: Optional[str] = None


class ReferencePreviewRequest(AIBaseModel):
    """R11.3-Fix: opaque ref_token in body only (never URL/query)."""
    ref_token: str = Field(..., min_length=8, max_length=4096)


class ExplainerChatResponse(AIBaseModel):
    session_id: str
    mode: str = "explainer"
    phase: str
    message: str
    answer: Optional[str] = None
    proposal_id: Optional[int] = None
    base_version: Optional[int] = None
    candidates: List[ExplainerCandidateItem] = Field(default_factory=list)
    valid_references: List[ReferenceItem] = Field(default_factory=list)
    invalid_references: List[InvalidReferenceItem] = Field(default_factory=list)
    source_set_version: Optional[int] = None
    expansion: Optional[Dict] = None
    model_fallback: Optional[ModelFallbackInfo] = None
    context_meta: Optional[Dict] = None
    resumed: Optional[bool] = None
    idempotent: Optional[bool] = None


class ChatDeleteResponse(BaseModel):
    """删除会话响应"""
    success: bool
    message: str


class ChatSessionCompactResponse(BaseModel):
    """会话压缩摘要响应"""
    session_id: str
    summary: str
    structured_json: Dict = Field(default_factory=dict)
    message_count: int
    last_message_at: Optional[str] = None


class AgentMemoryItem(BaseModel):
    """Agent 长期记忆项"""
    id: int
    memory_type: str
    content: str
    source: str
    confidence: float
    is_active: bool
    created_at: Optional[str]
    updated_at: Optional[str]
    quality_warnings: List[Dict] = Field(default_factory=list)


class AgentMemoryCreateRequest(BaseModel):
    """创建长期记忆"""
    memory_type: str = Field(..., pattern="^(goal|preference|project|profile|insight)$")
    content: str = Field(..., min_length=1, max_length=2000)
    source: str = Field(default="user", max_length=100)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class AgentMemoryUpdateRequest(BaseModel):
    """更新长期记忆"""
    memory_type: Optional[str] = Field(default=None, pattern="^(goal|preference|project|profile|insight)$")
    content: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    is_active: Optional[bool] = None


class AgentMemoryAnalyzeRequest(BaseModel):
    """分析候选长期记忆，不写入数据库"""
    memory_type: str = Field(..., pattern="^(goal|preference|project|profile|insight)$")
    content: str = Field(..., min_length=1, max_length=2000)
    exclude_memory_id: Optional[int] = None


class AgentMemoryAnalyzeResponse(BaseModel):
    """长期记忆质量分析结果"""
    normalized_content: str
    quality_warnings: List[Dict] = Field(default_factory=list)


class AgentMemoryMergeRequest(BaseModel):
    """用户确认后的长期记忆合并请求"""
    source_memory_ids: List[int] = Field(..., min_length=1, max_length=20)
    content: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class AgentMemoryMergeResponse(BaseModel):
    """长期记忆合并结果"""
    memory: AgentMemoryItem
    deactivated_memory_ids: List[int]


class AgentPendingActionItem(BaseModel):
    """Agent 待确认动作"""
    id: int
    session_id: Optional[str]
    action_type: str
    payload_json: Dict
    status: str
    result_json: Optional[Dict] = None
    error_message: Optional[str] = None
    created_by_message_id: Optional[int] = None
    confirmed_at: Optional[str] = None
    executed_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentPendingActionCreateRequest(BaseModel):
    """创建 Agent 待确认动作"""
    action_type: str = Field(..., pattern="^(create_todo|create_memory|update_memory|save_summary_entry)$")
    payload_json: Dict = Field(default_factory=dict)
    session_id: Optional[str] = Field(default=None, max_length=64)
    created_by_message_id: Optional[int] = None


class AgentToolItem(BaseModel):
    """Agent 工具定义"""
    name: str
    title: str
    permission: str


class AgentToolAuditItem(BaseModel):
    """Agent 工具调用审计"""
    id: int
    session_id: str
    tool_name: str
    permission: str
    status: str
    input_json: Optional[Dict] = None
    output_summary: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


class ReviewPreviewRequest(BaseModel):
    scope_type: Optional[str] = Field(
        default="recent_7d",
        description="recent|recent_7d|recent_30d|range|tag|entries",
    )
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    tag: Optional[str] = None
    entry_ids: Optional[List[int]] = None
    goal: Optional[str] = None
    message: Optional[str] = None


class ReviewPreviewResponse(BaseModel):
    status: str = "preview"
    title: str = ""
    summary: str
    content: str = ""
    trends: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)
    suggested_outputs: List[Dict] = Field(default_factory=list)
    source_entry_ids: List[int] = Field(default_factory=list)
    scope_type: Optional[str] = None
    entry_ids: Optional[List[int]] = None
    tag: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    episode_id: Optional[str] = None
    facet: Optional[str] = None
    facet_points: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    confidence: Optional[float] = None


def _build_review_preview_response(
    preview: Dict,
    source_entry_ids: List[int],
    scope_type: str,
    *,
    entry_ids: Optional[List[int]] = None,
    tag: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> ReviewPreviewResponse:
    return ReviewPreviewResponse(
        status="preview",
        title=preview.get("title") or "AI 复盘",
        summary=preview["summary"],
        content=preview.get("content") or preview["summary"],
        trends=preview.get("trends") or [],
        keywords=preview.get("keywords") or [],
        suggestions=preview.get("suggestions") or [],
        source_entry_ids=source_entry_ids,
        scope_type=scope_type,
        entry_ids=entry_ids,
        tag=tag,
        start_date=start_date,
        end_date=end_date,
        episode_id=preview.get("episode_id"),
        facet=preview.get("facet"),
        facet_points=preview.get("facet_points") or [],
        entities=preview.get("entities") or [],
        confidence=preview.get("confidence"),
        suggested_outputs=preview.get("suggested_outputs") or [],
    )


def _resolve_review_confirm_source_ids(db: Session, user_id: int, request) -> List[int]:
    """校验 confirm 范围：优先 source_entry_ids，并与 scope 参数交叉验证。"""
    preview_ids = sorted(
        {int(i) for i in (getattr(request, "source_entry_ids", None) or []) if i}
    )
    _, resolved_ids = _load_review_scope_entries(db, user_id, request)
    resolved_sorted = sorted(set(resolved_ids))

    if preview_ids:
        valid_entries = load_user_entries_by_ids(db, user_id, preview_ids)
        valid_ids = sorted({int(e.id) for e in valid_entries})
        if valid_ids != preview_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "REVIEW_SCOPE_MISMATCH",
                    "message": "复盘来源记录与预览不一致或包含无效记录，请重新生成预览后再保存",
                },
            )
        if resolved_sorted and resolved_sorted != preview_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "REVIEW_SCOPE_MISMATCH",
                    "message": "确认保存的范围与预览范围不一致，请重新生成预览后再保存",
                },
            )
        return preview_ids

    if not resolved_sorted:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "REVIEW_SCOPE_EMPTY", "message": "复盘范围为空，无法保存"},
        )
    return resolved_sorted


class ReviewConfirmRequest(BaseModel):
    type: str = Field(default="review", description="review|action_plan|tag_suggestion")
    scope_type: Optional[str] = Field(default="recent_7d")
    entry_ids: Optional[List[int]] = None
    tag: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    source_entry_ids: Optional[List[int]] = None
    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    metadata: Optional[Dict] = None


class ReviewConfirmResponse(BaseModel):
    id: int
    type: str
    source_entry_ids: List[int]
    created_at: str


class ReviewChatRequest(AIBaseModel):
    session_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=500)
    model_key: Optional[str] = None
    scope_type: Optional[str] = Field(default="recent_7d")
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    tag: Optional[str] = None
    entry_ids: Optional[List[int]] = None
    goal: Optional[str] = None


class ReviewChatResponse(AIBaseModel):
    session_id: str
    message: str
    mode: str = "query"
    review_preview: ReviewPreviewResponse
    valid_references: List[ReferenceItem] = Field(default_factory=list)
    model_fallback: Optional[ModelFallbackInfo] = None


class OrganizeSourceDisplay(BaseModel):
    title: str = ""
    created_at: str = ""
    kind: str = "entry"
    label_code: str = ""
    ref_token: Optional[str] = None


class DiffPreviewItem(BaseModel):
    action: str
    target_type: str = "derived_content"
    target_id: int | str = "pending"
    before: str = ""
    after: str = ""
    reason: str = ""
    risk: str = "medium"
    source_entry_ids: List[int] = Field(default_factory=list)
    sources_display: List[OrganizeSourceDisplay] = Field(default_factory=list)
    confidence: Optional[float] = None
    title: Optional[str] = None
    content: Optional[str] = None
    item_id: Optional[str] = None
    preview_id: Optional[str] = None


class OrganizeScopeRequest(AIBaseModel):
    days: Optional[int] = Field(default=30, description="7/30/90 or null=all")
    label_codes: Optional[List[str]] = None
    entry_ids: Optional[List[int]] = None


class OrganizeScopePreviewResponse(BaseModel):
    matched_count: int
    selected_count: int
    date_from: Optional[str] = None
    days: Optional[int] = None
    label_codes: List[str] = Field(default_factory=list)
    truncated: bool = False
    max_entries: int = 40
    source_entry_ids: List[int] = Field(default_factory=list)


class OrganizePreviewRequest(AIBaseModel):
    entry_ids: Optional[List[int]] = None
    days: Optional[int] = Field(default=30)
    label_codes: Optional[List[str]] = None
    goal: Optional[str] = None
    allowed_actions: Optional[List[str]] = None
    model_key: Optional[str] = None


class HermesProtocolInfo(BaseModel):
    protocol_version: Optional[str] = None
    request_id: Optional[str] = None
    via: Optional[str] = None
    permission_level: Optional[str] = None
    bridge_execution_source: Optional[str] = None
    runtime_scope: Optional[str] = None


class OrganizeScopeInfo(BaseModel):
    matched_count: int = 0
    selected_count: int = 0
    date_from: Optional[str] = None
    days: Optional[int] = None
    label_codes: List[str] = Field(default_factory=list)
    truncated: bool = False


class OrganizePreviewResponse(BaseModel):
    status: str
    message: str
    goal: Optional[str] = None
    diff_preview: List[DiffPreviewItem] = Field(default_factory=list)
    source_entry_ids: List[int] = Field(default_factory=list)
    used_default_entries: bool = False
    preview_id: Optional[str] = None
    preview_token: Optional[str] = None
    preview_session_id: Optional[str] = None
    scope: Optional[OrganizeScopeInfo] = None
    hermes_protocol: Optional[HermesProtocolInfo] = None


class OrganizeChatRequest(AIBaseModel):
    session_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=500)
    model_key: Optional[str] = None
    entry_ids: Optional[List[int]] = None
    days: Optional[int] = Field(default=30)
    label_codes: Optional[List[str]] = None
    goal: Optional[str] = None
    allowed_actions: Optional[List[str]] = None


class OrganizeChatResponse(AIBaseModel):
    session_id: str
    message: str
    mode: str = "organize"
    organize_preview: OrganizePreviewResponse
    valid_references: List[ReferenceItem] = Field(default_factory=list)
    model_fallback: Optional[ModelFallbackInfo] = None


class OrganizeConfirmRequest(BaseModel):
    source_entry_ids: List[int] = Field(default_factory=list)
    items: List[DiffPreviewItem] = Field(..., min_length=1)
    goal: Optional[str] = None
    metadata: Optional[Dict] = None
    preview_id: Optional[str] = None
    preview_token: Optional[str] = None


class OrganizeConfirmCreatedItem(BaseModel):
    id: int
    type: str
    title: str
    source_entry_ids: List[int] = Field(default_factory=list)


class OrganizeConfirmResponse(BaseModel):
    created: List[OrganizeConfirmCreatedItem]
    source_entry_ids: List[int]


class AnnotationPreviewResponse(AIBaseModel):
    status: str = "preview"
    entry_id: int
    source_entry_ids: List[int]
    summary: str
    action_hint: str = ""
    confidence: Optional[float] = None
    model_key: Optional[str] = None
    model_fallback: Optional[ModelFallbackInfo] = None


class AnnotationConfirmRequest(AIBaseModel):
    summary: str = Field(..., min_length=1)
    action_hint: Optional[str] = ""
    confidence: Optional[float] = None
    model_key: Optional[str] = None
    metadata: Optional[Dict] = None


class AnnotationConfirmResponse(BaseModel):
    id: int
    type: str = "annotation"
    source_entry_ids: List[int]
    created_at: str


class DerivedContentItem(BaseModel):
    id: int
    type: str
    title: str
    content: str
    source_entry_ids: List[int]
    metadata: Optional[Dict] = None
    status: str = "confirmed"
    created_at: str


class DerivedContentDetailResponse(BaseModel):
    id: int
    type: str
    title: str
    content: str
    source_entry_ids: List[int]
    metadata: Optional[Dict] = None
    scope_type: Optional[str] = None
    status: str = "confirmed"
    created_at: str
    updated_at: Optional[str] = None


class DerivedContentStatusResponse(BaseModel):
    id: int
    type: str
    status: str
    source_entry_ids: List[int]
    created_at: str
    updated_at: Optional[str] = None


class DerivedContentsResponse(BaseModel):
    items: List[DerivedContentItem]


class ScheduledReviewStatusResponse(BaseModel):
    system_enabled: bool = False
    user_authorized: bool = False
    effective_enabled: bool = False
    enabled: bool = False
    provider: str = ""
    review_days: int = 7
    poll_interval_seconds: int = 1200
    scheduled_review_last_at: Optional[str] = None
    pending_count: int = 0
    pending_entries: int = 0
    draft_count: int = 0
    pending_drafts: int = 0
    last_run_at: Optional[str] = None
    last_error_code: Optional[str] = None
    status: str = "idle"
    message: Optional[str] = None


class ScheduledReviewRunResponse(BaseModel):
    status: str
    processed: int = 0
    source_entry_ids: List[int] = Field(default_factory=list)
    draft_ids: List[int] = Field(default_factory=list)
    scheduled_review_last_at: Optional[str] = None
    processed_until: Optional[str] = None
    message: Optional[str] = None
    preview_outputs: Optional[Dict] = None


class AutoStructureStatusResponse(BaseModel):
    enabled: bool
    provider: str
    poll_interval_seconds: int
    max_records_per_run: int
    max_tokens_per_chunk: int
    last_processed_at: Optional[str] = None
    pending_records: int = 0
    status: str = "idle"
    message: Optional[str] = None


class AutoStructureRunResponse(BaseModel):
    status: str
    processed: int = 0
    source_entry_ids: List[int] = Field(default_factory=list)
    last_processed_at: Optional[str] = None
    processed_until: Optional[str] = None
    next_cursor: Optional[str] = None
    derived_content_ids: List[int] = Field(default_factory=list)
    message: Optional[str] = None
    preview_outputs: Optional[Dict] = None


class AIModelItem(AIBaseModel):
    model_key: str
    display_name: str
    provider: str
    default_modes: List[str] = Field(default_factory=list)
    supports_thinking: bool = False
    description: str = ""
    enabled: bool = True
    is_default: bool = False
    provider_configured: bool = True


class AIModelsResponse(BaseModel):
    models: List[AIModelItem]
    recommended_default: str
    mode: Optional[str] = None


# ========== Routes ==========

@router.post("/search", response_model=AISearchResponse)
async def ai_search(
    request: AISearchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    RAG 搜索（与检索员共用 ai_retrieval_service）。
    """
    from app.models import Entry
    entry_count = db.query(Entry).filter(Entry.user_id == current_user.id).count()
    if entry_count < 5:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_ENTRIES",
                "message": "记录数量太少啦，要先记多一点噢"
            }
        )

    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    try:
        from app.services.ai_retrieval_service import (
            RetrievalServiceError,
            run_shared_retrieval_turn,
        )

        payload = await run_shared_retrieval_turn(
            db,
            user_id=int(current_user.id),
            query=request.query,
            intent_hint=request.intent,
            label_code=request.label_code,
            top_k=int(request.top_k or 30),
            model_key=model_key,
            fallback_candidates=fallback,
            generate_answer=True,
            answer_style="default",
        )
        retrieval_results = [
            _enrich_reference(ref, source_reason=ref.get("source_reason", "hybrid_match"))
            for ref in (payload.get("retrieval_results") or [])
            if isinstance(ref, dict)
        ]
        used_refs = [
            _enrich_reference(ref, source_reason=ref.get("source_reason", "hybrid_match"))
            for ref in (payload.get("used_references") or [])
            if isinstance(ref, dict)
        ]
        invalid_refs = []
        for inv in payload.get("invalid_references") or []:
            if isinstance(inv, dict) and inv.get("entry_id") is not None:
                invalid_refs.append(
                    InvalidReferenceItem(
                        entry_id=int(inv["entry_id"]),
                        reason=str(inv.get("reason") or "内容与问题不相关"),
                    )
                )
        meta = payload.get("meta") or {}
        return AISearchResponse(
            answer=str(payload.get("answer") or ""),
            valid_references=used_refs,
            invalid_references=invalid_refs,
            intent=payload.get("intent"),
            found=payload.get("found"),
            matches=retrieval_results,
            used_references=used_refs,
            candidate_count=meta.get("candidate_count"),
            judged_count=meta.get("judged_count"),
            lookup_error_code=meta.get("lookup_error_code"),
        )

    except RetrievalServiceError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": exc.code,
                "message": exc.message,
                "must_not_treat_as_zero_hits": True,
            },
        ) from exc
    except LLMGatewayError as exc:
        raise _llm_unavailable_http(exc) from exc
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("AI Search 失败: %s", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "AI_SEARCH_FAILED", "message": "搜索失败，请稍后重试"},
        ) from e


@router.post("/embeddings/generate", response_model=EmbeddingGenerateResponse)
def generate_entry_embedding(
    request: EmbeddingGenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    为指定记录生成向量并存储
    """
    from app.models import Entry

    # 验证记录存在且属于当前用户
    entry = db.query(Entry).filter(
        Entry.id == request.entry_id,
        Entry.user_id == current_user.id
    ).first()

    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"记录不存在: {request.entry_id}"
        )

    try:
        # 提取标题和内容
        content = entry.content
        title = content.split("\n")[0] if content else ""
        body = content.replace(title, "").strip() if title else content

        # 生成向量
        vectors = generate_title_content_embedding(title, body)

        # 检查是否已有 embedding
        from app.models import Embedding
        existing = db.query(Embedding).filter(
            Embedding.entry_id == entry.id,
            Embedding.entry_type == "main"
        ).first()

        if existing:
            existing.title_vector = vectors["title_vector"]
            existing.content_vector = vectors["content_vector"]
        else:
            embedding = Embedding(
                entry_id=entry.id,
                entry_type="main",
                title_vector=vectors["title_vector"],
                content_vector=vectors["content_vector"],
            )
            db.add(embedding)

        db.commit()

        return EmbeddingGenerateResponse(
            entry_id=entry.id,
            success=True,
            message="向量生成成功"
        )

    except Exception as e:
        print(f"[Generate Embedding] 生成失败: {e}")
        return EmbeddingGenerateResponse(
            entry_id=request.entry_id,
            success=False,
            message=f"生成失败: {str(e)}"
        )


@router.post("/embeddings/batch-generate")
def batch_generate_embeddings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    批量为用户的所有记录生成向量
    同时更新 FAISS 索引
    """
    from app.models import Entry, Embedding
    from app.services.vector_search import add_entry_to_index

    # 获取用户所有没有向量的记录
    entries_without_embedding = db.query(Entry).filter(
        Entry.user_id == current_user.id,
        ~Entry.id.in_(
            db.query(Embedding.entry_id).filter(Embedding.entry_id == Entry.id)
        )
    ).all()

    success_count = 0
    error_count = 0

    for entry in entries_without_embedding:
        try:
            # 提取标题和内容
            content = entry.content
            title = content.split("\n")[0] if content else ""
            body = content.replace(title, "").strip() if title else content

            # 生成向量
            vectors = generate_title_content_embedding(title, body)

            embedding = Embedding(
                entry_id=entry.id,
                entry_type="main",
                title_vector=vectors["title_vector"],
                content_vector=vectors["content_vector"],
            )
            db.add(embedding)
            db.commit()

            # 更新 FAISS 索引
            add_entry_to_index(
                current_user.id,
                entry.id,
                vectors["title_vector"],
                vectors["content_vector"]
            )

            success_count += 1

        except Exception as e:
            print(f"[Batch Generate] 记录 {entry.id} 生成失败: {e}")
            error_count += 1

    return {
        "success": True,
        "message": f"批量生成完成，成功 {success_count} 条，失败 {error_count} 条",
        "success_count": success_count,
        "error_count": error_count
    }


# ========== Chat Routes ==========

@router.get("/models", response_model=AIModelsResponse)
def get_ai_models(
    mode: Optional[str] = Query(default=None, description="retrieval|review|organize"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回当前用户可用模型与推荐默认模型。"""
    data = list_ai_models_for_user(db, current_user, mode=mode)
    return AIModelsResponse(
        models=[AIModelItem(**m) for m in data["models"]],
        recommended_default=data["recommended_default"],
        mode=data.get("mode"),
    )


@router.post("/chat", response_model=ChatMessageResponse)
async def send_chat_message(
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """检索员聊天：原子创建 retriever persona；每轮独立 raw query + 共享 lookup 链路。"""
    from app.models import Entry
    entry_count = db.query(Entry).filter(Entry.user_id == current_user.id).count()
    if entry_count < 5:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "INSUFFICIENT_ENTRIES",
                "message": "记录数量太少啦，要先记多一点噢"
            }
        )

    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)
    session_id = _get_or_create_retriever_session_id(
        db, current_user.id, request.session_id
    )

    try:
        from app.services.ai_retriever import retrieve_for_retriever_turn
        from app.services.ai_retrieval_service import RetrievalServiceError

        payload = await retrieve_for_retriever_turn(
            db,
            user_id=current_user.id,
            query=request.message,
            top_k=30,
            model_key=model_key,
            fallback_candidates=fallback,
        )
        retrieval_results = [
            _enrich_reference(ref, source_reason=ref.get("source_reason", "hybrid_match"))
            for ref in (payload.get("retrieval_results") or [])
            if isinstance(ref, dict)
        ]
        used_refs = [
            _enrich_reference(ref, source_reason=ref.get("source_reason", "hybrid_match"))
            for ref in (payload.get("used_references") or [])
            if isinstance(ref, dict)
        ]
        invalid_refs = []
        for inv in payload.get("invalid_references") or []:
            if isinstance(inv, dict) and inv.get("entry_id") is not None:
                invalid_refs.append(
                    InvalidReferenceItem(
                        entry_id=int(inv["entry_id"]),
                        reason=str(inv.get("reason") or "内容与问题不相关"),
                    )
                )
        answer = str(payload.get("answer") or "")
        meta = payload.get("meta") or {}

        save_message(
            db, current_user.id, session_id, "user", request.message, commit=False
        )
        save_message(
            db,
            current_user.id,
            session_id,
            "assistant",
            answer,
            [ref.model_dump() for ref in used_refs],
            commit=False,
        )
        from app.services.ai_session import touch_session_activity

        touch_session_activity(db, current_user.id, session_id, commit=False)
        db.commit()

        return ChatMessageResponse(
            session_id=session_id,
            message=answer,
            valid_references=used_refs,
            invalid_references=invalid_refs,
            mode="retrieval",
            agent_steps=[],
            model_fallback=_model_fallback_info(payload.get("model_fallback")),
            retrieval_results=retrieval_results,
            used_references=used_refs,
            intent=payload.get("intent"),
            found=payload.get("found"),
            candidate_count=meta.get("candidate_count"),
            judged_count=meta.get("judged_count"),
        )

    except RetrievalServiceError as exc:
        db.rollback()
        raise HTTPException(
            status_code=exc.http_status,
            detail={
                "code": exc.code,
                "message": exc.message,
                "must_not_treat_as_zero_hits": True,
            },
        ) from exc
    except LLMGatewayError as exc:
        db.rollback()
        raise _llm_unavailable_http(exc) from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        import logging
        logging.getLogger(__name__).error("发送消息失败: %s", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "AI_CHAT_FAILED", "message": "发送消息失败，请稍后重试"},
        ) from e


@router.post("/query/chat", response_model=ChatMessageResponse)
@router.post("/agent/chat", response_model=ChatMessageResponse)
async def send_query_message(
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    查询模式聊天（原 Agent 模式）。

    后端执行一组只读工具，把数据库目录、语义检索、关键词检索、
    待办快照汇总给 LLM，由 LLM 基于工具证据回答。不沉淀长期经验。
    """
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "query", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    session_id = _get_or_create_session_id(db, current_user.id, request.session_id)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "query")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    try:
        save_message(db, current_user.id, session_id, "user", request.message)
        history = get_session_history(db, current_user.id, session_id)
        observations = await build_agent_observations_v2_async(
            db,
            current_user.id,
            session_id,
            request.message,
            conversation_history=history,
            planner=normalize_agent_planner(request.agent_planner),
            max_rounds=normalize_agent_tool_rounds(request.agent_tool_rounds),
        )
        answer, model_fallback_raw = await generate_query_answer(
            request.message,
            observations,
            history,
            model_key=model_key,
            user_id=int(current_user.id),
            fallback_candidates=fallback,
        )

        valid_refs = _observations_to_valid_refs(observations)

        assistant_message = save_message(
            db,
            current_user.id,
            session_id,
            "assistant",
            answer,
            [ref.model_dump() for ref in valid_refs],
        )
        try:
            from app.services.agent_pending_actions import propose_pending_actions_from_turn

            propose_pending_actions_from_turn(
                db,
                user_id=int(current_user.id),
                session_id=session_id,
                user_message=request.message,
                answer=answer,
                assistant_message_id=int(assistant_message.id),
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("pending action proposal failed: %s", exc)
        refresh_conversation_summary(db, current_user.id, session_id)

        return ChatMessageResponse(
            session_id=session_id,
            message=answer,
            valid_references=valid_refs,
            invalid_references=[],
            mode="query",
            agent_steps=observations.get("tool_steps", []),
            analysis=observations.get("analysis"),
            model_fallback=_model_fallback_info(model_fallback_raw),
        )
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"查询模式发送消息失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="查询模式发送失败，请稍后重试",
        )


@router.post("/hermes/chat", response_model=ChatMessageResponse)
async def send_hermes_message(
    request: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Hermes Agent 模式：GrowthLog 组装只读 observations，私有 Hermes CLI 综合分析。
    Hermes 不直连 MySQL，仅看到后端注入的 JSON 证据。
    """
    _ensure_ai_ready(db, current_user)

    session_id = _get_or_create_session_id(db, current_user.id, request.session_id)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "hermes")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    try:
        save_message(db, current_user.id, session_id, "user", request.message)
        history = get_session_history(db, current_user.id, session_id)
        observations = build_agent_observations(db, current_user.id, request.message)
        prompt = build_hermes_prompt(request.message, observations, history)
        hermes_result = invoke_hermes(prompt)

        if hermes_result.get("error"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "HERMES_UNAVAILABLE",
                    "message": "整合模式暂不可用，可先使用检索或复盘模式。",
                },
            )

        answer = hermes_result["answer"]
        valid_refs = _observations_to_valid_refs(observations)
        agent_steps = list(observations.get("tool_steps", []))
        agent_steps.append(
            {
                "tool": "hermes_agent",
                "title": "Hermes Agent 综合分析",
                "summary": "已调用私有 Hermes Agent",
            }
        )

        save_message(
            db,
            current_user.id,
            session_id,
            "assistant",
            answer,
            [ref.model_dump() for ref in valid_refs],
        )

        return ChatMessageResponse(
            session_id=session_id,
            message=answer,
            valid_references=valid_refs,
            invalid_references=[],
            mode="hermes",
            agent_steps=agent_steps,
        )
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Hermes Agent 模式发送消息失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Agent 模式发送失败，请稍后重试",
        )


@router.get("/chat/history", response_model=ChatHistoryResponse)
def get_chat_history(
    session_id: str = Query(..., description="会话ID"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """获取对话历史"""
    messages = get_session_history(db, current_user.id, session_id)

    return ChatHistoryResponse(
        session_id=session_id,
        messages=messages
    )


@router.post("/chat/sessions", response_model=CreateChatSessionResponse, status_code=status.HTTP_201_CREATED)
def create_chat_session(
    request: CreateChatSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建带 persona 的新会话。"""
    try:
        row = create_persona_session(
            db,
            current_user.id,
            request.persona,
            session_id=request.session_id,
            title=request.title,
        )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SessionConflictError as exc:
        raise _session_error_http(exc) from exc

    return CreateChatSessionResponse(
        session_id=row.session_id,
        persona=row.persona,
        title=row.title,
        status=row.status,
        legacy=False,
        continuable=True,
    )


@router.get("/chat/sessions", response_model=ChatSessionsResponse)
def get_chat_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取会话列表（含 legacy 与 persona 会话）。"""
    raw_sessions = list_user_sessions(db, current_user.id, limit=limit, offset=offset)
    sessions = [
        ChatSessionItem(
            session_id=item["session_id"],
            last_message=item.get("last_message") or "",
            role=item.get("persona"),
            persona=item.get("persona"),
            title=item.get("title"),
            status=item.get("status"),
            legacy=bool(item.get("legacy")),
            continuable=bool(item.get("continuable")),
            source_set_status=item.get("source_set_status"),
            current_source_set_version=item.get("current_source_set_version"),
            created_at=item.get("created_at"),
            updated_at=item.get("updated_at"),
        )
        for item in raw_sessions
    ]

    return ChatSessionsResponse(sessions=sessions, total=len(sessions))


@router.delete("/chat/sessions/{session_id}", response_model=ChatDeleteResponse)
def remove_chat_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """删除会话"""
    success = delete_session(db, current_user.id, session_id)

    if success:
        return ChatDeleteResponse(
            success=True,
            message="会话已删除"
        )
    else:
        return ChatDeleteResponse(
            success=False,
            message="会话不存在或删除失败"
        )


@router.post("/chat/sessions/{session_id}/compact", response_model=ChatSessionCompactResponse)
async def compact_chat_session(
    session_id: str,
    use_llm: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """兼容压缩端点（内部/回归用）。普通 UI 不再暴露；失败返回稳定业务错误。"""
    from app.services.ai_session import get_session_metadata
    from app.services.ai_persona_context import compact_explainer_session
    from app.services.ai_source_set import SourceSetError

    try:
        meta = get_session_metadata(db, current_user.id, session_id)
        if meta is not None and meta.persona == "explainer" and not use_llm:
            summary = compact_explainer_session(
                db,
                current_user.id,
                session_id,
                min_messages=1,
            )
        elif use_llm:
            summary = await compact_conversation_summary_llm(
                db,
                current_user.id,
                session_id,
                min_messages=1,
                max_messages=30,
            )
            if summary and meta is not None and meta.persona == "explainer":
                from app.services.ai_persona_context import sanitize_summary_structured
                from app.models import AIConversationSummary

                cleaned = sanitize_summary_structured(summary.get("structured_json"))
                cleaned["summary"] = cleaned.get("summary") or summary.get("summary") or ""
                row = (
                    db.query(AIConversationSummary)
                    .filter(
                        AIConversationSummary.user_id == current_user.id,
                        AIConversationSummary.session_id == session_id,
                    )
                    .first()
                )
                if row is not None:
                    row.summary = cleaned["summary"]
                    row.structured_json = cleaned
                    db.commit()
                summary = {
                    **summary,
                    "summary": cleaned["summary"],
                    "structured_json": cleaned,
                }
        else:
            summary = compact_conversation_summary(
                db,
                current_user.id,
                session_id,
                min_messages=1,
                max_messages=30,
            )
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc
    except TypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "COMPACT_FAILED", "message": "会话压缩暂时不可用，请稍后重试。"},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "COMPACT_FAILED", "message": "会话压缩暂时不可用，请稍后重试。"},
        ) from exc
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "COMPACT_EMPTY", "message": "会话不存在或没有可压缩消息"},
        )
    return ChatSessionCompactResponse(
        session_id=session_id,
        summary=summary.get("summary", ""),
        structured_json=summary.get("structured_json", {}),
        message_count=int(summary.get("message_count", 0)),
        last_message_at=summary.get("last_message_at"),
    )


@router.get("/chat/sessions/{session_id}/source-sets", response_model=SourceSetVersionsResponse)
def get_session_source_sets(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.ai_session import get_session_metadata

    meta = get_session_metadata(db, current_user.id, session_id)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "SESSION_NOT_FOUND", "message": "会话不存在"})
    versions = list_source_set_versions(db, current_user.id, session_id)
    return SourceSetVersionsResponse(
        session_id=session_id,
        current_version=meta.current_source_set_version,
        versions=[SourceSetVersionItem(**v) for v in versions],
    )


@router.get(
    "/chat/sessions/{session_id}/source-sets/current",
    response_model=CurrentLockedSourcesResponse,
)
def get_current_locked_sources(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """讲解员当前固定来源清单（只读展示 + opaque 预览 token）。"""
    from app.services.ai_stream_reference_preview import (
        ReferencePreviewError,
        build_current_locked_sources_view,
    )

    try:
        payload = build_current_locked_sources_view(
            db, user_id=int(current_user.id), session_id=session_id
        )
    except ReferencePreviewError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if exc.code in {"SESSION_NOT_FOUND", "SOURCE_REFERENCE_GONE"}
            else status.HTTP_400_BAD_REQUEST
        )
        if exc.code == "SESSION_PERSONA_MISMATCH":
            status_code = status.HTTP_409_CONFLICT
        if exc.code == "NOTION_SOURCE_SYNC_PENDING":
            status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return CurrentLockedSourcesResponse(**payload)


@router.post("/chat/sessions/{session_id}/source-sets/expand")
async def expand_session_source_set(
    session_id: str,
    request: SourceSetExpandRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """显式补充来源：生成 expansion proposal，不改变当前锁定版本直至用户确认。"""
    import secrets

    from app.services.ai_explainer import propose_source_expansion
    from app.services.ai_session import get_session_metadata
    from app.services.ai_session_constants import PERSONA_EXPLAINER
    from app.services.ai_source_set import SourceSetError
    from app.services.ai_stream_reference_preview import (
        build_current_locked_sources_view,
        build_pending_proposal_view,
    )

    meta = get_session_metadata(db, current_user.id, session_id)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "SESSION_NOT_FOUND", "message": "会话不存在"},
        )
    if str(meta.persona or "") != PERSONA_EXPLAINER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SESSION_PERSONA_MISMATCH", "message": "仅讲解员会话可补充来源"},
        )
    if meta.current_source_set_version is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SOURCE_SET_NOT_LOCKED", "message": "请先确认初始来源后再补充"},
        )
    current_view = build_current_locked_sources_view(
        db, user_id=int(current_user.id), session_id=session_id
    )
    if current_view.get("stale"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SOURCE_SET_STALE", "message": "来源已过期，请新建对话"},
        )
    if not current_view.get("can_expand"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "SOURCE_SET_NOT_EXPANDABLE", "message": "当前无法补充来源"},
        )

    from app.services.ai_expansion_intent import validate_expansion_topic, ExpansionIntentError

    try:
        query = validate_expansion_topic(request.query)
    except ExpansionIntentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    intent = {
        "action": "propose_source_expansion",
        "expansion_query": query,
    }
    request_id = secrets.token_hex(16)
    try:
        await propose_source_expansion(
            db,
            user_id=int(current_user.id),
            session_id=session_id,
            text=query,
            intent=intent,
            current_version=int(meta.current_source_set_version),
            request_id=request_id,
            use_query_planner=True,
        )
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    pending = build_pending_proposal_view(
        db, user_id=int(current_user.id), session_id=session_id
    )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "EXPANSION_PROPOSAL_MISSING", "message": "补充来源候选生成失败"},
        )
    return {"pending": True, **pending}


@router.post("/chat/sessions/{session_id}/source-sets/propose", response_model=SourceSetProposalResponse)
def propose_session_source_set(
    session_id: str,
    request: SourceSetProposalRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    members = [
        SourceMemberIdentity(
            reference_key=m.reference_key,
            display_order=m.display_order,
        )
        for m in request.members
    ]
    try:
        if request.base_version is None or int(request.base_version) == 0:
            row = create_initial_proposal(db, current_user.id, session_id, members)
        else:
            row = create_expansion_proposal(
                db,
                current_user.id,
                session_id,
                members,
                base_version=int(request.base_version),
            )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc

    return SourceSetProposalResponse(
        proposal_id=int(row.id),
        session_id=session_id,
        base_version=row.base_version,
        status=row.status,
    )


@router.post("/chat/sessions/{session_id}/source-sets/confirm", response_model=SourceSetConfirmResponse)
def confirm_session_source_set(
    session_id: str,
    request: SourceSetConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    selected_keys = request.selected_reference_keys
    if request.selected_ref_tokens:
        from app.services.ai_stream_protocol import (
            PURPOSE_CANDIDATE_CONFIRM,
            verify_reference_token,
        )

        resolved: List[str] = []
        for token in request.selected_ref_tokens:
            payload = verify_reference_token(
                str(token),
                user_id=int(current_user.id),
                session_id=session_id,
                purpose=PURPOSE_CANDIDATE_CONFIRM,
            )
            if payload is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SOURCE_REFERENCE_INVALID", "message": "所选来源令牌无效。"},
                )
            from app.services.reference_identity import parse_proposal_reference_key

            reference_key = str(payload.get("reference_key") or "").strip()
            parsed = parse_proposal_reference_key(reference_key) if reference_key else None
            if parsed is not None:
                resolved.append(reference_key)
                continue
            attachment_id = payload.get("attachment_id")
            entry_id = payload.get("entry_id")
            if attachment_id is not None:
                resolved.append(f"attachment:source:{int(attachment_id)}")
            elif entry_id is not None:
                resolved.append(f"entry:source:{int(entry_id)}")
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SOURCE_REFERENCE_INVALID", "message": "所选来源令牌无效。"},
                )
        selected_keys = resolved

    try:
        row = confirm_proposal(
            db,
            current_user.id,
            session_id,
            request.proposal_id,
            base_version=int(request.base_version),
            selected_reference_keys=selected_keys,
        )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc

    from app.services.ai_source_set import parse_manifest

    manifest = parse_manifest(row.source_manifest)
    return SourceSetConfirmResponse(
        session_id=session_id,
        version=int(row.version or 0),
        content_fingerprint=str(row.content_fingerprint or ""),
        status=row.status,
        member_count=len(manifest.members),
    )


@router.delete(
    "/chat/sessions/{session_id}/source-sets/proposals/{proposal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def cancel_session_source_set_proposal(
    session_id: str,
    proposal_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancel a proposed source set (idempotent 204).
    Only status=proposed may be cancelled; confirmed → 409 SOURCE_SET_ALREADY_CONFIRMED.
    """
    from app.services.ai_source_set import cancel_proposal

    try:
        cancel_proposal(db, current_user.id, session_id, int(proposal_id))
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chat/sessions/{session_id}/source-sets/check-stale")
def check_session_source_set_stale(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.ai_session import get_session_metadata

    meta = get_session_metadata(db, current_user.id, session_id)
    if meta is None or meta.current_source_set_version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "SESSION_NOT_FOUND", "message": "会话或来源版本不存在"})
    stale = check_and_mark_stale_if_changed(
        db,
        current_user.id,
        session_id,
        int(meta.current_source_set_version),
    )
    return {"session_id": session_id, "stale": stale}


# ========== Explainer (R11.2) ==========

@router.post(
    "/explainer/source-candidates",
    response_model=ExplainerCandidatesResponse,
    response_model_exclude_none=True,
)
async def explainer_source_candidates(
    request: ExplainerCandidatesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unified first-turn orchestration: one RAG + atomic proposal/user message."""
    _ensure_ai_ready(db, current_user)
    from app.services.ai_explainer import orchestrate_initial_source_candidates

    try:
        validate_session_for_mode(db, current_user.id, request.session_id, "explainer")
        payload = await orchestrate_initial_source_candidates(
            db,
            user_id=current_user.id,
            session_id=request.session_id,
            query=request.query,
            use_query_planner=True,
        )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc

    return ExplainerCandidatesResponse(
        session_id=payload["session_id"],
        proposal_id=int(payload["proposal_id"]),
        base_version=int(payload.get("base_version") or 0),
        status=str(payload.get("status") or "proposed"),
        query=str(payload.get("query") or ""),
        candidates=_public_explainer_candidates(
            list(payload.get("candidates") or []),
            user_id=int(current_user.id),
            session_id=request.session_id,
        ),
        message=str(payload.get("message") or ""),
    )


@router.post(
    "/explainer/chat",
    response_model=ExplainerChatResponse,
    response_model_exclude_none=True,
)
async def explainer_chat_route(
    request: ExplainerChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Explainer chat:
    - no locked sources → candidates + awaiting confirm (no formal answer)
    - locked sources → fixed-context answer; optional expansion proposal
    """
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)
    from app.services.ai_explainer import explainer_chat
    from app.services.llm_gateway import LLMGatewayError as _LLMErr

    try:
        validate_session_for_mode(db, current_user.id, request.session_id, "explainer")
        payload = await explainer_chat(
            db,
            user_id=current_user.id,
            session_id=request.session_id,
            message=request.message,
            model_key=model_key,
            fallback_candidates=fallback,
        )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc
    except _LLMErr as exc:
        raise _llm_unavailable_http(exc) from exc

    valid_refs = []
    for ref in payload.get("valid_references") or []:
        if not isinstance(ref, dict):
            continue
        if (
            ref.get("entry_id") is None
            and ref.get("attachment_id") is None
            and str(ref.get("source_type") or "") not in {"todo", "notion_page"}
        ):
            continue
        # Attachment-only fixed sources keep parent entry_id when present.
        if ref.get("entry_id") is None and ref.get("attachment_id") is not None:
            parent = ref.get("parent_id") or (ref.get("metadata") or {}).get("entry_id")
            if parent is not None:
                ref = dict(ref)
                ref["entry_id"] = int(parent)
            else:
                ref = dict(ref)
                ref["entry_id"] = int(ref["attachment_id"])
        valid_refs.append(
            _enrich_reference(ref, source_reason="explainer_fixed")
        )
    invalid_refs = []
    for inv in payload.get("invalid_references") or []:
        if isinstance(inv, dict) and inv.get("entry_id") is not None:
            invalid_refs.append(
                InvalidReferenceItem(
                    entry_id=int(inv["entry_id"]),
                    reason=str(inv.get("reason") or "不相关"),
                )
            )

    return ExplainerChatResponse(
        session_id=payload["session_id"],
        mode="explainer",
        phase=str(payload.get("phase") or "answered"),
        message=str(payload.get("message") or ""),
        answer=payload.get("answer"),
        proposal_id=payload.get("proposal_id"),
        base_version=payload.get("base_version"),
        candidates=_public_explainer_candidates(
            list(payload.get("candidates") or []),
            user_id=int(current_user.id),
            session_id=request.session_id,
        ),
        valid_references=valid_refs,
        invalid_references=invalid_refs,
        source_set_version=payload.get("source_set_version"),
        expansion=_public_explainer_expansion(
            payload.get("expansion"),
            user_id=int(current_user.id),
            session_id=request.session_id,
        ),
        model_fallback=_model_fallback_info(payload.get("model_fallback")),
        context_meta=payload.get("context_meta"),
        resumed=payload.get("resumed"),
        idempotent=payload.get("idempotent"),
    )


@router.post("/explainer/resume", response_model=ExplainerChatResponse)
async def explainer_resume_route(
    request: ExplainerResumeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Resume answering the user question bound to a confirmed proposal_id."""
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)
    from app.services.ai_explainer import resume_proposal
    from app.services.llm_gateway import LLMGatewayError as _LLMErr

    try:
        validate_session_for_mode(db, current_user.id, request.session_id, "explainer")
        payload = await resume_proposal(
            db,
            user_id=current_user.id,
            session_id=request.session_id,
            proposal_id=int(request.proposal_id),
            model_key=model_key,
            fallback_candidates=fallback,
        )
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    except SourceSetError as exc:
        raise _source_set_error_http(exc) from exc
    except _LLMErr as exc:
        raise _llm_unavailable_http(exc) from exc

    valid_refs = []
    for ref in payload.get("valid_references") or []:
        if not isinstance(ref, dict):
            continue
        if (
            ref.get("entry_id") is None
            and ref.get("attachment_id") is None
            and str(ref.get("source_type") or "") not in {"todo", "notion_page"}
        ):
            continue
        if ref.get("entry_id") is None and ref.get("attachment_id") is not None:
            parent = ref.get("parent_id") or (ref.get("metadata") or {}).get("entry_id")
            ref = dict(ref)
            ref["entry_id"] = int(parent) if parent is not None else int(ref["attachment_id"])
        valid_refs.append(
            _enrich_reference(ref, source_reason="explainer_fixed")
        )
    return ExplainerChatResponse(
        session_id=payload["session_id"],
        mode="explainer",
        phase=str(payload.get("phase") or "answered"),
        message=str(payload.get("message") or ""),
        answer=payload.get("answer"),
        proposal_id=payload.get("proposal_id"),
        valid_references=valid_refs,
        invalid_references=[],
        source_set_version=payload.get("source_set_version"),
        model_fallback=_model_fallback_info(payload.get("model_fallback")),
        context_meta=payload.get("context_meta"),
        resumed=payload.get("resumed"),
        idempotent=payload.get("idempotent"),
    )


# ========== Streaming (R11.3, in progress — see docs/00 §4.16) ==========

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def _sse_streaming_response(generator):
    from app.services.sse_keepalive import iter_sse_with_keepalive

    return StreamingResponse(
        iter_sse_with_keepalive(
            generator,
            interval_seconds=settings.sse_keepalive_seconds,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/chat/sessions/{session_id}/retrieve/stream")
async def retrieve_stream_route(
    session_id: str,
    request: StreamTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """检索员真实 provider streaming（SSE）。会话必须已存在且 persona=retriever。"""
    _ensure_ai_ready(db, current_user)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "retriever")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    from app.services.ai_stream_retrieve import stream_retriever_turn

    generator = stream_retriever_turn(
        db,
        user_id=current_user.id,
        session_id=session_id,
        message=request.message,
        request_id=request.request_id,
        model_key=model_key,
        fallback_candidates=fallback,
    )

    return _sse_streaming_response(generator)



@router.post("/chat/sessions/{session_id}/explain/stream")
async def explain_stream_route(
    session_id: str,
    request: StreamTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """讲解员真实 provider streaming（SSE）。未锁定来源仅推送 source_candidate。"""
    _ensure_ai_ready(db, current_user)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "explainer")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    from app.services.ai_stream_explain import stream_explainer_turn

    generator = stream_explainer_turn(
        db,
        user_id=current_user.id,
        session_id=session_id,
        message=request.message,
        request_id=request.request_id,
        model_key=model_key,
        fallback_candidates=fallback,
    )

    return _sse_streaming_response(generator)



@router.post("/chat/sessions/{session_id}/explain/resume/stream")
async def explain_resume_stream_route(
    session_id: str,
    request: StreamResumeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """讲解员来源确认后的流式 resume（SSE）。只写 assistant，不重复写 user。"""
    _ensure_ai_ready(db, current_user)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "explainer")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    model_key = _resolve_request_model(db, current_user, "retrieval", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    from app.services.ai_stream_explain import stream_explainer_resume

    generator = stream_explainer_resume(
        db,
        user_id=current_user.id,
        session_id=session_id,
        proposal_id=int(request.proposal_id),
        request_id=request.request_id,
        model_key=model_key,
        fallback_candidates=fallback,
    )

    return _sse_streaming_response(generator)



@router.post("/chat/sessions/{session_id}/references/preview")
def reference_preview_route(
    session_id: str,
    request: ReferencePreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """完整引用预览：ref_token 仅在 body；复用现有记录/附件权限。"""
    from app.services.ai_stream_protocol import PURPOSE_ANSWER_REFERENCE
    from app.services.ai_stream_reference_preview import (
        ReferencePreviewError,
        build_reference_preview,
    )

    try:
        return build_reference_preview(
            db,
            user_id=int(current_user.id),
            session_id=session_id,
            ref_token=request.ref_token,
            purpose=PURPOSE_ANSWER_REFERENCE,
        )
    except ReferencePreviewError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "SOURCE_REFERENCE_GONE"
            else status.HTTP_400_BAD_REQUEST
        )
        if exc.code == "NOTION_SOURCE_SYNC_PENDING":
            status_code = status.HTTP_409_CONFLICT
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


@router.get("/chat/sessions/{session_id}/pending-proposal")
def pending_proposal_route(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """断线恢复：返回当前待确认来源候选（若有）。"""
    from app.services.ai_stream_reference_preview import build_pending_proposal_view

    payload = build_pending_proposal_view(
        db, user_id=int(current_user.id), session_id=session_id
    )
    if payload is None:
        return {"pending": False}
    return {"pending": True, **payload}


# ========== Agent Runtime Routes ==========

@router.get("/agent/tools", response_model=List[AgentToolItem])
def list_agent_tools(
    current_user: User = Depends(get_current_user),
):
    """列出当前 Agent Tool Registry 中开放的工具"""
    from app.services.agent_query_engine import TOOL_REGISTRY

    return [
        AgentToolItem(name=tool.name, title=tool.title, permission=tool.permission)
        for tool in TOOL_REGISTRY
    ]


@router.get("/agent/tool-calls", response_model=List[AgentToolAuditItem])
def list_agent_tool_calls(
    session_id: Optional[str] = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取当前用户的 Agent 工具调用审计"""
    from app.models import AgentToolAudit

    query = db.query(AgentToolAudit).filter(AgentToolAudit.user_id == current_user.id)
    if session_id:
        query = query.filter(AgentToolAudit.session_id == session_id)
    rows = query.order_by(AgentToolAudit.created_at.desc()).limit(limit).all()
    return [
        AgentToolAuditItem(
            id=int(row.id),
            session_id=row.session_id,
            tool_name=row.tool_name,
            permission=row.permission,
            status=row.status,
            input_json=row.input_json,
            output_summary=row.output_summary,
            error_message=row.error_message,
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in rows
    ]


def _memory_to_item(memory, quality_warnings: Optional[List[Dict]] = None) -> AgentMemoryItem:
    return AgentMemoryItem(
        id=int(memory.id),
        memory_type=memory.memory_type,
        content=memory.content,
        source=memory.source,
        confidence=float(memory.confidence or 0.0),
        is_active=bool(memory.is_active),
        created_at=memory.created_at.isoformat() if memory.created_at else None,
        updated_at=memory.updated_at.isoformat() if memory.updated_at else None,
        quality_warnings=quality_warnings or [],
    )


@router.get("/memories", response_model=List[AgentMemoryItem])
def list_agent_memories(
    include_inactive: bool = Query(default=False, description="是否包含已停用记忆"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户的 Agent 长期记忆"""
    from app.models import AgentMemory

    query = db.query(AgentMemory).filter(AgentMemory.user_id == current_user.id)
    if not include_inactive:
        query = query.filter(AgentMemory.is_active.is_(True))
    rows = query.order_by(AgentMemory.updated_at.desc(), AgentMemory.created_at.desc()).all()
    return [_memory_to_item(row) for row in rows]


@router.post("/memories", response_model=AgentMemoryItem)
def create_agent_memory(
    request: AgentMemoryCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建当前用户的 Agent 长期记忆"""
    from app.models import AgentMemory
    from app.services.agent_memory_quality import analyze_memory_candidate

    quality = analyze_memory_candidate(
        db,
        user_id=int(current_user.id),
        memory_type=request.memory_type,
        content=request.content,
    )
    memory = AgentMemory(
        user_id=current_user.id,
        memory_type=request.memory_type,
        content=quality.normalized_content,
        source=request.source.strip() or "user",
        confidence=request.confidence,
        is_active=True,
    )
    db.add(memory)
    db.commit()
    db.refresh(memory)
    from app.services.agent_memory_embeddings import after_agent_memory_mutation

    after_agent_memory_mutation(db, user_id=int(current_user.id), memory=memory)
    return _memory_to_item(memory, quality.warnings)


@router.post("/memories/analyze", response_model=AgentMemoryAnalyzeResponse)
def analyze_agent_memory(
    request: AgentMemoryAnalyzeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """只读分析候选长期记忆，提示近重复或潜在冲突"""
    from app.services.agent_memory_quality import analyze_memory_candidate

    quality = analyze_memory_candidate(
        db,
        user_id=int(current_user.id),
        memory_type=request.memory_type,
        content=request.content,
        exclude_memory_id=request.exclude_memory_id,
    )
    return AgentMemoryAnalyzeResponse(
        normalized_content=quality.normalized_content,
        quality_warnings=quality.warnings,
    )


@router.patch("/memories/{memory_id}", response_model=AgentMemoryItem)
def update_agent_memory(
    memory_id: int,
    request: AgentMemoryUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户的 Agent 长期记忆"""
    from app.models import AgentMemory
    from app.services.agent_memory_quality import analyze_memory_candidate, normalize_memory_content

    memory = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.user_id == current_user.id,
    ).first()
    if not memory:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="个性设置不存在")

    next_memory_type = request.memory_type if request.memory_type is not None else memory.memory_type
    quality_warnings: List[Dict] = []
    if request.content is not None or request.memory_type is not None:
        quality = analyze_memory_candidate(
            db,
            user_id=int(current_user.id),
            memory_type=next_memory_type,
            content=request.content if request.content is not None else memory.content,
            exclude_memory_id=int(memory_id),
        )
        quality_warnings = quality.warnings

    if request.memory_type is not None:
        memory.memory_type = request.memory_type
    if request.content is not None:
        memory.content = normalize_memory_content(request.content)
    if request.confidence is not None:
        memory.confidence = request.confidence
    if request.is_active is not None:
        memory.is_active = request.is_active

    db.commit()
    db.refresh(memory)
    from app.services.agent_memory_embeddings import after_agent_memory_mutation

    after_agent_memory_mutation(db, user_id=int(current_user.id), memory=memory)
    return _memory_to_item(memory, quality_warnings)


@router.post("/memories/{memory_id}/merge", response_model=AgentMemoryMergeResponse)
def merge_agent_memories(
    memory_id: int,
    request: AgentMemoryMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """合并当前用户已确认的同类型个性设置，来源项仅软停用"""
    from app.models import AgentMemory
    from app.services.agent_memory_embeddings import after_agent_memory_mutation
    from app.services.agent_memory_merge import merge_memory_rows
    from app.services.agent_memory_quality import analyze_memory_candidate

    source_ids = sorted({int(item) for item in request.source_memory_ids if int(item) != int(memory_id)})
    if not source_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="至少需要一条来源个性设置")

    rows = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.user_id == current_user.id,
            AgentMemory.id.in_([int(memory_id), *source_ids]),
        )
        .all()
    )
    by_id = {int(row.id): row for row in rows}
    target = by_id.get(int(memory_id))
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标个性设置不存在")

    missing_source_ids = [item for item in source_ids if item not in by_id]
    if missing_source_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="来源个性设置不存在")

    sources = [by_id[item] for item in source_ids]
    if any(not bool(getattr(item, "is_active", False)) for item in [target, *sources]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只能合并启用中的个性设置")
    if any(item.memory_type != target.memory_type for item in sources):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="只能合并同类型个性设置")

    deactivated_ids = merge_memory_rows(
        target=target,
        sources=sources,
        replacement_content=request.content,
        confidence=request.confidence,
    )
    db.commit()
    db.refresh(target)
    for source_id in deactivated_ids:
        src = by_id.get(int(source_id))
        if src is not None:
            db.refresh(src)
    after_agent_memory_mutation(
        db,
        user_id=int(current_user.id),
        memories=[target, *[by_id[i] for i in deactivated_ids if i in by_id]],
    )

    quality = analyze_memory_candidate(
        db,
        user_id=int(current_user.id),
        memory_type=target.memory_type,
        content=target.content,
        exclude_memory_id=int(target.id),
    )
    return AgentMemoryMergeResponse(
        memory=_memory_to_item(target, quality.warnings),
        deactivated_memory_ids=deactivated_ids,
    )


@router.delete("/memories/{memory_id}", response_model=ChatDeleteResponse)
def delete_agent_memory(
    memory_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """停用当前用户的一条个性设置"""
    from app.models import AgentMemory
    from app.services.agent_memory_embeddings import after_agent_memory_mutation

    memory = db.query(AgentMemory).filter(
        AgentMemory.id == memory_id,
        AgentMemory.user_id == current_user.id,
    ).first()
    if not memory:
        return ChatDeleteResponse(success=False, message="个性设置不存在")

    memory.is_active = False
    db.commit()
    after_agent_memory_mutation(db, user_id=int(current_user.id), memory=memory)
    return ChatDeleteResponse(success=True, message="个性设置已停用")


# ========== Agent Pending Action Routes ==========

def _pending_action_to_item(action) -> AgentPendingActionItem:
    return AgentPendingActionItem(
        id=int(action.id),
        session_id=action.session_id,
        action_type=action.action_type,
        payload_json=action.payload_json or {},
        status=action.status,
        result_json=action.result_json,
        error_message=action.error_message,
        created_by_message_id=int(action.created_by_message_id) if action.created_by_message_id else None,
        confirmed_at=action.confirmed_at.isoformat() if action.confirmed_at else None,
        executed_at=action.executed_at.isoformat() if action.executed_at else None,
        created_at=action.created_at.isoformat() if action.created_at else None,
        updated_at=action.updated_at.isoformat() if action.updated_at else None,
    )


@router.get("/agent/pending-actions", response_model=List[AgentPendingActionItem])
def list_agent_pending_actions(
    session_id: Optional[str] = Query(default=None, max_length=64),
    status_filter: Optional[str] = Query(default=None, alias="status", pattern="^(pending|confirmed|rejected|executed|failed)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户的 Agent 待确认动作"""
    from app.models import AgentPendingAction

    query = db.query(AgentPendingAction).filter(AgentPendingAction.user_id == current_user.id)
    if session_id:
        query = query.filter(AgentPendingAction.session_id == session_id)
    if status_filter:
        query = query.filter(AgentPendingAction.status == status_filter)
    rows = query.order_by(AgentPendingAction.created_at.desc()).limit(50).all()
    return [_pending_action_to_item(row) for row in rows]


@router.post("/agent/pending-actions", response_model=AgentPendingActionItem, status_code=status.HTTP_201_CREATED)
def create_agent_pending_action(
    request: AgentPendingActionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建一个 Agent 待确认动作；不会直接写业务表"""
    from app.services.agent_pending_actions import create_pending_action

    action = create_pending_action(
        db,
        user_id=int(current_user.id),
        session_id=request.session_id,
        action_type=request.action_type,
        payload_json=request.payload_json,
        created_by_message_id=request.created_by_message_id,
    )
    return _pending_action_to_item(action)


@router.post("/agent/pending-actions/{action_id}/confirm", response_model=AgentPendingActionItem)
def confirm_agent_pending_action(
    action_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认并执行一个白名单 Agent 待确认动作"""
    from app.models import AgentPendingAction
    from app.services.agent_pending_actions import PendingActionError, confirm_pending_action

    action = db.query(AgentPendingAction).filter(
        AgentPendingAction.id == action_id,
        AgentPendingAction.user_id == current_user.id,
    ).first()
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待确认动作不存在")
    try:
        action = confirm_pending_action(db, action)
    except PendingActionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _pending_action_to_item(action)


@router.post("/agent/pending-actions/{action_id}/reject", response_model=AgentPendingActionItem)
def reject_agent_pending_action(
    action_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """拒绝一个 Agent 待确认动作；不会产生业务副作用"""
    from app.models import AgentPendingAction
    from app.services.agent_pending_actions import PendingActionError, reject_pending_action

    action = db.query(AgentPendingAction).filter(
        AgentPendingAction.id == action_id,
        AgentPendingAction.user_id == current_user.id,
    ).first()
    if not action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="待确认动作不存在")
    try:
        action = reject_pending_action(db, action)
    except PendingActionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _pending_action_to_item(action)


@router.post("/review/preview", response_model=ReviewPreviewResponse)
async def review_preview(
    request: ReviewPreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """复盘模式预览：只读拉取 + LLM 生成，不写库。"""
    _ensure_ai_ready(db, current_user)

    entries, source_entry_ids = _load_review_scope_entries(db, current_user.id, request)
    if not source_entry_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "REVIEW_SCOPE_EMPTY", "message": "复盘范围为空，请调整范围后重试"},
        )

    goal = request.message or request.goal or "近期成长复盘"
    scope_type = request.scope_type or "recent_7d"
    preview = await generate_review_preview(
        db,
        goal=goal,
        entries=entries,
        source_entry_ids=source_entry_ids,
    )

    return _build_review_preview_response(
        preview,
        source_entry_ids,
        scope_type,
        entry_ids=request.entry_ids,
        tag=request.tag,
        start_date=request.start_date,
        end_date=request.end_date,
    )


@router.post("/review/chat", response_model=ReviewChatResponse)
async def review_chat(
    request: ReviewChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """复盘模式聊天：写入会话历史 + 生成 preview（不写 Entry）。"""
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "review", request.model_key)
    fallback = _fallback_candidates(db, current_user, model_key)

    session_id = _get_or_create_session_id(db, current_user.id, request.session_id)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "review")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc
    save_message(db, current_user.id, session_id, "user", request.message)

    entries, source_entry_ids = _load_review_scope_entries(db, current_user.id, request)
    if not source_entry_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "REVIEW_SCOPE_EMPTY", "message": "复盘范围为空，请调整范围后重试"},
        )

    goal = request.message or request.goal or "近期成长复盘"
    scope_type = request.scope_type or "recent_7d"
    preview_raw = await generate_review_preview(
        db,
        goal=goal,
        entries=entries,
        source_entry_ids=source_entry_ids,
        model_key=model_key,
        user_id=int(current_user.id),
        fallback_candidates=fallback,
    )
    preview_resp = _build_review_preview_response(
        preview_raw,
        source_entry_ids,
        scope_type,
        entry_ids=request.entry_ids,
        tag=request.tag,
        start_date=request.start_date,
        end_date=request.end_date,
    )
    valid_refs = _entries_to_valid_refs(db, entries)

    save_message(
        db,
        current_user.id,
        session_id,
        "assistant",
        preview_resp.summary,
        {
            "kind": "review",
            "mode": "query",
            "review_preview": preview_resp.model_dump(),
            "valid_references": [ref.model_dump() for ref in valid_refs],
        },
    )

    return ReviewChatResponse(
        session_id=session_id,
        message=preview_resp.summary,
        mode="query",
        review_preview=preview_resp,
        valid_references=valid_refs,
        model_fallback=_model_fallback_info(preview_raw.get("model_fallback")),
    )


@router.post("/review/confirm", response_model=ReviewConfirmResponse)
def review_confirm(
    request: ReviewConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认写入 AI 复盘派生内容（review / action_plan / tag_suggestion），不改原始 Entry。"""
    confirm_type = (request.type or "review").strip()
    if confirm_type not in REVIEW_CONFIRM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_TYPE_INVALID", "message": "不支持的复盘保存类型"},
        )

    source_entry_ids = _resolve_review_confirm_source_ids(db, current_user.id, request)
    if not source_entry_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "REVIEW_SCOPE_EMPTY", "message": "复盘范围为空，无法保存"},
        )

    metadata = build_derived_metadata(
        request.metadata,
        mode="review",
        scope_type=request.scope_type or "recent_7d",
    )
    metadata["source_entry_ids"] = source_entry_ids

    if confirm_type == "review":
        if not request.content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "EMPTY_CONTENT", "message": "复盘内容不能为空"},
            )
        derived_type = "review"
        title = request.title.strip()
        content = request.content.strip()
    elif confirm_type == "action_plan":
        suggestions = metadata.get("suggestions") or []
        if not suggestions:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "ACTION_PLAN_EMPTY", "message": "没有可保存的行动项"},
            )
        derived_type = "action_plan"
        title = (request.title or "行动计划").strip()[:255]
        content = compose_action_plan_content([str(s) for s in suggestions])
    else:
        keywords = metadata.get("keywords") or metadata.get("suggested_tags") or []
        if not keywords:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "TAG_SUGGESTION_EMPTY", "message": "没有可保存的标签建议"},
            )
        derived_type = "tag_suggestion"
        title = (request.title or "标签建议").strip()[:255]
        content = compose_tag_suggestion_content([str(k) for k in keywords])

    row = create_derived_content(
        db,
        current_user.id,
        type=derived_type,
        title=title,
        content=content,
        scope_type=request.scope_type or "recent_7d",
        source_entry_ids=source_entry_ids,
        metadata=metadata,
        status="confirmed",
    )

    created_at = row.created_at.isoformat() if row.created_at else now_local().isoformat()
    return ReviewConfirmResponse(
        id=int(row.id),
        type=derived_type,
        source_entry_ids=source_entry_ids,
        created_at=created_at,
    )


@router.get("/derived-contents", response_model=DerivedContentsResponse)
def get_derived_contents(
    type: Optional[str] = Query(
        default=None,
        description="派生类型，支持 review 或逗号分隔多类型",
    ),
    auto: Optional[bool] = Query(default=None, description="true=仅自动结构化，false=仅手动"),
    status: Optional[str] = Query(
        default=None,
        description="draft|confirmed|dismissed；默认仅返回 confirmed",
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取当前用户的 AI 派生内容列表，支持 type / auto / status 筛选。"""
    rows = list_derived_contents(
        db,
        current_user.id,
        type=type,
        auto=auto,
        status=status,
        limit=50,
    )
    items = []
    for row in rows:
        content = row.content or ""
        summary_text = content if len(content) <= 240 else content[:240] + "…"
        items.append(
            DerivedContentItem(
                id=int(row.id),
                type=row.type,
                title=row.title,
                content=summary_text,
                source_entry_ids=row.source_entry_ids or [],
                metadata=row.meta or {},
                status=row.status or "confirmed",
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
        )
    return DerivedContentsResponse(items=items)


@router.get("/derived-contents/{content_id}", response_model=DerivedContentDetailResponse)
def get_derived_content_detail(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取当前用户单条派生内容完整详情（不可跨用户）。"""
    from app.services.derived_content import get_derived_content

    row = get_derived_content(db, current_user.id, content_id)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "DERIVED_NOT_FOUND", "message": "派生内容不存在或无权访问"},
        )
    return DerivedContentDetailResponse(
        id=int(row.id),
        type=row.type,
        title=row.title,
        content=row.content or "",
        source_entry_ids=row.source_entry_ids or [],
        metadata=row.meta or {},
        scope_type=row.scope_type,
        status=row.status or "confirmed",
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.post("/derived-contents/{content_id}/confirm", response_model=DerivedContentStatusResponse)
def confirm_derived_content(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """将 draft 派生内容确认为正式内容，不改 Entry。"""
    from app.services.derived_content import confirm_derived_content_draft

    row = confirm_derived_content_draft(db, current_user.id, content_id)
    return DerivedContentStatusResponse(
        id=int(row.id),
        type=row.type,
        status=row.status,
        source_entry_ids=row.source_entry_ids or [],
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.post("/derived-contents/{content_id}/dismiss", response_model=DerivedContentStatusResponse)
def dismiss_derived_content(
    content_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """忽略 draft 派生内容，不改 Entry。"""
    from app.services.derived_content import dismiss_derived_content_draft

    row = dismiss_derived_content_draft(db, current_user.id, content_id)
    return DerivedContentStatusResponse(
        id=int(row.id),
        type=row.type,
        status=row.status,
        source_entry_ids=row.source_entry_ids or [],
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


async def _run_organize_preview(
    db: Session,
    user_id: int,
    request,
    model_key: str,
    *,
    session_id: Optional[str] = None,
) -> tuple[OrganizePreviewResponse, Optional[ModelFallbackInfo]]:
    from app.services.organize_service import OrganizeError

    user = db.query(User).filter(User.id == user_id).first()
    fallback = _fallback_candidates(db, user, model_key) if user else []
    goal = getattr(request, "goal", None) or getattr(request, "message", None) or "整理记录"
    try:
        result = await generate_organize_preview(
            db,
            goal=goal,
            user_id=user_id,
            days=getattr(request, "days", 30),
            label_codes=getattr(request, "label_codes", None),
            entry_ids=getattr(request, "entry_ids", None),
            allowed_actions=getattr(request, "allowed_actions", None),
            model_key=model_key,
            fallback_candidates=fallback,
            session_id=session_id or getattr(request, "session_id", None),
        )
    except OrganizeError as exc:
        raise HTTPException(
            status_code=int(exc.http_status),
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    message = result.get("message") or "已生成整合改动预览。"
    diff_items = []
    for item in result.get("diff_preview") or []:
        if not isinstance(item, dict):
            continue
        sources_display = []
        for src in item.get("sources_display") or []:
            if isinstance(src, dict):
                sources_display.append(OrganizeSourceDisplay(**{
                    k: src.get(k)
                    for k in ("title", "created_at", "kind", "label_code", "ref_token")
                    if k in src or k in {"title", "created_at", "kind", "label_code"}
                }))
        payload = {
            k: v
            for k, v in item.items()
            if k
            in {
                "action",
                "title",
                "content",
                "reason",
                "risk",
                "confidence",
                "source_entry_ids",
                "item_id",
                "preview_id",
                "before",
                "after",
            }
        }
        payload["sources_display"] = sources_display
        diff_items.append(DiffPreviewItem(**payload))
    scope_raw = result.get("scope") if isinstance(result.get("scope"), dict) else {}
    scope_payload = {
        k: scope_raw[k]
        for k in (
            "matched_count",
            "selected_count",
            "date_from",
            "days",
            "label_codes",
            "truncated",
            "max_entries",
        )
        if k in scope_raw
    }
    scope = OrganizeScopeInfo(**scope_payload) if scope_payload else None
    hermes_raw = result.get("hermes_protocol")
    hermes_info = HermesProtocolInfo(**hermes_raw) if isinstance(hermes_raw, dict) else None

    return OrganizePreviewResponse(
        status=result.get("status") or "preview",
        message=message,
        goal=result.get("goal"),
        diff_preview=diff_items,
        source_entry_ids=list(result.get("source_entry_ids") or []),
        used_default_entries=bool(result.get("used_default_entries")),
        preview_id=result.get("preview_id"),
        preview_token=result.get("preview_token"),
        preview_session_id=result.get("preview_session_id"),
        scope=scope,
        hermes_protocol=hermes_info,
    ), _model_fallback_info(result.get("model_fallback"))


@router.post("/organize/scope-preview", response_model=OrganizeScopePreviewResponse)
def organize_scope_preview(
    request: OrganizeScopeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """只读范围预览：按当前用户隔离，服务端重算条数。"""
    _ensure_ai_ready(db, current_user)
    data = preview_organize_scope(
        db,
        current_user.id,
        days=request.days,
        label_codes=request.label_codes,
        entry_ids=request.entry_ids,
    )
    return OrganizeScopePreviewResponse(**data)


@router.post("/organize/preview", response_model=OrganizePreviewResponse)
async def organize_preview(
    request: OrganizePreviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """整合模式 preview：只读生成 diff，不写库。"""
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "organize", getattr(request, "model_key", None))
    preview, _ = await _run_organize_preview(db, current_user.id, request, model_key)
    if not preview.source_entry_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ORGANIZE_SCOPE_EMPTY", "message": "没有可用于整合的记录"},
        )
    return preview


@router.post("/organize/chat", response_model=OrganizeChatResponse)
async def organize_chat(
    request: OrganizeChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """整合模式聊天：成功时原子写入 user+assistant preview；失败不留半消息。"""
    _ensure_ai_ready(db, current_user)
    model_key = _resolve_request_model(db, current_user, "organize", request.model_key)

    session_id = _get_or_create_session_id(db, current_user.id, request.session_id)
    try:
        validate_session_for_mode(db, current_user.id, session_id, "organize")
    except SessionPersonaError as exc:
        raise _session_error_http(exc) from exc

    # Generate preview BEFORE any message commit.
    try:
        preview, model_fallback = await _run_organize_preview(
            db, current_user.id, request, model_key, session_id=session_id
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    if not preview.source_entry_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "ORGANIZE_SCOPE_EMPTY", "message": "没有可用于整合的记录"},
        )

    entries = load_user_entries_by_ids(db, current_user.id, preview.source_entry_ids)
    valid_refs = _entries_to_valid_refs(db, entries)
    summary = preview.message or "已生成整合改动预览。"

    try:
        from app.services.ai_session import touch_session_activity

        save_message(
            db,
            current_user.id,
            session_id,
            "user",
            request.message,
            commit=False,
        )
        save_message(
            db,
            current_user.id,
            session_id,
            "assistant",
            summary,
            {
                "kind": "organize",
                "mode": "organize",
                "organize_preview": preview.model_dump(),
                "valid_references": [ref.model_dump() for ref in valid_refs],
            },
            commit=False,
        )
        touch_session_activity(db, current_user.id, session_id, commit=False)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "INTERNAL_ERROR", "message": "整理预览保存失败，请重试"},
        )

    return OrganizeChatResponse(
        session_id=session_id,
        message=summary,
        mode="organize",
        organize_preview=preview,
        valid_references=valid_refs,
        model_fallback=model_fallback,
    )


@router.post("/organize/confirm", response_model=OrganizeConfirmResponse)
def organize_confirm(
    request: OrganizeConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """整合模式安全确认写入：只写 ai_derived_contents，不改 Entry 原文。"""
    _ensure_ai_ready(db, current_user)
    items = [item.model_dump() for item in request.items]
    created = confirm_organize_items(
        db,
        current_user.id,
        source_entry_ids=request.source_entry_ids or [],
        items=items,
        goal=request.goal,
        metadata=request.metadata,
        preview_id=request.preview_id,
        preview_token=request.preview_token,
    )
    bound_ids = sorted({int(i) for row in created for i in (row.get("source_entry_ids") or [])})
    return OrganizeConfirmResponse(
        created=[OrganizeConfirmCreatedItem(**row) for row in created],
        source_entry_ids=bound_ids or sorted(set(int(i) for i in (request.source_entry_ids or []))),
    )


@router.post("/entries/{entry_id}/annotations/preview", response_model=AnnotationPreviewResponse)
async def annotation_preview(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """为单条记录生成 AI 批注 preview，不写库。"""
    _ensure_ai_ready(db, current_user)
    user = db.query(User).filter(User.id == current_user.id).first()
    model_key = resolve_model_key_for_mode(db, user, "review", None)
    fallback = get_fallback_candidates(db, user, model_key) if user else []
    result = await generate_annotation_preview(
        db,
        user_id=current_user.id,
        entry_id=entry_id,
        model_key=model_key,
        fallback_candidates=fallback,
    )
    if result.get("error") == "ENTRY_NOT_FOUND":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ENTRY_NOT_FOUND", "message": "记录不存在或无权访问"},
        )
    return AnnotationPreviewResponse(
        entry_id=entry_id,
        source_entry_ids=result.get("source_entry_ids") or [entry_id],
        summary=result.get("summary") or "",
        action_hint=result.get("action_hint") or "",
        confidence=result.get("confidence"),
        model_key=result.get("model_key"),
        model_fallback=_model_fallback_info(result.get("model_fallback")),
    )


@router.post("/entries/{entry_id}/annotations/confirm", response_model=AnnotationConfirmResponse)
def annotation_confirm(
    entry_id: int,
    request: AnnotationConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """确认写入 AI 批注到 ai_derived_contents，不改 Entry 原文。"""
    if not request.summary.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_CONTENT", "message": "批注内容不能为空"},
        )
    row = confirm_annotation(
        db,
        current_user.id,
        entry_id,
        summary=request.summary,
        action_hint=request.action_hint or "",
        confidence=request.confidence,
        model_key=request.model_key,
        metadata=request.metadata,
    )
    created_at = row.created_at.isoformat() if row.created_at else now_local().isoformat()
    return AnnotationConfirmResponse(
        id=int(row.id),
        source_entry_ids=row.source_entry_ids or [entry_id],
        created_at=created_at,
    )


@router.get("/auto-structure/status", response_model=AutoStructureStatusResponse)
def auto_structure_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hermes 自动结构化任务状态（Phase 8A）。"""
    from app.services.hermes_auto_structure import get_auto_structure_status

    data = get_auto_structure_status(db, current_user.id)
    return AutoStructureStatusResponse(**data)


@router.post("/auto-structure/run", response_model=AutoStructureRunResponse)
def auto_structure_run(
    dry_run: bool = Query(default=True, description="true=预览，不推进游标不写库"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """触发当前用户的 Hermes 自动结构化（按 user_id 隔离）。"""
    from app.services.hermes_auto_structure import run_hermes_auto_structure

    result = run_hermes_auto_structure(db, current_user.id, dry_run=dry_run)
    return AutoStructureRunResponse(
        status=result.get("status", "unknown"),
        processed=int(result.get("processed") or 0),
        source_entry_ids=result.get("source_entry_ids") or [],
        last_processed_at=result.get("last_processed_at"),
        processed_until=result.get("processed_until"),
        next_cursor=result.get("next_cursor"),
        derived_content_ids=result.get("derived_content_ids") or [],
        message=result.get("message"),
        preview_outputs=result.get("preview_outputs"),
    )


@router.get("/scheduled-review/status", response_model=ScheduledReviewStatusResponse)
def scheduled_review_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """自动复盘调度状态（Phase 10：含授权信息，不暴露未授权用户的记录细节）。"""
    from app.services.scheduled_review_service import get_scheduled_review_status

    data = get_scheduled_review_status(db, current_user.id)
    return ScheduledReviewStatusResponse(**data)


@router.post("/scheduled-review/run", response_model=ScheduledReviewRunResponse)
def scheduled_review_run(
    dry_run: bool = Query(default=True, description="true=预览，不推进游标不写 draft"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """触发当前用户的自动复盘（需管理员授权，只写 draft）。"""
    from app.services.hermes_review_permissions import assert_user_hermes_review_authorized
    from app.services.scheduled_review_service import run_scheduled_review

    assert_user_hermes_review_authorized(db, current_user)
    result = run_scheduled_review(db, current_user.id, dry_run=dry_run, skip_auth=True)
    return ScheduledReviewRunResponse(
        status=result.get("status", "unknown"),
        processed=int(result.get("processed") or 0),
        source_entry_ids=result.get("source_entry_ids") or [],
        draft_ids=result.get("draft_ids") or [],
        scheduled_review_last_at=result.get("scheduled_review_last_at"),
        processed_until=result.get("processed_until"),
        message=result.get("message"),
        preview_outputs=result.get("preview_outputs"),
    )

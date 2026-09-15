"""
配置管理模块
从环境变量读取配置，使用 Pydantic Settings
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
from pathlib import Path


class Settings(BaseSettings):
    """应用配置"""
    
    # 数据库配置（从环境变量 DATABASE_URL 读取）
    database_url: str = Field(validation_alias="DATABASE_URL")
    
    # JWT 配置（从环境变量 JWT_SECRET 读取）
    jwt_secret: str = Field(validation_alias="JWT_SECRET")
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24
    
    # CORS 配置（从环境变量 CORS_ORIGINS 读取，逗号分隔）
    cors_origins: List[str] = ["http://localhost:5173"]
    
    # 文件上传配置
    upload_dir: str = "uploads"
    max_file_size: int = 52428800
    allowed_mime_types: List[str] = [
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ]
    attachment_ocr_provider: str = Field(default="pytesseract", validation_alias="ATTACHMENT_OCR_PROVIDER")
    attachment_ocr_pipeline_version: str = Field(
        default="quality-gated-rapidocr-v1",
        validation_alias="ATTACHMENT_OCR_PIPELINE_VERSION",
    )
    attachment_tesseract_lang: str = Field(default="chi_sim+eng", validation_alias="ATTACHMENT_TESSERACT_LANG")
    # Optional absolute/relative path to tesseract executable (Windows/Linux). Empty = PATH lookup.
    attachment_tesseract_cmd: Optional[str] = Field(default=None, validation_alias="ATTACHMENT_TESSERACT_CMD")
    # When true, image OCR unavailable/failed fails the attachment; default false keeps caption-only indexed.
    attachment_image_ocr_required: bool = Field(
        default=False,
        validation_alias="ATTACHMENT_IMAGE_OCR_REQUIRED",
    )
    attachment_paddleocr_lang: str = Field(default="ch", validation_alias="ATTACHMENT_PADDLEOCR_LANG")
    attachment_paddleocr_use_angle_cls: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_PADDLEOCR_USE_ANGLE_CLS",
    )
    # R5.3.2 / R5.5A: Tesseract first; RapidOCR on low_quality/unusable/empty.
    # R5.5B: also verify when primary is "ok" but confidence is missing or weak
    # (CJK-looking garbage can pass ratio gates without being searchable).
    attachment_rapidocr_fallback_enabled: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_RAPIDOCR_FALLBACK_ENABLED",
    )
    attachment_rapidocr_verify_below_confidence: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        validation_alias="ATTACHMENT_RAPIDOCR_VERIFY_BELOW_CONFIDENCE",
    )
    attachment_rapidocr_model_dir: Optional[str] = Field(
        default=None,
        validation_alias="ATTACHMENT_RAPIDOCR_MODEL_DIR",
    )
    attachment_rapidocr_det_name: str = Field(
        default="PP-OCRv6_det_small.onnx",
        validation_alias="ATTACHMENT_RAPIDOCR_DET_NAME",
    )
    attachment_rapidocr_rec_name: str = Field(
        default="PP-OCRv6_rec_small.onnx",
        validation_alias="ATTACHMENT_RAPIDOCR_REC_NAME",
    )
    attachment_rapidocr_cls_name: str = Field(
        default="ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        validation_alias="ATTACHMENT_RAPIDOCR_CLS_NAME",
    )
    attachment_rapidocr_timeout_sec: int = Field(
        default=60,
        validation_alias="ATTACHMENT_RAPIDOCR_TIMEOUT_SEC",
    )
    attachment_rapidocr_max_output_bytes: int = Field(
        default=256000,
        validation_alias="ATTACHMENT_RAPIDOCR_MAX_OUTPUT_BYTES",
    )
    attachment_pdf_ocr_fallback_enabled: bool = Field(
        default=False,
        validation_alias="ATTACHMENT_PDF_OCR_FALLBACK_ENABLED",
    )
    attachment_pdf_ocr_dpi: int = Field(default=180, validation_alias="ATTACHMENT_PDF_OCR_DPI")
    # When migration 014 jobs table exists, upload still schedules a safe BackgroundTasks
    # fallback so attachments do not stay uploaded/pending without a dedicated worker.
    # Set false only when an independent process_attachments worker is always running.
    attachment_upload_background_fallback_enabled: bool = Field(
        default=True,
        validation_alias="ATTACHMENT_UPLOAD_BACKGROUND_FALLBACK_ENABLED",
    )
    attachment_upload_fallback_max_concurrency: int = Field(
        default=1,
        validation_alias="ATTACHMENT_UPLOAD_FALLBACK_MAX_CONCURRENCY",
    )
    
    # 日志配置
    log_level: str = "INFO"

    # Admin 接口鉴权（可选）：仅允许本机或 Bearer ADMIN_TOKEN 调用 POST /api/admin/*
    admin_token: Optional[str] = Field(default=None, validation_alias="ADMIN_TOKEN")

    # DeepSeek API 配置（Phase C.1.1 默认且唯一运行时 LLM provider）
    deepseek_api_key: Optional[str] = Field(default=None, validation_alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", validation_alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-v4-pro", validation_alias="DEEPSEEK_MODEL")

    # LLM HTTP timeouts (seconds). Explicit finite bounds — never rely on SDK 600s defaults.
    llm_connect_timeout_seconds: float = Field(default=10.0, validation_alias="LLM_CONNECT_TIMEOUT_SECONDS")
    llm_read_timeout_seconds: float = Field(default=180.0, validation_alias="LLM_READ_TIMEOUT_SECONDS")
    llm_write_timeout_seconds: float = Field(default=60.0, validation_alias="LLM_WRITE_TIMEOUT_SECONDS")
    llm_pool_timeout_seconds: float = Field(default=30.0, validation_alias="LLM_POOL_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(default=1, ge=0, le=3, validation_alias="LLM_MAX_RETRIES")

    # R11.3-Fix2: durable stream request_id claim lease / heartbeat (seconds).
    # Default lease covers a typical LLM read timeout; heartbeat renews before expiry.
    stream_claim_lease_seconds: int = Field(
        default=120, ge=15, le=3600, validation_alias="STREAM_CLAIM_LEASE_SECONDS"
    )
    stream_claim_heartbeat_seconds: float = Field(
        default=30.0, ge=5.0, le=600.0, validation_alias="STREAM_CLAIM_HEARTBEAT_SECONDS"
    )
    # Public SSE transport heartbeat. This is separate from the database claim heartbeat.
    sse_keepalive_seconds: float = Field(
        default=15.0, ge=5.0, le=60.0, validation_alias="SSE_KEEPALIVE_SECONDS"
    )

    # Runtime semantic version baked into immutable production images.
    growthlog_version: str = Field(
        default="0.0.0-dev", validation_alias="GROWTHLOG_VERSION"
    )

    # Deprecated after Phase C.1.1: kept for old .env compatibility; runtime does not read these.
    minimax_api_key: Optional[str] = Field(default=None, validation_alias="MINIMAX_API_KEY")
    minimax_model: str = "MiniMax-M2.7"

    # Embedding 模型配置
    embedding_model: str = "intfloat/multilingual-e5-base"
    embedding_dimension: int = 768

    # Visual RAG provider: disabled | fake | openclip
    visual_rag_provider: str = Field(default="disabled", validation_alias="VISUAL_RAG_PROVIDER")
    visual_rag_model: str = Field(default="ViT-B-32", validation_alias="VISUAL_RAG_MODEL")
    visual_rag_pretrained: str = Field(
        default="laion2b_s34b_b79k",
        validation_alias="VISUAL_RAG_PRETRAINED",
    )
    visual_rag_device: str = Field(default="cpu", validation_alias="VISUAL_RAG_DEVICE")
    visual_rag_embedding_dim: int = Field(default=512, validation_alias="VISUAL_RAG_EMBEDDING_DIM")

    # Visual late fusion into Hybrid RAG (Phase 5E). Default off — online text RAG unchanged.
    rag_enable_visual_late_fusion: bool = Field(
        default=False,
        validation_alias="RAG_ENABLE_VISUAL_LATE_FUSION",
    )
    rag_visual_top_k: int = Field(default=3, validation_alias="RAG_VISUAL_TOP_K")
    rag_visual_min_score: float = Field(default=0.25, validation_alias="RAG_VISUAL_MIN_SCORE")

    # Visual index disk snapshot (Phase 5F). Default off — memory index unchanged.
    visual_index_persistence_enabled: bool = Field(
        default=False,
        validation_alias="VISUAL_INDEX_PERSISTENCE_ENABLED",
    )
    visual_index_cache_dir: str = Field(
        default="vector_indexes/visual",
        validation_alias="VISUAL_INDEX_CACHE_DIR",
    )

    # AI session vector index disk snapshot (Agent Phase 6D). Default off.
    ai_session_index_persistence_enabled: bool = Field(
        default=False,
        validation_alias="AI_SESSION_INDEX_PERSISTENCE_ENABLED",
    )
    ai_session_index_cache_dir: str = Field(
        default="vector_indexes/ai_sessions",
        validation_alias="AI_SESSION_INDEX_CACHE_DIR",
    )

    # Notification worker runtime controls. Delivery is Web Push only.
    notification_public_base_url: str = Field(
        default="http://localhost:8000",
        validation_alias="NOTIFICATION_PUBLIC_BASE_URL",
    )
    notification_worker_enabled: bool = Field(
        default=False,
        validation_alias="NOTIFICATION_WORKER_ENABLED",
    )
    notification_bind_start_cooldown_seconds: int = Field(
        default=30,
        ge=1,
        validation_alias="NOTIFICATION_BIND_START_COOLDOWN_SECONDS",
    )
    notification_test_cooldown_seconds: int = Field(
        default=60,
        ge=1,
        validation_alias="NOTIFICATION_TEST_COOLDOWN_SECONDS",
    )

    # R10 Web Push (VAPID). Private key never returned to clients or logs.
    web_push_enabled: bool = Field(default=False, validation_alias="WEB_PUSH_ENABLED")
    web_push_vapid_public_key: Optional[str] = Field(
        default=None, validation_alias="WEB_PUSH_VAPID_PUBLIC_KEY"
    )
    web_push_vapid_private_key: Optional[str] = Field(
        default=None, validation_alias="WEB_PUSH_VAPID_PRIVATE_KEY"
    )
    web_push_vapid_subject: str = Field(
        default="mailto:admin@example.com",
        validation_alias="WEB_PUSH_VAPID_SUBJECT",
    )
    web_push_subscription_encryption_key: Optional[str] = Field(
        default=None,
        validation_alias="WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY",
    )
    web_push_use_fake: bool = Field(default=False, validation_alias="WEB_PUSH_USE_FAKE")
    # Comma-separated host suffixes for Push Service endpoints (exact or subdomain).
    web_push_allowed_host_suffixes: Optional[str] = Field(
        default=None,
        validation_alias="WEB_PUSH_ALLOWED_HOST_SUFFIXES",
    )

    # Notion read-only integration. Secrets never returned to clients or logs.
    notion_integration_enabled: bool = Field(default=False, validation_alias="NOTION_INTEGRATION_ENABLED")
    notion_sync_enabled: bool = Field(default=True, validation_alias="NOTION_SYNC_ENABLED")
    notion_oauth_client_id: Optional[str] = Field(default=None, validation_alias="NOTION_OAUTH_CLIENT_ID")
    notion_oauth_client_secret: Optional[str] = Field(default=None, validation_alias="NOTION_OAUTH_CLIENT_SECRET")
    notion_oauth_redirect_uri: Optional[str] = Field(default=None, validation_alias="NOTION_OAUTH_REDIRECT_URI")
    notion_api_version: str = Field(default="2026-03-11", validation_alias="NOTION_API_VERSION")
    notion_api_host: str = Field(default="https://api.notion.com", validation_alias="NOTION_API_HOST")
    notion_token_encryption_key: Optional[str] = Field(
        default=None,
        validation_alias="NOTION_TOKEN_ENCRYPTION_KEY",
    )
    notion_webhook_verification_token: Optional[str] = Field(
        default=None,
        validation_alias="NOTION_WEBHOOK_VERIFICATION_TOKEN",
    )
    notion_webhook_bootstrap_file: Optional[str] = Field(
        default=None,
        validation_alias="NOTION_WEBHOOK_BOOTSTRAP_FILE",
    )
    notion_use_fake: bool = Field(default=False, validation_alias="NOTION_USE_FAKE")

    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )


# 全局配置实例
settings = Settings()

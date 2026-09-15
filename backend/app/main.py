"""
FastAPI 应用入口
配置 CORS、路由、静态文件服务
"""
import logging
import re
import threading

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.config import settings

# 初始化日志配置（遵循 settings.log_level；默认 INFO）
from app.logging_config import setup_logging
setup_logging(settings.log_level)

logger = logging.getLogger(__name__)

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

# 创建 FastAPI 应用
app = FastAPI(
    title="Growth Log API",
    description="成长记录系统 API",
    version="0.1.0"
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由（必须在 SPA fallback 之前注册）
from app.routes import auth, labels, entries, admin, todos, ai, attachments, notifications, notion

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(labels.router, prefix="/api/labels", tags=["labels"])
app.include_router(entries.router, prefix="/api/entries", tags=["entries"])
app.include_router(todos.router, prefix="/api/todos", tags=["todos"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(attachments.router, prefix="/api", tags=["attachments"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
app.include_router(notion.router, prefix="/api/integrations/notion", tags=["notion"])


@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return {"ok": True}


@app.get("/api/version")
async def runtime_version(response: Response):
    """Return only the sanitized semantic version of the running image."""
    from app.services.runtime_version import sanitize_public_version

    response.headers["Cache-Control"] = "no-store"
    return {"version": sanitize_public_version(settings.growthlog_version)}


# 静态文件服务（生产环境：前端 build 产物）
# 注意：必须在 API 路由之后注册，避免拦截 /api 请求
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    def _current_asset_name(extension: str) -> str | None:
        """Return the asset filename referenced by the current SPA index."""
        index_file = static_dir / "index.html"
        if index_file.exists():
            match = re.search(
                rf"/assets/(index-[^\"']+\.{extension})",
                index_file.read_text(encoding="utf-8", errors="ignore"),
            )
            if match:
                return match.group(1)

        assets_dir = static_dir / "assets"
        candidates = sorted(
            assets_dir.glob(f"index-*.{extension}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return candidates[0].name if candidates else None

    class GrowthStaticFiles(StaticFiles):
        """Serve Vite assets and tolerate stale hashed asset URLs."""

        async def get_response(self, path: str, scope):
            response = None
            try:
                response = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code != 404:
                    raise

            if response is not None and response.status_code != 404:
                response.headers.setdefault(
                    "Cache-Control",
                    "public, max-age=31536000, immutable",
                )
                return response

            requested_name = Path(path).name
            if re.fullmatch(r"index-[A-Za-z0-9_-]+\.(js|css)", requested_name):
                extension = requested_name.rsplit(".", 1)[1]
                current_name = _current_asset_name(extension)
                if current_name:
                    fallback_file = static_dir / "assets" / current_name
                    media_type = "application/javascript" if extension == "js" else "text/css"
                    return FileResponse(
                        str(fallback_file),
                        media_type=media_type,
                        headers=NO_STORE_HEADERS,
                    )

            if response is not None:
                response.headers.update(NO_STORE_HEADERS)
                return response

            raise StarletteHTTPException(status_code=404)

    # 挂载静态文件到根路径，这样 /assets/... 可以直接访问
    app.mount("/assets", GrowthStaticFiles(directory=str(static_dir / "assets")), name="assets")
    app.mount("/static", GrowthStaticFiles(directory=str(static_dir)), name="static")
    
    # Root-level PWA / brand assets (must not fall through to SPA index.html)
    ROOT_STATIC_FILES = {
        "favicon.svg": "image/svg+xml",
        "manifest.json": "application/manifest+json",
        "release-notes.json": "application/json",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
        "service-worker.js": "application/javascript",
        "sw-url-safety.js": "application/javascript",
        # Temporary R10.2 isolate CA for LAN HTTPS acceptance only (public cert).
        "_r102_temp_ca.cer": "application/x-x509-ca-cert",
    }

    # SPA 路由：所有非 API/static/assets 请求返回 index.html
    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
    async def serve_spa(full_path: str):
        """生产环境：前端路由回退到 index.html"""
        # 排除 API、静态资源和 assets 路径
        if full_path.startswith("api") or full_path.startswith("static") or full_path.startswith("assets"):
            return {"detail": "Not found"}

        # Serve real root static files when present (PWA icons / favicon / manifest)
        if full_path in ROOT_STATIC_FILES:
            candidate = static_dir / full_path
            if candidate.is_file():
                return FileResponse(
                    str(candidate),
                    media_type=ROOT_STATIC_FILES[full_path],
                    headers=NO_STORE_HEADERS,
                )

        index_file = static_dir / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file), headers=NO_STORE_HEADERS)
        return {"detail": "Not found"}


@app.on_event("startup")
def startup_event():
    """
    后端启动时自动同步所有用户的向量数据
    使用后台线程执行，不阻塞服务启动
    """
    import os

    if (os.environ.get("RAG_STARTUP_SYNC_DISABLED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        logger.info("[Startup] embedding sync disabled by RAG_STARTUP_SYNC_DISABLED")
        return

    from app.services.startup_sync import sync_all_users_embeddings

    t = threading.Thread(target=sync_all_users_embeddings, daemon=True)
    t.start()
    logger.info("[Startup] 向量同步任务已启动")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    全局异常处理器
    捕获所有未处理的异常，记录完整堆栈信息
    """
    import traceback
    logger.error(f"[全局异常] {request.method} {request.url}")
    logger.error(f"[异常类型] {type(exc).__name__}: {exc}")
    logger.error(f"[堆栈信息]\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {type(exc).__name__}"},
    )

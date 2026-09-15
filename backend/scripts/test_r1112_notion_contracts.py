"""GrowthLog 11.12.0 Notion contracts: crypto, OAuth, fake provider, webhook, sync, AI."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("NOTION_USE_FAKE", "true")
os.environ.setdefault("NOTION_INTEGRATION_ENABLED", "true")
os.environ.setdefault("NOTION_SYNC_ENABLED", "true")
os.environ.setdefault("NOTION_OAUTH_CLIENT_ID", "fake-client")
os.environ.setdefault("NOTION_OAUTH_CLIENT_SECRET", "fake-secret")
os.environ.setdefault("NOTION_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/api/integrations/notion/oauth/callback")
os.environ.setdefault("NOTION_TOKEN_ENCRYPTION_KEY", "0" * 64)
os.environ.setdefault("NOTION_WEBHOOK_VERIFICATION_TOKEN", "webhook-secret")

from app.config import settings

settings.notion_use_fake = True
settings.notion_integration_enabled = True
settings.notion_sync_enabled = True
settings.notion_oauth_client_id = "fake-client"
settings.notion_oauth_client_secret = "fake-secret"
settings.notion_oauth_redirect_uri = "http://127.0.0.1:8000/api/integrations/notion/oauth/callback"
settings.notion_token_encryption_key = "0" * 64
settings.notion_webhook_verification_token = "webhook-secret"

from app.database import Base
from app.models import (
    KnowledgeChunk,
    KnowledgeEmbedding,
    KnowledgeSource,
    NotionConnection,
    NotionOAuthState,
    NotionPage,
    NotionSyncJob,
    User,
)
from app.services import knowledge_rag
from app.services.notion_crypto import PURPOSE_ACCESS, PURPOSE_REFRESH, decrypt_token, encrypt_token, NotionCryptoError
from app.services.notion_errors import (
    NOTION_OAUTH_STATE_EXPIRED,
    NOTION_OAUTH_STATE_INVALID,
    NOTION_SOURCE_SYNC_PENDING,
    NOTION_WEBHOOK_INVALID,
)
from app.services.notion_jobs import (
    claim_is_active,
    claim_notion_jobs,
    enqueue_job,
    finalize_job,
    renew_lease,
)
from app.services.notion_knowledge import search_keyword_notion_pages
from app.services.notion_oauth import handle_callback, start_oauth
from app.services.notion_provider import get_fake_notion_provider, reset_fake_notion_provider
from app.services.notion_reference import sanitize_notion_url
from app.services.notion_sync import (
    accept_webhook_verification,
    disconnect,
    enqueue_webhook_event,
    process_claimed_job,
    run_discovery,
    sync_one_page,
    verify_webhook_signature,
)
from app.services.reference_identity import canonical_reference_key, parse_proposal_reference_key
from app.timeutil import now_local

_SEQ = {"user": 0, "connection": 0, "page": 0, "job": 0, "state": 0, "source": 0, "chunk": 0, "embedding": 0}


def _next(kind: str) -> int:
    _SEQ[kind] += 1
    return _SEQ[kind]


@contextmanager
def isolated_db() -> Iterator[Session]:
    for key in _SEQ:
        _SEQ[key] = 0
    fake_path = Path(tempfile.mkdtemp(prefix="r1112_fake_")) / "state.json"
    reset_fake_notion_provider(fake_path)
    os.environ["NOTION_FAKE_STATE_PATH"] = str(fake_path)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            NotionConnection.__table__,
            NotionOAuthState.__table__,
            NotionPage.__table__,
            NotionSyncJob.__table__,
            KnowledgeSource.__table__,
            KnowledgeChunk.__table__,
            KnowledgeEmbedding.__table__,
        ],
    )
    Local = sessionmaker(bind=engine, expire_on_commit=False)

    @event.listens_for(Local, "before_flush")
    def _ids(session, _ctx, _instances):  # noqa: ANN001
        mapping = {
            User: "user",
            NotionConnection: "connection",
            NotionPage: "page",
            NotionSyncJob: "job",
            NotionOAuthState: "state",
            KnowledgeSource: "source",
            KnowledgeChunk: "chunk",
            KnowledgeEmbedding: "embedding",
        }
        for obj in session.new:
            kind = mapping.get(type(obj))
            if kind and getattr(obj, "id", None) is None:
                obj.id = _next(kind)

    db = Local()
    knowledge_rag._KNOWLEDGE_TABLES_CACHED = True
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        engine.dispose()
        knowledge_rag._KNOWLEDGE_TABLES_CACHED = None


def _user(db: Session, name: str = "owner") -> User:
    user = User(username=name, password_hash="x", is_active=True, is_admin=False)
    db.add(user)
    db.flush()
    return user


def test_aes_gcm() -> None:
    blob = encrypt_token("secret-token", purpose=PURPOSE_ACCESS, user_id=1, bot_id="bot", workspace_id="ws")
    assert decrypt_token(blob, purpose=PURPOSE_ACCESS, user_id=1, bot_id="bot", workspace_id="ws") == "secret-token"
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    try:
        decrypt_token(bytes(tampered), purpose=PURPOSE_ACCESS, user_id=1, bot_id="bot", workspace_id="ws")
        raise AssertionError("tamper should fail")
    except NotionCryptoError:
        pass
    try:
        decrypt_token(blob, purpose=PURPOSE_ACCESS, user_id=2, bot_id="bot", workspace_id="ws")
        raise AssertionError("wrong aad should fail")
    except NotionCryptoError:
        pass
    try:
        decrypt_token(blob, purpose=PURPOSE_REFRESH, user_id=1, bot_id="bot", workspace_id="ws")
        raise AssertionError("wrong purpose should fail")
    except NotionCryptoError:
        pass


def test_oauth_states() -> None:
    with isolated_db() as db:
        user = _user(db)
        started = start_oauth(db, int(user.id))
        assert "api.notion.com" in started["authorization_url"]
        state = started["authorization_url"].split("state=")[1]
        handle_callback(db, code="ok", state=state, error=None)
        conn = db.query(NotionConnection).filter(NotionConnection.user_id == user.id).one()
        assert conn.status == "active"
        assert b"fake-access" not in conn.access_token_encrypted
        try:
            handle_callback(db, code="ok", state=state, error=None)
            raise AssertionError("replay should fail")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_OAUTH_STATE_INVALID
        try:
            handle_callback(db, code="ok", state="missing", error=None)
            raise AssertionError("bad state should fail")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_OAUTH_STATE_INVALID
        expired = start_oauth(db, int(user.id))
        raw = expired["authorization_url"].split("state=")[1]
        row = db.query(NotionOAuthState).filter(NotionOAuthState.consumed_at.is_(None)).one()
        row.expires_at = now_local()
        db.add(row)
        db.commit()
        try:
            handle_callback(db, code="ok", state=raw, error=None)
            raise AssertionError("expired should fail")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_OAUTH_STATE_EXPIRED
        try:
            handle_callback(db, code=None, state="x", error="access_denied")
            raise AssertionError("cancel should fail")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_OAUTH_STATE_INVALID


def test_fake_provider_and_sync() -> None:
    with isolated_db() as db:
        user = _user(db)
        started = start_oauth(db, int(user.id))
        handle_callback(db, code="ok", state=started["authorization_url"].split("state=")[1], error=None)
        connection = db.query(NotionConnection).one()
        fake = get_fake_notion_provider()
        pages = []
        cursor = None
        while True:
            payload = fake.search_pages("t", connection_key="1", start_cursor=cursor)
            pages.extend(payload["results"])
            if not payload["has_more"]:
                break
            cursor = payload["next_cursor"]
        assert len(pages) >= 3
        blocks = []
        cursor = None
        while True:
            payload = fake.list_block_children("t", "22222222-2222-2222-2222-222222222222", connection_key="1", start_cursor=cursor)
            blocks.extend(payload["results"])
            if not payload["has_more"]:
                break
            cursor = payload["next_cursor"]
        assert any("alphafox" in json.dumps(block) for block in blocks)
        fake.configure(force_429=True)
        try:
            from app.services.notion_provider import NotionProviderError

            fake.search_pages("t", connection_key="1")
            raise AssertionError("429 expected")
        except NotionProviderError as exc:
            assert exc.code == "NOTION_RATE_LIMITED"
        fake.configure(force_401=True, consumed_401=False, fail_refresh=False)
        try:
            fake.search_pages("t", connection_key="1")
        except NotionProviderError:
            fake.refresh_token("x")
        fake.configure(permission_lost_ids=["22222222-2222-2222-2222-222222222222"])
        try:
            fake.get_page("t", "22222222-2222-2222-2222-222222222222", connection_key="1")
            raise AssertionError("permission lost expected")
        except NotionProviderError as exc:
            assert exc.code == "NOTION_PERMISSION_LOST"
        fake.configure(deleted_ids=["33333333-3333-3333-3333-333333333333"], permission_lost_ids=[])
        try:
            fake.get_page("t", "33333333-3333-3333-3333-333333333333", connection_key="1")
            raise AssertionError("deleted expected")
        except NotionProviderError as exc:
            assert exc.code == "NOTION_REMOTE_DELETED"
        fake.configure(force_401=False, force_429=False, permission_lost_ids=[], deleted_ids=[])

        with patch("app.services.notion_knowledge.generate_embedding", return_value=[0.1] * 8):
            run_discovery(db, connection)
            jobs = db.query(NotionSyncJob).filter(NotionSyncJob.job_type == "sync_page").all()
            assert jobs
            for job in jobs:
                page = db.query(NotionPage).filter(NotionPage.id == job.notion_page_id).one()
                sync_one_page(db, connection, page)
            pages = db.query(NotionPage).all()
            database_row = next(
                page
                for page in pages
                if str(page.notion_page_uuid) == "44444444-4444-4444-4444-444444444444"
            )
            assert database_row.breadcrumb == "Parent Page / Fake DB / Database Row"
            indexed = [page for page in pages if page.sync_status in {"indexed", "partial"}]
            assert indexed
            first = indexed[0]
            first_hash = first.indexed_content_hash
            with patch("app.services.notion_knowledge.generate_embedding") as emb:
                sync_one_page(db, connection, first)
                emb.assert_not_called()
            assert first.indexed_content_hash == first_hash
            with patch("app.services.notion_sync._collect_all_pages", return_value=[]):
                run_discovery(db, connection)
            assert db.query(NotionSyncJob).filter(NotionSyncJob.job_type == "delete_page").count() == 0
            assert first.sync_status in {"indexed", "partial"}
            hits = search_keyword_notion_pages(db, user_id=int(user.id), terms=["alphafox"])
            assert hits
            assert all(item["source_type"] == "notion_page" for item in hits)
            first.observed_content_hash = "0" * 64
            db.add(first)
            db.commit()
            stale = search_keyword_notion_pages(db, user_id=int(user.id), terms=["alphafox"])
            assert all(int(item["source_id"]) != int(first.id) for item in stale)
            disconnect(db, int(user.id))
            assert db.query(KnowledgeSource).count() == 0
            assert db.query(KnowledgeChunk).count() == 0


def test_webhook_and_jobs() -> None:
    secret = "webhook-secret"
    body = b'{"id":"evt-1","type":"page.content_updated","workspace_id":"ws-fake","entity":{"id":"11111111-1111-1111-1111-111111111111"}}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    verify_webhook_signature(body, f"sha256={sig}")
    with patch.object(settings, "notion_webhook_verification_token", "verification-token-1234"):
        assert accept_webhook_verification({"verification_token": "verification-token-1234"}, None)
        assert not accept_webhook_verification({"verification_token": "verification-token-1234"}, "signed")
        try:
            accept_webhook_verification({"verification_token": "wrong-verification-token"}, None)
            raise AssertionError("verification mismatch")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_WEBHOOK_INVALID
    with tempfile.TemporaryDirectory(prefix="r1112_webhook_") as tmp:
        bootstrap = Path(tmp) / "verification-token"
        with (
            patch.object(settings, "notion_webhook_verification_token", None),
            patch.object(settings, "notion_webhook_bootstrap_file", str(bootstrap)),
        ):
            bootstrap_token = "bootstrap-verification-token-1234"
            assert accept_webhook_verification({"verification_token": bootstrap_token}, None)
            assert bootstrap.read_text(encoding="utf-8") == bootstrap_token
            bootstrap_body = b'{"id":"evt-bootstrap","workspace_id":"ws-fake","type":"page.content_updated"}'
            bootstrap_sig = hmac.new(bootstrap_token.encode(), bootstrap_body, hashlib.sha256).hexdigest()
            verify_webhook_signature(bootstrap_body, f"sha256={bootstrap_sig}")
            try:
                accept_webhook_verification({"verification_token": "different-bootstrap-token"}, None)
                raise AssertionError("bootstrap overwrite should fail")
            except Exception as exc:
                assert getattr(exc, "code", "") == NOTION_WEBHOOK_INVALID
    try:
        verify_webhook_signature(body, "sha256=deadbeef")
        raise AssertionError("bad signature")
    except Exception as exc:
        assert getattr(exc, "code", "") == NOTION_WEBHOOK_INVALID
    try:
        verify_webhook_signature(b"x" * (65 * 1024), f"sha256={sig}")
        raise AssertionError("oversize body should fail")
    except Exception as exc:
        assert getattr(exc, "code", "") == NOTION_WEBHOOK_INVALID
    with isolated_db() as db:
        user = _user(db)
        started = start_oauth(db, int(user.id))
        handle_callback(db, code="ok", state=started["authorization_url"].split("state=")[1], error=None)
        try:
            enqueue_webhook_event(db, {"id": "missing-workspace", "type": "page.content_updated"})
            raise AssertionError("missing workspace should fail")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_WEBHOOK_INVALID
            db.rollback()
        enqueue_webhook_event(db, json.loads(body))
        enqueue_webhook_event(db, json.loads(body))
        events = db.query(NotionSyncJob).filter(NotionSyncJob.provider_event_id.isnot(None)).all()
        assert len(events) == 1
        connection = db.query(NotionConnection).one()
        db.query(NotionSyncJob).delete()
        db.commit()
        filtered = json.loads(body)
        filtered["id"] = "evt-filtered"
        filtered["accessible_by"] = [{"id": "different-bot", "type": "bot"}]
        enqueue_webhook_event(db, filtered)
        assert db.query(NotionSyncJob).filter(NotionSyncJob.provider_event_id.like("%:evt-filtered")).count() == 0
        filtered["id"] = "evt-matching"
        filtered["accessible_by"] = [{"id": connection.bot_id, "type": "bot"}]
        enqueue_webhook_event(db, filtered)
        assert db.query(NotionSyncJob).filter(NotionSyncJob.provider_event_id.like("%:evt-matching")).count() == 1
        jobs = claim_notion_jobs(db, 2, "worker-a")
        assert jobs
        job_id = int(jobs[0].id)
        assert claim_is_active(db, job_id, "worker-a")
        assert not renew_lease(db, job_id, "worker-b")
        assert claim_is_active(db, job_id, "worker-a")
        assert not finalize_job(db, job_id, worker_id="worker-b", ok=True)
        assert finalize_job(db, job_id, worker_id="worker-a", ok=True)


def test_ai_identity_and_preview() -> None:
    from app.services.ai_source_set import SourceMemberIdentity, SourceSetError, _resolve_one_member
    from app.services.ai_stream_protocol import PURPOSE_ANSWER_REFERENCE, sign_reference_token
    from app.services.ai_stream_reference_preview import _notion_page_preview

    with isolated_db() as db:
        user = _user(db)
        other = _user(db, "other")
        started = start_oauth(db, int(user.id))
        handle_callback(db, code="ok", state=started["authorization_url"].split("state=")[1], error=None)
        connection = db.query(NotionConnection).one()
        with patch("app.services.notion_knowledge.generate_embedding", return_value=[0.2] * 8):
            run_discovery(db, connection)
            for job in db.query(NotionSyncJob).filter(NotionSyncJob.job_type == "sync_page").all():
                page = db.query(NotionPage).filter(NotionPage.id == job.notion_page_id).one()
                sync_one_page(db, connection, page)
        page = db.query(NotionPage).filter(NotionPage.sync_status.in_(("indexed", "partial"))).first()
        assert page is not None
        key = f"notion_page:source:{int(page.id)}"
        assert parse_proposal_reference_key(key) == ("notion_page", "source", int(page.id))
        assert canonical_reference_key({"source_type": "notion_page", "source_id": int(page.id)}) == key
        member = _resolve_one_member(db, int(user.id), SourceMemberIdentity(reference_key=key, display_order=0))
        assert member.source_type == "notion_page"
        try:
            _resolve_one_member(db, int(other.id), SourceMemberIdentity(reference_key=key, display_order=0))
            raise AssertionError("cross user")
        except SourceSetError:
            pass
        preview = _notion_page_preview(db, user_id=int(user.id), notion_page_id=int(page.id), display_index=1)
        assert preview["source_type"] == "notion_page"
        assert "open_url" in preview
        dumped = json.dumps(preview)
        assert "fake-access" not in dumped
        assert page.notion_page_uuid not in dumped
        token = sign_reference_token(
            user_id=int(user.id),
            session_id="s1",
            display_index=1,
            purpose=PURPOSE_ANSWER_REFERENCE,
            reference_key=key,
        )
        assert token
        assert sanitize_notion_url("https://evil.example/redirect") is None
        assert sanitize_notion_url("https://www.notion.so/ok")
        from app.services.notion_reference import resolve_notion_page_url, notion_page_reference
        from app.services.notion_normalize import safe_page_url
        real_shape_uuid = "12345678-1234-4234-8234-123456789abc"
        expected_url = "https://www.notion.so/12345678123442348234123456789abc"
        assert resolve_notion_page_url(None, real_shape_uuid) == expected_url
        assert resolve_notion_page_url(None, "123") is None
        assert resolve_notion_page_url(None, "../unsafe") is None
        assert resolve_notion_page_url("https://evil.example/redirect", real_shape_uuid) == expected_url
        assert resolve_notion_page_url("https://www.notion.so/ok", real_shape_uuid) == "https://www.notion.so/ok"
        assert safe_page_url({"id": real_shape_uuid}) == expected_url
        page.notion_page_uuid = real_shape_uuid
        page.notion_url = None
        db.commit()
        legacy_preview = _notion_page_preview(db, user_id=int(user.id), notion_page_id=int(page.id), display_index=1)
        assert legacy_preview["open_url"] == expected_url
        assert legacy_preview["reference"]["metadata"]["open_url"] == expected_url
        assert notion_page_reference(page, method="fixture", score=1)["metadata"]["has_open_url"]
        try:
            _notion_page_preview(db, user_id=int(other.id), notion_page_id=int(page.id), display_index=1)
            raise AssertionError("cross-user URL fallback must be denied")
        except Exception as exc:
            assert getattr(exc, "code", "") == "SOURCE_REFERENCE_GONE"
        page.sync_status = "pending"
        page.indexed_content_hash = None
        db.add(page)
        db.commit()
        try:
            _notion_page_preview(db, user_id=int(user.id), notion_page_id=int(page.id), display_index=1)
            raise AssertionError("pending preview should block")
        except Exception as exc:
            assert getattr(exc, "code", "") == NOTION_SOURCE_SYNC_PENDING
        try:
            _resolve_one_member(db, int(user.id), SourceMemberIdentity(reference_key=key, display_order=0))
            raise AssertionError("pending should block")
        except SourceSetError as exc:
            assert exc.code == NOTION_SOURCE_SYNC_PENDING


def test_ui_status_has_no_secrets() -> None:
    from app.services.notion_sync import connection_status_view

    with isolated_db() as db:
        user = _user(db)
        started = start_oauth(db, int(user.id))
        handle_callback(db, code="ok", state=started["authorization_url"].split("state=")[1], error=None)
        view = connection_status_view(db, int(user.id))
        blob = json.dumps(view)
        assert "fake-access" not in blob
        assert "fake-secret" not in blob
        assert "state=" not in blob
        assert view["ui_state"] in {"syncing", "pending", "connected", "partial", "failed"}


def test_worker_attachment_priority() -> None:
    from scripts import process_attachments as worker

    source = Path(worker.__file__).read_text(encoding="utf-8")
    attach_at = source.find("claim_attachment_jobs")
    notion_at = source.find("claim_notion_jobs")
    assert attach_at > 0 and notion_at > attach_at
    assert "NOTION_BATCH_SIZE" in source
    assert "NotionJobLeaseHeartbeat" in source
    assert "worker_id=worker_id" in source
    assert "claim_check=lease.assert_owned" in source
    assert source.find("_process_jobs_batch") < source.find("_process_notion_batch")


def test_migration_034_sql_contract() -> None:
    sql = (BACKEND / "migrations" / "034_add_notion_integration.sql").read_text(encoding="utf-8")
    for token in (
        "notion_connections",
        "notion_oauth_states",
        "notion_pages",
        "notion_sync_jobs",
        "notion_page",
        "notion_text",
        "CREATE TABLE IF NOT EXISTS",
    ):
        assert token in sql
    assert "IS NULL OR" not in sql


def main() -> int:
    tests = [
        test_aes_gcm,
        test_oauth_states,
        test_fake_provider_and_sync,
        test_webhook_and_jobs,
        test_ai_identity_and_preview,
        test_ui_status_has_no_secrets,
        test_worker_attachment_priority,
        test_migration_034_sql_contract,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print(f"failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

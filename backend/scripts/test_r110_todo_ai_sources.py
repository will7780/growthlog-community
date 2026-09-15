"""GrowthLog 11.10.0 Todo AI source contracts."""
from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse, urlunparse
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.database import Base
from app.models import (
    AIConversationSourceSet, Entry, EntryLabel, KnowledgeChunk, KnowledgeEmbedding,
    KnowledgeSource, Todo, User,
)
from app.services import knowledge_rag
from app.services.ai_persona_context import build_evidence_from_manifest, evidence_to_generation_references
from app.services.ai_explainer import _safe_candidate_item
from app.services.ai_stream_protocol import (
    PURPOSE_ANSWER_REFERENCE,
    PURPOSE_CANDIDATE_CONFIRM,
    sanitize_candidate_for_stream,
    sign_reference_token,
    verify_reference_token,
)
from app.services.ai_source_set import (
    SourceManifest,
    SourceMemberIdentity,
    SourceSetError,
    _resolve_members_from_db,
    check_and_mark_stale_if_changed,
    fingerprint_manifest,
)
from app.services.ai_stream_reference_preview import _todo_preview
from app.services.evidence_text import serialize_evidence_body
from app.services.entry_family import select_answer_context_refs
from app.services.relevance_ranker import build_safe_summary
from app.services.reference_identity import canonical_reference_key, parse_proposal_reference_key
from app.services.todo_hierarchy import build_index
from app.services.todo_knowledge import (
    best_effort_sync_todo_ids,
    delete_todo_knowledge,
    search_keyword_todos,
    sync_todo_to_knowledge,
    todo_content_hash,
    todo_reference,
)

_SEQ = {"user": 0, "label": 0, "entry": 0, "todo": 0, "source": 0, "chunk": 0, "embedding": 0, "source_set": 0}


def _next(kind: str) -> int:
    _SEQ[kind] += 1
    return _SEQ[kind]


@contextmanager
def isolated_db() -> Iterator[Session]:
    for key in _SEQ:
        _SEQ[key] = 0
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            EntryLabel.__table__,
            Entry.__table__,
            Todo.__table__,
            KnowledgeSource.__table__,
            KnowledgeChunk.__table__,
            KnowledgeEmbedding.__table__,
            AIConversationSourceSet.__table__,
        ],
    )
    Local = sessionmaker(bind=engine, expire_on_commit=False)

    @event.listens_for(Local, "before_flush")
    def _assign_ids(session, _ctx, _instances):  # noqa: ANN001
        mapping = {
            User: "user",
            EntryLabel: "label",
            Entry: "entry",
            Todo: "todo",
            KnowledgeSource: "source",
            KnowledgeChunk: "chunk",
            KnowledgeEmbedding: "embedding",
            AIConversationSourceSet: "source_set",
        }
        for obj in session.new:
            kind = mapping.get(type(obj))
            if kind and getattr(obj, "id", None) is None:
                obj.id = _next(kind)

    db = Local()
    knowledge_rag._KNOWLEDGE_TABLES_CACHED = None
    try:
        yield db
    finally:
        db.rollback()
        db.close()
        engine.dispose()
        knowledge_rag._KNOWLEDGE_TABLES_CACHED = None


def _seed_tree(db: Session) -> tuple[User, Todo, Todo, Todo, Todo]:
    owner = User(username="r110_owner", password_hash="x", is_active=True, is_admin=False)
    db.add(owner)
    db.flush()
    root = Todo(
        user_id=owner.id,
        content="发布 GrowthLog 11.10",
        priority="P1",
        due_date=date(2026, 9, 1),
        is_done=False,
        is_urgent=True,
    )
    db.add(root)
    db.flush()
    child = Todo(
        user_id=owner.id,
        parent_id=root.id,
        content="验证 AI 小要事引用",
        priority="P0",
        due_date=date(2026, 8, 31),
        is_done=True,
        is_urgent=True,
        completed_at=datetime(2026, 8, 28, 9, 30),
        completion_note="检索鼠已命中子任务",
    )
    db.add(child)
    db.flush()
    grandchild = Todo(
        user_id=owner.id,
        parent_id=child.id,
        content="检查任务树预览",
        priority="P2",
        is_done=False,
        is_urgent=False,
    )
    db.add(grandchild)
    db.flush()
    great_grandchild = Todo(
        user_id=owner.id,
        parent_id=grandchild.id,
        content="三级子任务核对引用隐私",
        priority="P3",
        due_date=date(2026, 9, 3),
        is_done=False,
        is_urgent=False,
    )
    db.add(great_grandchild)
    db.commit()
    return owner, root, child, grandchild, great_grandchild


def test_migration_and_model_contract() -> None:
    sql = (BACKEND / "migrations" / "032_add_todo_knowledge_source.sql").read_text("utf-8")
    assert "ENUM('entry','attachment','memory','todo')" in sql.replace(" ", "")
    assert "knowledge_sources" in sql and "knowledge_chunks" in sql
    assert "CREATE TABLE" not in sql.upper()
    assert "todo_embeddings" not in sql
    print("PASS migration_and_model_contract")


def test_mysql_migration_idempotent_and_preserves_rows() -> None:
    from app.config import settings
    import pymysql

    url = str(settings.database_url)
    parsed = urlparse(url)
    if not parsed.scheme.startswith("mysql"):
        print("SKIP mysql_migration_idempotent_and_preserves_rows non_mysql")
        return
    if (parsed.hostname or "") not in {"127.0.0.1", "localhost", "::1"}:
        raise AssertionError("refuse_non_loopback_database")
    base_schema = parsed.path.strip("/") or "growth_log"
    schema = f"growth_log_r110_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    if not schema.startswith("growth_log_r110_"):
        raise AssertionError("unsafe isolate schema")
    admin_url = urlunparse(parsed._replace(path=f"/{base_schema}"))
    target_url = urlunparse(parsed._replace(path=f"/{schema}"))
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {schema} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        target = create_engine(
            target_url,
            connect_args={"client_flag": pymysql.constants.CLIENT.MULTI_STATEMENTS},
        )
        raw_connection = target.raw_connection()
        try:
            cursor = raw_connection.cursor()
            migration_010 = (BACKEND / "migrations" / "010_create_knowledge_rag_tables.sql").read_text("utf-8")
            cursor.execute(migration_010)
            while cursor.nextset():
                pass
            cursor.execute(
                "INSERT INTO knowledge_sources "
                "(user_id,source_type,source_id,title,status) VALUES (1,'entry',9,'legacy','indexed')"
            )
            source_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO knowledge_chunks "
                "(source_id,user_id,origin_type,origin_id,entry_id,chunk_index,chunk_type,title,content) "
                "VALUES (%s,1,'entry',9,9,0,'entry_text','legacy','legacy content')",
                (source_id,),
            )
            raw_connection.commit()
            migration_032 = (BACKEND / "migrations" / "032_add_todo_knowledge_source.sql").read_text("utf-8")
            migration_032 = re.sub(r"(?im)^\s*USE\s+[^;]+;\s*$", "", migration_032)
            for _ in range(2):
                cursor.execute(migration_032)
                while cursor.nextset():
                    pass
                raw_connection.commit()
            cursor.execute(
                "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='knowledge_sources' AND COLUMN_NAME='source_type'",
                (schema,),
            )
            source_enum = str(cursor.fetchone()[0])
            cursor.execute(
                "SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='knowledge_chunks' AND COLUMN_NAME='origin_type'",
                (schema,),
            )
            chunk_enum = str(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM knowledge_sources WHERE source_type='entry'")
            assert int(cursor.fetchone()[0]) == 1
            cursor.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE origin_type='entry'")
            assert int(cursor.fetchone()[0]) == 1
            assert "'todo'" in source_enum and "'todo'" in chunk_enum
        finally:
            raw_connection.close()
            target.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {schema}"))
        admin.dispose()
    print("PASS mysql_migration_idempotent_and_preserves_rows")

def test_hash_path_exclusion_and_canonical_identity() -> None:
    a = Todo(
        id=7,
        user_id=1,
        parent_id=None,
        content="同一个任务",
        priority="P2",
        due_date=date(2026, 9, 2),
        is_done=False,
        is_urgent=True,
    )
    b = Todo(
        id=7,
        user_id=1,
        parent_id=99,
        content="同一个任务",
        priority="P2",
        due_date=date(2026, 9, 2),
        is_done=False,
        is_urgent=True,
    )
    assert todo_content_hash(a) != todo_content_hash(b)
    c = Todo(
        id=7,
        user_id=1,
        parent_id=100,
        content="同一个任务",
        priority="P0",
        due_date=None,
        is_done=False,
        is_urgent=False,
    )
    assert todo_content_hash(b) == todo_content_hash(c)
    c.content = "正文改变"
    assert todo_content_hash(b) != todo_content_hash(c)
    assert canonical_reference_key({"source_type": "todo", "source_id": 7}) == "todo:source:7"
    assert parse_proposal_reference_key("todo:source:7") == ("todo", "source", 7)
    assert parse_proposal_reference_key("todo:chunk:7") is None
    assert canonical_reference_key({"source_type": "entry", "source_id": 7}) == "entry:source:7"
    print("PASS hash_path_exclusion_and_canonical_identity")


def test_mirror_keyword_stale_dense_and_delete() -> None:
    with isolated_db() as db:
        owner, root, child, grandchild, great_grandchild = _seed_tree(db)
        all_todos = [root, child, grandchild, great_grandchild]
        with patch("app.services.todo_knowledge.generate_embedding", return_value=[1.0, 0.0, 0.0, 0.0]):
            for todo in all_todos:
                assert sync_todo_to_knowledge(db, todo)["ok"] is True

        assert db.query(KnowledgeSource).filter(KnowledgeSource.source_type == "todo").count() == 4
        assert db.query(KnowledgeChunk).filter(KnowledgeChunk.origin_type == "todo").count() == 4
        assert db.query(KnowledgeEmbedding).count() == 4

        lexical = search_keyword_todos(
            db,
            user_id=owner.id,
            terms=["检索鼠", "完成批注", "三级子任务"],
            top_k=20,
        )
        assert lexical and lexical[0]["source_type"] == "todo"
        assert any(item.get("todo_id") == great_grandchild.id for item in lexical)

        dense = knowledge_rag.search_knowledge_chunks(
            db,
            user_id=owner.id,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            top_k=20,
            min_score=0.1,
        )
        assert any(item.get("todo_id") == child.id for item in dense)
        assert any(item.get("todo_id") == great_grandchild.id for item in dense)

        child.content = "状态已改变但镜像尚未同步"
        db.commit()
        with patch(
            "app.services.todo_knowledge.generate_embedding",
            side_effect=RuntimeError("provider unavailable"),
        ):
            sync_result = best_effort_sync_todo_ids(
                db, user_id=owner.id, todo_ids=[child.id]
            )
        assert sync_result["failed"] == 1
        lexical_after_failure = search_keyword_todos(
            db, user_id=owner.id, terms=["状态已改变"], top_k=20
        )
        assert any(item.get("todo_id") == child.id for item in lexical_after_failure)
        stale_dense = knowledge_rag.search_knowledge_chunks(
            db,
            user_id=owner.id,
            query_vector=[1.0, 0.0, 0.0, 0.0],
            top_k=20,
            min_score=0.1,
        )
        assert not any(item.get("todo_id") == child.id for item in stale_dense)

        deleted = delete_todo_knowledge(
            db, user_id=owner.id, todo_ids=[int(todo.id) for todo in all_todos]
        )
        assert deleted == 4
        db.flush()
        db.delete(root)
        db.commit()
        assert db.query(KnowledgeSource).filter(KnowledgeSource.source_type == "todo").count() == 0
        assert db.query(KnowledgeChunk).filter(KnowledgeChunk.origin_type == "todo").count() == 0
        assert db.query(KnowledgeEmbedding).count() == 0
    print("PASS mirror_keyword_stale_dense_and_delete")


def test_source_set_live_state_and_deleted_stale() -> None:
    with isolated_db() as db:
        owner, root, child, _grandchild, great_grandchild = _seed_tree(db)
        members = _resolve_members_from_db(
            db,
            owner.id,
            [SourceMemberIdentity(reference_key=f"todo:source:{child.id}", display_order=0)],
        )
        assert len(members) == 1
        member = members[0]
        assert member.source_type == "todo"
        assert member.content_hash is None
        manifest = SourceManifest(members=members)

        first, first_truncated = build_evidence_from_manifest(db, owner.id, manifest)
        assert first_truncated is False
        assert "已完成" in first[0].text
        assert "检索鼠已命中子任务" in first[0].text
        assert "发布 GrowthLog 11.10" in first[0].text

        child.is_done = False
        child.completed_at = None
        child.completion_note = None
        child.priority = "P3"
        db.commit()
        second, second_truncated = build_evidence_from_manifest(db, owner.id, manifest)
        assert second_truncated is False
        assert "未完成" in second[0].text and "优先级：" not in second[0].text
        refs = evidence_to_generation_references(second)
        assert refs[0]["source_type"] == "todo"

        db.delete(child)
        db.commit()
        try:
            build_evidence_from_manifest(db, owner.id, manifest)
        except SourceSetError as exc:
            assert exc.code == "SOURCE_SET_STALE"
        else:
            raise AssertionError("deleted Todo must stale the locked source set")
    print("PASS source_set_live_state_and_deleted_stale")


def test_tree_context_and_readonly_preview_privacy() -> None:
    with isolated_db() as db:
        owner, root, child, grandchild, great_grandchild = _seed_tree(db)
        index = build_index([root, child, grandchild, great_grandchild])
        ref = todo_reference(child, index, method="test", score=0.9)
        body = serialize_evidence_body(ref)
        assert "小要事 路径" in body
        assert "发布 GrowthLog 11.10" in body
        preview = _todo_preview(
            db,
            user_id=owner.id,
            todo_id=child.id,
            display_index=1,
        )
        encoded = json.dumps(preview, ensure_ascii=False)
        assert preview["source_type"] == "todo"
        assert len(preview["todo_tree"]) == 4
        assert max(node["depth"] for node in preview["todo_tree"]) == 3
        assert any(node["is_hit"] for node in preview["todo_tree"])
        for forbidden in ("todo_id", "source_id", "reference_key", "chunk_id", "knowledge_source_id"):
            assert forbidden not in encoded
        assert "edit" not in encoded.lower() and "delete" not in encoded.lower()
    print("PASS tree_context_and_readonly_preview_privacy")


def test_candidate_stream_token_privacy_and_isolation() -> None:
    candidate = _safe_candidate_item(
        reference_key="todo:source:7",
        source_type="todo",
        title="核对线上发布",
        snippet="发布计划 / 核对线上发布",
        family_root_entry_id=None,
        display_order=0,
    )
    assert "family_root_todo_id" not in candidate
    public = sanitize_candidate_for_stream(
        candidate,
        user_id=11,
        session_id="r110-session",
        display_index=1,
    )
    encoded = json.dumps(public, ensure_ascii=False)
    for forbidden in ("todo_id", "source_id", "reference_key", "family_root_todo_id", "todo:source:7"):
        assert forbidden not in encoded
    assert public["source_type"] == "todo"
    from app.routes.ai import _public_explainer_candidates

    nonstream = _public_explainer_candidates(
        [candidate], user_id=11, session_id="r110-session"
    )[0].model_dump(exclude_none=True)
    nonstream_json = json.dumps(nonstream, ensure_ascii=False)
    assert "ref_token" in nonstream
    for forbidden in ("todo_id", "source_id", "reference_key", "family_root_todo_id", "todo:source:7"):
        assert forbidden not in nonstream_json
    token = str(public["ref_token"])
    payload = verify_reference_token(
        token,
        user_id=11,
        session_id="r110-session",
        purpose=PURPOSE_CANDIDATE_CONFIRM,
    )
    assert payload is not None and payload["reference_key"] == "todo:source:7"
    assert verify_reference_token(
        token,
        user_id=12,
        session_id="r110-session",
        purpose=PURPOSE_CANDIDATE_CONFIRM,
    ) is None
    assert verify_reference_token(
        token,
        user_id=11,
        session_id="other-session",
        purpose=PURPOSE_CANDIDATE_CONFIRM,
    ) is None
    assert verify_reference_token(
        token,
        user_id=11,
        session_id="r110-session",
        purpose=PURPOSE_ANSWER_REFERENCE,
    ) is None
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert verify_reference_token(
        tampered,
        user_id=11,
        session_id="r110-session",
        purpose=PURPOSE_CANDIDATE_CONFIRM,
    ) is None

    entry_token = sign_reference_token(
        user_id=11,
        session_id="r110-session",
        display_index=2,
        purpose=PURPOSE_CANDIDATE_CONFIRM,
        reference_key="entry:source:7",
    )
    entry_payload = verify_reference_token(
        entry_token,
        user_id=11,
        session_id="r110-session",
        purpose=PURPOSE_CANDIDATE_CONFIRM,
    )
    assert entry_payload is not None
    assert entry_payload["reference_key"] != payload["reference_key"]

    from types import SimpleNamespace
    from app.routes.ai import SourceSetConfirmRequest, confirm_session_source_set

    manifest_payload = {
        "members": [{
            "source_type": "todo",
            "source_id": 7,
            "display_order": 0,
            "todo_id": 7,
            "family_root_todo_id": 7,
            "content_hash": None,
        }]
    }
    confirmed = SimpleNamespace(
        version=1,
        content_fingerprint="f" * 64,
        status="locked",
        source_manifest=manifest_payload,
    )
    request = SourceSetConfirmRequest(
        proposal_id=3,
        base_version=0,
        selected_ref_tokens=[token],
    )
    with patch("app.routes.ai.confirm_proposal", return_value=confirmed) as confirm_mock:
        response = confirm_session_source_set(
            "r110-session",
            request,
            current_user=SimpleNamespace(id=11),
            db=MagicMock(),
        )
    assert response.version == 1 and response.member_count == 1
    assert confirm_mock.call_args.kwargs["selected_reference_keys"] == ["todo:source:7"]
    print("PASS candidate_stream_token_privacy_and_isolation")

def test_locked_source_set_update_live_delete_stale() -> None:
    with isolated_db() as db:
        owner, _root, _child, _grandchild, leaf = _seed_tree(db)
        members = _resolve_members_from_db(
            db,
            owner.id,
            [SourceMemberIdentity(reference_key=f"todo:source:{leaf.id}", display_order=0)],
        )
        manifest = SourceManifest(members=members)
        row = AIConversationSourceSet(
            user_id=owner.id,
            session_id="r110-stale",
            version=1,
            base_version=0,
            status="locked",
            source_manifest=manifest.model_dump(mode="json"),
            content_fingerprint=fingerprint_manifest(manifest),
        )
        db.add(row)
        db.commit()

        leaf.content = "更新后的三级子任务"
        leaf.priority = "P0"
        leaf.is_urgent = True
        leaf.is_done = True
        leaf.completed_at = datetime(2026, 8, 28, 12, 0, 0)
        leaf.completion_note = "最新完成批注"
        db.commit()
        assert check_and_mark_stale_if_changed(db, owner.id, "r110-stale", 1) is False
        assert row.status == "locked"

        db.delete(leaf)
        db.commit()
        assert check_and_mark_stale_if_changed(db, owner.id, "r110-stale", 1) is True
        assert row.status == "stale"
    print("PASS locked_source_set_update_live_delete_stale")


def test_todo_lexical_survives_entry_lexical_failure() -> None:
    from app.services.lookup_all_candidates import build_lookup_candidate_pool

    with isolated_db() as db:
        owner, root, child, grandchild, great_grandchild = _seed_tree(db)
        index = build_index([root, child, grandchild, great_grandchild])
        todo_hit = todo_reference(child, index, method="keyword", score=0.91)
        with (
            patch("app.services.lookup_all_candidates.initialize_user_index", return_value=None),
            patch("app.services.lookup_all_candidates.lexical_union_entries", side_effect=RuntimeError("entry lexical down")),
            patch("app.services.lookup_all_candidates.search_keyword_todos", return_value=[todo_hit]),
        ):
            result = build_lookup_candidate_pool(
                db,
                user_id=owner.id,
                query="检索鼠子任务",
                topic_terms=["检索鼠", "子任务"],
                candidate_cap=20,
                include_attachments=False,
                generate_query_embedding=False,
            )
        assert any(item.get("source_type") == "todo" for item in result["candidates"])
        assert str(result["channel_status"]["lexical_union"]).startswith("degraded:")
        assert result["channel_status"]["todo_lexical"] == "ok"
    print("PASS todo_lexical_survives_entry_lexical_failure")

def test_safe_ranker_summary_and_hit_ancestor_budget() -> None:
    summary = build_safe_summary(
        {
            "source_type": "todo",
            "source_id": 99,
            "title": "核对发布公告",
            "snippet": "状态：未完成；todo_id=99",
            "metadata": {
                "path_titles": ["发布验收主任务", "核对发布公告"],
                "status": "open",
                "priority": "P0",
                "urgent": True,
                "due_date": "2026-08-29",
                "completed_at": None,
            },
            "judge_evidence": {
                "hit_window": "状态：未完成；todo_id=99",
                "title_term_hit": True,
            },
        },
        alias="S1",
        hybrid_rank=1,
    )
    assert summary["source_kind"] == "todo"
    assert summary["task_path"] == ["发布验收主任务", "核对发布公告"]
    assert summary["task_status"] == "open"
    assert summary["task_priority"] == "P0"
    assert summary["task_urgent"] is True
    safe_blob = json.dumps(summary, ensure_ascii=False)
    assert "todo_id" not in safe_blob and "99" not in safe_blob

    def todo_ref(source_id: int, content: str, path: list[str]) -> dict:
        return {
            "source_type": "todo",
            "source_id": source_id,
            "todo_id": source_id,
            "root_todo_id": 1,
            "title": content,
            "content": content,
            "snippet": content,
            "metadata": {"path_titles": path},
        }

    root = todo_ref(1, "根任务", ["根任务"])
    noise = [
        todo_ref(100 + idx, f"前置兄弟 {idx}", ["根任务", f"前置兄弟 {idx}"])
        for idx in range(25)
    ]
    parent = todo_ref(2, "目标父任务", ["根任务", "目标父任务"])
    leaf = todo_ref(3, "目标子任务", ["根任务", "目标父任务", "目标子任务"])
    selected = select_answer_context_refs(
        [root, *noise, parent, leaf],
        [{**leaf, "relevance_level": 4}],
        max_refs=3,
        max_chars=2000,
    )
    assert [int(item["source_id"]) for item in selected] == [1, 2, 3]
    print("PASS safe_ranker_summary_and_hit_ancestor_budget")


def test_write_hook_and_frontend_contracts() -> None:
    todos_route = (BACKEND / "app" / "routes" / "todos.py").read_text("utf-8")
    pending = (BACKEND / "app" / "services" / "agent_pending_actions.py").read_text("utf-8")
    organizer = (ROOT / "frontend" / "src" / "components" / "ai" / "OrganizeControls.tsx").read_text("utf-8")
    assert todos_route.count("best_effort_sync_todo_ids") >= 4
    assert todos_route.count("delete_todo_knowledge") >= 3
    assert "_sync_todo_knowledge_after_pending" in pending
    assert "TodoReferencePreview" in (ROOT / "frontend" / "src" / "components" / "ai" / "StreamReferencePreview.tsx").read_text("utf-8")
    ai_route = (BACKEND / "app" / "routes" / "ai.py").read_text("utf-8")
    assert "parse_proposal_reference_key(reference_key)" in ai_route
    assert "_public_explainer_candidates" in ai_route
    assert "todo" not in organizer.lower(), "organizer scope must remain record-only"
    print("PASS write_hook_and_frontend_contracts")


def main() -> None:
    test_migration_and_model_contract()
    test_mysql_migration_idempotent_and_preserves_rows()
    test_hash_path_exclusion_and_canonical_identity()
    test_mirror_keyword_stale_dense_and_delete()
    test_source_set_live_state_and_deleted_stale()
    test_locked_source_set_update_live_delete_stale()
    test_todo_lexical_survives_entry_lexical_failure()
    test_tree_context_and_readonly_preview_privacy()
    test_candidate_stream_token_privacy_and_isolation()
    test_safe_ranker_summary_and_hit_ancestor_budget()
    test_write_hook_and_frontend_contracts()
    print("R110_TODO_AI_SOURCES_ALL_PASS")


if __name__ == "__main__":
    main()
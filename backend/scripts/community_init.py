"""Initialize an EMPTY MySQL database. Never an upgrade or production migration."""
import argparse
import getpass
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def initialize():
    import app.models
    from app.database import Base, engine, SessionLocal
    from sqlalchemy import inspect, text
    from app.models import EntryLabel
    with engine.connect() as conn:
        if not conn.execute(text("SELECT GET_LOCK('growthlog_community_init', 0)")).scalar():
            raise RuntimeError("INITIALIZATION_BUSY")
        try:
            if inspect(conn).get_table_names():
                raise RuntimeError("DATABASE_NOT_EMPTY")
            Base.metadata.create_all(conn)
            migration = Path(__file__).resolve().parents[1] / "migrations/029_add_ai_stream_request_id.sql"
            # Non-ORM durable claim table is taken from the authoritative migration.
            claim_sql = migration.with_name("029_add_ai_stream_request_id.sql")
            candidates = list(migration.parent.glob("*.sql"))
            ddl = None
            for item in candidates:
                matches = re.findall(r"CREATE TABLE IF NOT EXISTS [`]?ai_stream_request_claims[`]?\s*\(.*?;", item.read_text(encoding="utf-8"), re.S | re.I)
                if matches:
                    ddl = matches[0]
                    break
            if not ddl:
                raise RuntimeError("CLAIM_SCHEMA_MISSING")
            # ORM identifiers are signed BIGINT; the legacy DDL used UNSIGNED.
            conn.execute(text(ddl.replace("BIGINT UNSIGNED", "BIGINT")))
            conn.execute(text("ALTER TABLE ai_conversations ADD FULLTEXT INDEX ft_ai_conversations_content (content)"))
            conn.execute(text("CREATE INDEX idx_todos_user_parent_sort ON todos (user_id,parent_id,sort_order,id)"))
            conn.commit()
        finally:
            conn.execute(text("SELECT RELEASE_LOCK('growthlog_community_init')"))
    with SessionLocal() as db:
        for pos, (code, name) in enumerate([("idea","小巧思"),("gain","小收获"),("knowledge","小知识"),("mood","小心情"),("todo","小要事")]):
            db.add(EntryLabel(code=code, name=name, sort_order=pos, user_id=None, is_active=True))
        db.commit()
    print("COMMUNITY_EMPTY_DATABASE_INITIALIZED")

def create_admin():
    from app.database import SessionLocal
    from app.models import User
    from app.auth import get_password_hash
    with SessionLocal() as db:
        if db.query(User).count():
            raise RuntimeError("FIRST_ADMIN_REQUIRES_NO_USERS")
        username = input("Administrator username: ").strip()
        if not 3 <= len(username) <= 64:
            raise RuntimeError("USERNAME_LENGTH_3_TO_64")
        password = getpass.getpass("Password (at least 12 characters): ")
        if len(password) < 12 or len(password.encode("utf-8")) > 72:
            raise RuntimeError("PASSWORD_LENGTH_INVALID")
        if password != getpass.getpass("Repeat password: "):
            raise RuntimeError("PASSWORD_MISMATCH")
        db.add(User(username=username, password_hash=get_password_hash(password), is_active=True, is_admin=True, can_edit_delete_own_entries=True))
        db.commit()
    print("FIRST_ADMIN_CREATED")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "create-admin"])
    args = parser.parse_args()
    try:
        initialize() if args.action == "init" else create_admin()
    except Exception as exc:
        code = getattr(getattr(exc, "orig", None), "args", [None])[0]
        print("COMMUNITY_SETUP_FAILED:" + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__) + (":" + str(code) if isinstance(code, int) else ""))
        raise SystemExit(1)

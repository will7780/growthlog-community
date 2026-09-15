"""Synthetic isolation regression for label list and cache boundaries."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DATABASE_URL", "mysql+pymysql://test:test@127.0.0.1/unused")
os.environ.setdefault("JWT_SECRET", "synthetic-label-contract-only")
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.models import User, EntryLabel
from app.database import get_db
from app.auth import get_current_user
from app.routes import labels

def main():
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    User.__table__.create(engine)
    EntryLabel.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([User(id=i,username=f"synthetic_{i}",password_hash="unused",is_active=True) for i in (1,2)])
        db.add_all([
            EntryLabel(id=1,code="system",name="Shared",sort_order=1,user_id=None,is_active=True),
            EntryLabel(id=2,code="private_a",name="Only A",sort_order=2,user_id=1,is_active=True),
            EntryLabel(id=3,code="private_b",name="Only B",sort_order=3,user_id=2,is_active=True),
            EntryLabel(id=4,code="inactive",name="Hidden",sort_order=4,user_id=1,is_active=False),
        ])
        db.commit()
    app=FastAPI()
    app.include_router(labels.router,prefix="/api/labels")
    def session():
        with Session(engine) as db: yield db
    def user(x_test_user: int=Header()):
        return User(id=x_test_user,username="synthetic",is_active=True)
    app.dependency_overrides[get_db]=session
    app.dependency_overrides[get_current_user]=user
    labels._invalidate_cache()
    try:
        with TestClient(app) as client:
            for uid in (1,2,2,1):
                r=client.get("/api/labels",headers={"x-test-user":str(uid)})
                assert r.status_code==200
                expected={"Shared","Only A" if uid==1 else "Only B"}
                assert {x["name"] for x in r.json()}==expected,"LABEL_OWNERSHIP_OR_CACHE_LEAK"
        print("LABEL_ISOLATION_CONTRACT_OK")
    finally:
        labels._invalidate_cache()
        engine.dispose()
if __name__=="__main__":main()

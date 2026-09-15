"""Real HTTP, synthetic-only smoke against the dedicated community Compose stack."""
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main():
    if os.environ.get("COMMUNITY_SMOKE") != "1":
        raise SystemExit("Explicit isolated COMMUNITY_SMOKE=1 required")
    import httpx
    from sqlalchemy.engine import make_url
    from app.config import settings
    if make_url(settings.database_url).host != "db":
        raise SystemExit("Dedicated Compose database required")
    from app.database import SessionLocal
    from app.models import User, EntryLabel, Todo, KnowledgeSource
    from app.auth import get_password_hash
    suffix=uuid.uuid4().hex[:10]
    password=secrets.token_urlsafe(24)
    users=[]
    with SessionLocal() as db:
        for role in ("a","b"):
            user=User(username="community_test_"+role+"_"+suffix,password_hash=get_password_hash(password),is_active=True,is_admin=False,can_edit_delete_own_entries=True)
            db.add(user);db.flush();users.append((user.id,user.username))
        db.add(EntryLabel(code="community_private_"+suffix,name="Synthetic private label",user_id=users[0][0],sort_order=9,is_active=True))
        db.commit()
    base="http://app:8000"
    try:
        with httpx.Client(base_url=base,timeout=180) as client:
            tokens=[]
            for _,name in users:
                r=client.post("/api/auth/login",json={"username":name,"password":password})
                assert r.status_code==200, "login"
                tokens.append({"Authorization":"Bearer "+r.json()["access_token"]})
            for index in (0,1,0,1):
                r=client.get("/api/labels",headers=tokens[index])
                assert r.status_code==200
                names={x["name"] for x in r.json()}
                assert ("Synthetic private label" in names)==(index==0),"label isolation/cache"
            root=client.post("/api/todos",headers=tokens[0],json={"content":"Community synthetic research","priority":"P1"})
            assert root.status_code==201,"root create"
            ids=[root.json()["id"]]
            for depth in range(3):
                child=client.post(f"/api/todos/{ids[-1]}/children",headers=tokens[0],json={"content":f"Community synthetic step {depth}"})
                assert child.status_code==201,"child depth"
                ids.append(child.json()["id"])
            rejected=client.post(f"/api/todos/{ids[-1]}/children",headers=tokens[0],json={"content":"Too deep"})
            assert rejected.status_code in (400,409,422),"depth limit"
            assert client.patch(f"/api/todos/{ids[0]}",headers=tokens[0],json={"is_done":True,"completion_note":"done"}).status_code==409
            assert client.post(f"/api/todos/{ids[0]}/children",headers=tokens[1],json={"content":"forbidden"}).status_code==404
            assert client.post(f"/api/todos/{ids[-1]}/today-plan",headers=tokens[0],json={}).status_code in (200,201)
            for task in reversed(ids):
                r=client.patch(f"/api/todos/{task}",headers=tokens[0],json={"is_done":True,"completion_note":"Synthetic completion"})
                assert r.status_code==200,"complete children first"
            r=client.patch(f"/api/todos/{ids[-1]}",headers=tokens[0],json={"is_done":False})
            assert r.status_code==200
            tree=client.get("/api/todos/tree",headers=tokens[0]).json()["items"]
            assert not next(x for x in tree if x["id"]==ids[0])["is_done"],"ancestor reopen"
            e=client.post("/api/entries",headers=tokens[0],json={"label_code":"knowledge","content":"Synthetic notebook: recovery after exercise uses rest and gradual training."})
            assert e.status_code==201,"entry"
            eid=e.json()["id"]
            assert client.get(f"/api/entries/{eid}",headers=tokens[1]).status_code==404
            assert client.get("/api/todos/tree").status_code==401
            assert client.delete(f"/api/todos/{ids[0]}?expected_descendant_count=3",headers=tokens[0]).status_code==204
            assert client.delete(f"/api/entries/{eid}",headers=tokens[0]).status_code==204
        with SessionLocal() as db:
            assert db.query(Todo).filter(Todo.user_id==users[0][0]).count()==0
        print("COMMUNITY_HTTP_SMOKE_OK")
    finally:
        with SessionLocal() as db:
            for uid,_ in users:
                user=db.get(User,uid)
                if user: db.delete(user)
            db.commit()
            assert db.query(User).filter(User.id.in_([x[0] for x in users])).count()==0
            assert db.query(KnowledgeSource).filter(KnowledgeSource.user_id.in_([x[0] for x in users])).count()==0
        print("COMMUNITY_SYNTHETIC_CLEANUP_OK")

if __name__=="__main__":
    main()

"""Public-source and image boundary checks. Reports paths, never matching values."""
import argparse
import re
import subprocess
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="store_true")
    args = parser.parse_args()
    root = Path("/app") if args.image else Path(__file__).resolve().parents[1]
    if args.image:
        paths = [p for p in root.rglob("*") if p.is_file()]
    else:
        names = subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"],cwd=root).decode().split("\0")
        paths = [root/n for n in names if n]
    patterns = {
        "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "provider_token": re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
        "github_token": re.compile(rb"(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
        "notion_token": re.compile(rb"\b(?:ntn_|secret_)[A-Za-z0-9]{30,}\b"),
        "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "private_machine": re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+(?!example)"),
    }
    errors=[]; scanned=0
    forbidden={"uploads","vector_indexes","logs","test_artifacts","backups",".deploy"}
    for path in paths:
        if not path.is_file(): continue
        rel=path.relative_to(root)
        if "__pycache__" in rel.parts: continue
        if set(rel.parts)&forbidden or (path.name.startswith(".env") and path.name!=".env.example") or path.suffix in {".pem",".key",".db",".sqlite",".gz"}:
            errors.append(str(rel)+":private_path"); continue
        raw=path.read_bytes();scanned+=1
        for name,pattern in patterns.items():
            if pattern.search(raw): errors.append(str(rel)+":"+name)
    if errors:
        print("\n".join(sorted(set(errors))));raise SystemExit("COMMUNITY_PRIVACY_FAILED")
    print(f"COMMUNITY_PRIVACY_OK files={scanned}")
if __name__=="__main__": main()

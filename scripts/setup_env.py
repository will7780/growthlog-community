"""Create local credentials without displaying them or overwriting an environment."""
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
if target.exists():
    raise SystemExit(".env already exists; edit it locally instead.")
source = (root / ".env.example").read_text(encoding="utf-8")
for key in ("MYSQL_ROOT_PASSWORD", "MYSQL_PASSWORD", "JWT_SECRET"):
    source = source.replace(key + "=\n", key + "=" + secrets.token_hex(32) + "\n", 1)
with target.open("x", encoding="utf-8") as stream:
    stream.write(source)
target.chmod(0o600)
print("Created .env with fresh local credentials. No credentials were printed.")

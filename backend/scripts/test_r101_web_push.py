#!/usr/bin/env python3
"""R10.1 / R10.1-Fix focused unit gates for Web Push (no primary growth_log writes)."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))

os.environ["WEB_PUSH_USE_FAKE"] = "true"
os.environ["WEB_PUSH_ENABLED"] = "true"
os.environ["WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
os.environ["NOTIFICATION_PUBLIC_BASE_URL"] = "https://example.com"

from app.config import settings

settings.web_push_use_fake = True
settings.web_push_enabled = True
settings.web_push_subscription_encryption_key = os.environ["WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY"]
settings.notification_public_base_url = "https://example.com"
settings.web_push_vapid_public_key = None
settings.web_push_vapid_private_key = None

from app.services import notification_service as ns
from app.services import web_push_crypto as crypto
from app.services import web_push_endpoint as ep
from app.services import web_push_provider as provider
from app.services import web_push_service as wps

PASSES: list[str] = []
FAILS: list[str] = []


def _pass(name: str) -> None:
    PASSES.append(name)
    print(f"PASS {name}")


def _fail(name: str, detail: str = "") -> None:
    FAILS.append(name)
    print(f"FAIL {name} {detail}".strip())


def _gen_vapid_pair() -> tuple[str, str]:
    from py_vapid import Vapid
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    vapid = Vapid()
    vapid.generate_keys()
    pub = vapid.public_key.public_bytes(encoding=Encoding.X962, format=PublicFormat.UncompressedPoint)
    priv = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    return (
        base64.urlsafe_b64encode(pub).decode().rstrip("="),
        base64.urlsafe_b64encode(priv).decode().rstrip("="),
    )


def test_subscription_crypto_no_plaintext() -> None:
    sub = {
        "endpoint": "https://push.example.test/device-A",
        "keys": {"p256dh": "p256dh_value_abcdef", "auth": "auth_value_abcdef"},
    }
    blob = crypto.encrypt_subscription(sub)
    assert crypto.decrypt_subscription(blob)["endpoint"] == sub["endpoint"]
    assert sub["endpoint"] not in blob.decode("latin1", errors="ignore")
    _pass("subscription_crypto")


def test_ssrf_endpoint_allowlist() -> None:
    settings.web_push_use_fake = True
    # Fake mode: only fixed test host
    assert ep.validate_push_endpoint("https://push.example.test/v1/x").startswith("https://")
    for bad in (
        "https://web.push.apple.com/x",  # real provider blocked in fake mode
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://10.0.0.1/x",
        "https://[::1]/x",
        "https://user:pass@push.example.test/x",
        "http://push.example.test/x",
        "https://push.example.test:8443/x",
        "https://evilpush.apple.com/x",
        "https://not-web.push.apple.com/x",
    ):
        try:
            ep.validate_push_endpoint(bad)
            raise AssertionError(bad)
        except ValueError:
            pass

    settings.web_push_use_fake = False
    settings.web_push_allowed_host_suffixes = None
    assert ep.host_matches_suffix("web.push.apple.com", "web.push.apple.com")
    assert ep.host_matches_suffix("cdn.web.push.apple.com", "web.push.apple.com")
    assert not ep.host_matches_suffix("evilweb.push.apple.com", "web.push.apple.com")
    assert not ep.host_matches_suffix("apple.com.evil.example", "apple.com")
    for good in (
        "https://web.push.apple.com/push",
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
    ):
        ep.validate_push_endpoint(good)
    for bad in (
        "https://169.254.169.254/latest",
        "https://192.168.1.1/x",
        "https://evil.com/x",
        "https://push.example.test/x",  # fake host not allowed outside fake mode
    ):
        try:
            ep.validate_push_endpoint(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    settings.web_push_use_fake = True
    _pass("ssrf_endpoint_allowlist")


def test_invalid_ports_stable_code() -> None:
    settings.web_push_use_fake = True
    for bad in (
        "https://push.example.test:abc/x",
        "https://push.example.test:99999/x",
        "https://push.example.test:444/x",
    ):
        try:
            ep.validate_push_endpoint(bad)
            raise AssertionError(bad)
        except ValueError as exc:
            assert str(exc) == "ENDPOINT_PORT_FORBIDDEN"
            # Must not leak parser text or user input fragments into the code.
            assert "abc" not in str(exc)
            assert "99999" not in str(exc)
            assert "invalid" not in str(exc).lower()
            assert "could not be cast" not in str(exc).lower()
    # Also via service layer → NotificationServiceError.code
    for bad in (
        "https://push.example.test:abc/x",
        "https://push.example.test:99999/x",
        "https://push.example.test:444/x",
    ):
        try:
            wps.validate_subscription_payload(
                {
                    "endpoint": bad,
                    "keys": {"p256dh": "x" * 20, "auth": "y" * 20},
                }
            )
            raise AssertionError(bad)
        except ns.NotificationServiceError as exc:
            assert exc.code == "ENDPOINT_PORT_FORBIDDEN"
            assert "abc" not in exc.code
            assert ":" not in exc.code
    _pass("invalid_ports_stable_code")


def test_validate_subscription_uses_allowlist() -> None:
    settings.web_push_use_fake = True
    good = {
        "endpoint": "https://push.example.test/fcm/abc",
        "expirationTime": None,
        "keys": {"p256dh": "x" * 20, "auth": "y" * 20},
    }
    assert wps.validate_subscription_payload(good)["endpoint"].startswith("https://")
    try:
        wps.validate_subscription_payload({**good, "endpoint": "https://127.0.0.1/x"})
        raise AssertionError("ssrf")
    except ns.NotificationServiceError as exc:
        assert exc.code == "ENDPOINT_HOST_FORBIDDEN"
    try:
        wps.validate_subscription_payload({**good, "expirationTime": "123"})
        raise AssertionError("exp")
    except ns.NotificationServiceError as exc:
        assert exc.code == "INVALID_EXPIRATION_TIME"
    _pass("validate_subscription_ssrf")


def test_vapid_invalid_and_mismatch() -> None:
    pub, priv = _gen_vapid_pair()
    pub2, priv2 = _gen_vapid_pair()
    settings.web_push_use_fake = False
    settings.web_push_enabled = True
    settings.web_push_vapid_public_key = pub
    settings.web_push_vapid_private_key = priv
    assert provider.vapid_keys_valid() is True
    assert provider.web_push_ready() is True

    # Length-enough but invalid uncompressed point (does not start with 0x04)
    settings.web_push_vapid_public_key = base64.urlsafe_b64encode(b"\x05" + os.urandom(64)).decode().rstrip("=")
    settings.web_push_vapid_private_key = priv
    assert provider.vapid_keys_valid() is False
    assert provider.web_push_ready() is False
    view = wps.public_key_view()
    assert view["public_key"] == ""
    assert view["ready"] is False

    # Mismatched pair
    settings.web_push_vapid_public_key = pub
    settings.web_push_vapid_private_key = priv2
    assert provider.vapid_keys_valid() is False
    view2 = wps.public_key_view()
    assert view2["public_key"] == ""

    # Restore fake mode for other tests
    settings.web_push_use_fake = True
    settings.web_push_vapid_public_key = None
    settings.web_push_vapid_private_key = None
    _pass("vapid_invalid_mismatch")


def test_public_key_only_when_ready() -> None:
    pub, priv = _gen_vapid_pair()
    settings.web_push_use_fake = False
    settings.web_push_enabled = True
    settings.web_push_vapid_public_key = pub
    settings.web_push_vapid_private_key = priv
    view = wps.public_key_view()
    assert view["ready"] is True and view["public_key"] == pub
    settings.web_push_enabled = False
    settings.web_push_use_fake = False
    view2 = wps.public_key_view()
    assert view2["public_key"] == "" and view2["enabled"] is False
    settings.web_push_use_fake = True
    settings.web_push_enabled = True
    settings.web_push_vapid_public_key = None
    settings.web_push_vapid_private_key = None
    # Fake without valid VAPID: ready may be true via crypto, but no usable public_key.
    view3 = wps.public_key_view()
    assert view3["public_key"] == ""
    _pass("public_key_only_when_ready")


def test_sw_url_behavior() -> None:
    safety = REPO / "frontend" / "public" / "sw-url-safety.js"
    runner = BACKEND / "scripts" / "_sw_url_behavior_runner.mjs"
    runner.write_text(
        """
import fs from 'fs';
import vm from 'vm';

const file = process.argv[2];
const code = fs.readFileSync(file, 'utf8');
const sandbox = {
  module: { exports: {} },
  exports: {},
  console,
  URL,
  String,
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const mod = sandbox.module.exports;
const origin = 'https://example.com';
const def = origin + '/app?tab=todos&review=1';
const cases = [
  ['/\\\\evil.com', def],
  ['//evil.com', def],
  ['https://evil.com', def],
  ['http://evil.com/app', def],
  ['/api/notifications/settings', def],
  ['/app?tab=todos&review=1', origin + '/app?tab=todos&review=1'],
  ['/app', origin + '/app'],
  ['/login', origin + '/login'],
  ['/profile', origin + '/profile'],
  ['/admin', origin + '/admin'],
  ['/not-allowed', def],
];
let failed = 0;
for (const [raw, expect] of cases) {
  const got = mod.resolveSafeAppUrl(raw, origin);
  if (got !== expect) {
    console.error('CASE_FAIL', JSON.stringify(raw), 'got', got, 'expect', expect);
    failed += 1;
  }
}
if (failed) process.exit(1);
console.log('SW_URL_BEHAVIOR_OK', cases.length);
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["node", str(runner), str(safety)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    try:
        runner.unlink(missing_ok=True)
    except Exception:
        pass
    if proc.returncode != 0:
        _fail("sw_url_behavior", (proc.stderr or proc.stdout)[:300])
        return
    _pass("sw_url_behavior")


def test_service_worker_no_fetch() -> None:
    sw = (REPO / "frontend" / "public" / "service-worker.js").read_text(encoding="utf-8")
    assert "importScripts('/sw-url-safety.js')" in sw
    assert "resolveSafeAppUrl" in sw
    assert "addEventListener('fetch'" not in sw and 'addEventListener("fetch"' not in sw
    assert "silent: false" in sw or "silent:false" in sw
    _pass("service_worker_contract")


def test_frontend_permission_gate() -> None:
    panel = (REPO / "frontend" / "src" / "components" / "todos" / "MobileNotificationPanel.tsx").read_text(
        encoding="utf-8"
    )
    web = (REPO / "frontend" / "src" / "pwa" / "webPush.ts").read_text(encoding="utf-8")
    assert "isValidVapidPublicKey" in panel
    assert "getWebPushSubscriptionStatus" in panel
    assert "current_device_active" in panel or "currentDeviceActive" in panel
    assert "forceResubscribe" in panel
    assert "requestPermission" in web
    # Must not call requestPermission outside subscribeWebPush
    assert "requestPermission" not in panel
    reg = (REPO / "frontend" / "src" / "pwa" / "registerServiceWorker.ts").read_text(encoding="utf-8")
    assert "requestPermission" not in reg
    _pass("frontend_permission_and_device_status_gate")


def test_frontend_no_wechat_copy() -> None:
    roots = [REPO / "frontend" / "src", REPO / "frontend" / "public"]
    banned = ("微信", "二维码", "WxPusher", "wxpusher", "WXPUSHER", "wx-reminder")
    hits: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".ts", ".tsx", ".js", ".json", ".html", ".css"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for word in banned:
                if word in text:
                    hits.append(f"{path.relative_to(REPO)}:{word}")
    if hits:
        _fail("frontend_no_wechat_copy", "; ".join(hits[:8]))
    else:
        _pass("frontend_no_wechat_copy")


def test_safe_payload_and_relative_url() -> None:
    payload = ns.build_safe_message(3, 1)
    assert payload["url"] == "/app?tab=todos&review=1"
    assert "https://" not in payload["url"]
    for bad in ("todo_title", "entry_id", "ocr_text", "attachment_id", "ai_message"):
        try:
            ns._assert_safe_payload({**payload, bad: "secret"})
            raise AssertionError(bad)
        except ns.NotificationServiceError as exc:
            assert exc.code == "PAYLOAD_PRIVACY_VIOLATION"
    _pass("payload_relative_and_privacy")


def test_io_outside_txn() -> None:
    src = (BACKEND / "app" / "services" / "notification_service.py").read_text(encoding="utf-8")
    m = re.search(
        r"subscription = decrypt_subscription\(.*?\)\s*"
        r".*?db\.commit\(\)\s*"
        r".*?result = send_web_push\(",
        src,
        re.S,
    )
    if not m:
        _fail("io_outside_txn")
        return
    _pass("io_outside_txn")


def test_status_api_contract_source() -> None:
    routes = (BACKEND / "app" / "routes" / "notifications.py").read_text(encoding="utf-8")
    assert '"/web-push/subscriptions/status"' in routes
    assert "subscription_status_for_endpoint" in routes
    svc = (BACKEND / "app" / "services" / "web_push_service.py").read_text(encoding="utf-8")
    assert "current_device_active" in svc
    assert "Never echoes endpoint" in svc or "never echoes" in svc.lower()
    _pass("status_api_contract_source")


def test_no_legacy_fallback_or_dual_send() -> None:
    src = (BACKEND / "app" / "services" / "notification_service.py").read_text(encoding="utf-8")
    assert "notification_provider" not in src
    assert "UserNotificationBinding" not in src
    assert "notification_provider_session" not in src
    assert "WXPUSHER_RETIRED" in src
    assert "binding_id is not None and row.web_push_subscription_id is None" in src
    enqueue = src[src.index("def enqueue_due_notifications") : src.index("def reclaim_stale_processing")]
    assert "binding_id=None" in enqueue
    assert "web_push_subscription_id=int(subscription.id)" in enqueue
    assert "legacy" not in enqueue.lower()
    _pass("no_legacy_fallback_or_dual_send")


def test_openapi_has_no_wxpusher_paths() -> None:
    from app.routes.notifications import router

    paths = [route.path for route in router.routes]
    assert not any("wxpusher" in path.lower() for path in paths)
    _pass("openapi_no_wxpusher_paths")


def _schema_url(url: str, schema: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path=f"/{schema}"))


def test_cancel_legacy_pending_script_isolate() -> None:
    """Exercise dry-run and apply only in a random, disposable MySQL schema."""
    from sqlalchemy import create_engine, text

    base_url = settings.database_url
    schema = f"growth_log_r104_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    isolate_url = _schema_url(base_url, schema)
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE `{schema}`"))
        isolate_engine = create_engine(isolate_url)
        with isolate_engine.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE notification_outbox (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    binding_id BIGINT NULL,
                    web_push_subscription_id BIGINT NULL,
                    status VARCHAR(16) NOT NULL,
                    error_code VARCHAR(64) NULL,
                    locked_by VARCHAR(128) NULL,
                    locked_at TIMESTAMP NULL,
                    updated_at TIMESTAMP NULL
                )
                """
            ))
            conn.execute(text(
                """
                INSERT INTO notification_outbox
                    (binding_id, web_push_subscription_id, status, locked_by, locked_at)
                VALUES
                    (1, NULL, 'pending', 'worker', CURRENT_TIMESTAMP),
                    (1, NULL, 'retry', 'worker', CURRENT_TIMESTAMP),
                    (1, NULL, 'processing', 'worker', CURRENT_TIMESTAMP),
                    (NULL, 1, 'pending', 'worker', CURRENT_TIMESTAMP),
                    (1, 1, 'pending', 'worker', CURRENT_TIMESTAMP),
                    (1, NULL, 'sent', 'worker', CURRENT_TIMESTAMP)
                """
            ))
        script = BACKEND / "scripts" / "cancel_legacy_wxpusher_pending.py"
        env = {**os.environ, "DATABASE_URL": isolate_url}
        dry_run = subprocess.run(
            [sys.executable, str(script), "--allow-schema", schema],
            cwd=str(BACKEND),
            env=env,
            capture_output=True,
            text=True,
        )
        assert dry_run.returncode == 0 and "pending=1" in dry_run.stdout and "cancelled=0" in dry_run.stdout
        with isolate_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM notification_outbox WHERE status='cancelled'")).scalar() == 0
        applied = subprocess.run(
            [sys.executable, str(script), "--apply", "--allow-schema", schema],
            cwd=str(BACKEND),
            env=env,
            capture_output=True,
            text=True,
        )
        assert applied.returncode == 0 and "cancelled=3" in applied.stdout
        with isolate_engine.connect() as conn:
            affected = conn.execute(text(
                """
                SELECT COUNT(*) FROM notification_outbox
                WHERE status='cancelled' AND error_code='WXPUSHER_RETIRED'
                  AND locked_by IS NULL AND locked_at IS NULL
                """
            )).scalar()
            assert affected == 3
    finally:
        try:
            with admin_engine.connect() as conn:
                conn.execute(text(f"DROP DATABASE IF EXISTS `{schema}`"))
        except Exception:
            pass
        admin_engine.dispose()
    _pass("cancel_legacy_pending_script_isolate")


def main() -> int:
    tests = [
        test_subscription_crypto_no_plaintext,
        test_ssrf_endpoint_allowlist,
        test_invalid_ports_stable_code,
        test_validate_subscription_uses_allowlist,
        test_vapid_invalid_and_mismatch,
        test_public_key_only_when_ready,
        test_sw_url_behavior,
        test_service_worker_no_fetch,
        test_frontend_permission_gate,
        test_frontend_no_wechat_copy,
        test_safe_payload_and_relative_url,
        test_io_outside_txn,
        test_status_api_contract_source,
        test_no_legacy_fallback_or_dual_send,
        test_openapi_has_no_wxpusher_paths,
        test_cancel_legacy_pending_script_isolate,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            _fail(fn.__name__, type(exc).__name__ + ":" + str(exc)[:160])
    print(f"SUMMARY pass={len(PASSES)} fail={len(FAILS)}")
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
        return 1
    print("ALL_UNIT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

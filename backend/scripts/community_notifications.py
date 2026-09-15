"""Community Web Push contracts; production legacy-outbox cancellation is excluded."""
import test_r101_web_push as checks
names = [
    "test_subscription_crypto_no_plaintext", "test_ssrf_endpoint_allowlist",
    "test_invalid_ports_stable_code", "test_validate_subscription_uses_allowlist",
    "test_vapid_invalid_and_mismatch", "test_public_key_only_when_ready",
    "test_sw_url_behavior", "test_service_worker_no_fetch",
    "test_frontend_permission_gate", "test_frontend_no_wechat_copy",
    "test_safe_payload_and_relative_url", "test_io_outside_txn",
    "test_status_api_contract_source", "test_no_legacy_fallback_or_dual_send",
    "test_openapi_has_no_wxpusher_paths",
]
for name in names:
    getattr(checks, name)()
assert not checks.FAILS, checks.FAILS
print("COMMUNITY_WEB_PUSH_CONTRACTS_OK")

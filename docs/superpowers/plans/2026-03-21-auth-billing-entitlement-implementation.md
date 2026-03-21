# Auth Billing Entitlement Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deploy-ready login, membership billing, and quota governance so platform-key generation enforces free/monthly and member/weekly limits while BYOK stays unlimited.

**Architecture:** Introduce a dedicated auth/billing backend service (FastAPI + MySQL + Redis), then integrate existing `web/server.py` generation flow with atomic `reserve/finalize` entitlement APIs. Keep resume generation logic in `tools/generate_resume.py`, adding request-level AI config override for BYOK and strict audit/idempotency semantics.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Alembic, Redis, MySQL, unittest, existing `web/server.py` and `tools/generate_resume.py`

---

## File Structure (lock before tasks)

- Create: `backend/auth_billing_service/main.py` (FastAPI app, route registration, middleware)
- Create: `backend/auth_billing_service/config.py` (env config + defaults from spec section 11)
- Create: `backend/auth_billing_service/db.py` (engine/session bootstrap)
- Create: `backend/auth_billing_service/models.py` (users, identities, bindings, subscriptions, counters, reservations, events, api keys, jobs, orders)
- Create: `backend/auth_billing_service/schemas.py` (request/response contracts)
- Create: `backend/auth_billing_service/services/auth_service.py` (send/login/refresh/logout)
- Create: `backend/auth_billing_service/services/session_service.py` (token issue/rotate/revoke, max 3 devices)
- Create: `backend/auth_billing_service/services/migration_service.py` (user-person binding bootstrap)
- Create: `backend/auth_billing_service/services/entitlement_service.py` (`reserve/finalize`, expiry recycle, idempotency)
- Create: `backend/auth_billing_service/workers/finalize_retry_worker.py` (pending finalize retry)
- Create: `backend/auth_billing_service/workers/reservation_recycle_worker.py` (expired reservation recycle)
- Create: `backend/auth_billing_service/services/payment_service.py` (create order + webhook idempotent update)
- Create: `backend/auth_billing_service/services/byok_service.py` (upsert/delete/resolve active key)
- Create: `backend/auth_billing_service/tests/test_auth_api.py`
- Create: `backend/auth_billing_service/tests/test_binding_migration.py`
- Create: `backend/auth_billing_service/tests/test_entitlement_reserve_finalize.py`
- Create: `backend/auth_billing_service/tests/test_entitlement_idempotency.py`
- Create: `backend/auth_billing_service/tests/test_billing_webhook.py`
- Create: `backend/auth_billing_service/tests/test_byok_api.py`
- Modify: `web/server.py` (`/api/generate` path: auth + reserve/finalize integration)
- Modify: `tools/generate_resume.py` (request-level ai config override, BYOK-safe logging)
- Modify: `README.md` (deployment + new backend env and flow)

---

## Chunk 1: Auth + Entitlement Core + Existing Service Integration

### Task 1: Scaffold backend service and config contracts

**Files:**
- Create: `backend/auth_billing_service/main.py`
- Create: `backend/auth_billing_service/config.py`
- Create: `backend/auth_billing_service/db.py`
- Create: `backend/auth_billing_service/schemas.py`
- Test: `backend/auth_billing_service/tests/test_auth_api.py`

- [ ] **Step 1: Write failing tests for health and auth route placeholders**
```python
def test_health_endpoint_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200

def test_auth_routes_not_ready_yet(client):
    resp = client.post("/auth/login", json={"channel": "email", "target": "a@b.com", "code": "000000"})
    assert resp.status_code in (401, 404)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_auth_api -v`  
Expected: FAIL with import/module/route-not-found errors

- [ ] **Step 3: Implement minimal FastAPI app and config loader**
- [ ] **Step 4: Implement `/health` route and schema stubs**

- [ ] **Step 5: Re-run tests and ensure wiring starts passing**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_auth_api -v`  
Expected: health PASS; auth placeholder behavior explicit

- [ ] **Step 6: Commit**
```bash
git add backend/auth_billing_service/main.py backend/auth_billing_service/config.py backend/auth_billing_service/db.py backend/auth_billing_service/schemas.py backend/auth_billing_service/tests/test_auth_api.py
git commit -m "feat: scaffold auth billing backend service"
```

### Task 2: Implement auth/session flows (send-code/login/refresh/logout)

**Files:**
- Create: `backend/auth_billing_service/services/auth_service.py`
- Create: `backend/auth_billing_service/services/session_service.py`
- Modify: `backend/auth_billing_service/main.py`
- Modify: `backend/auth_billing_service/models.py`
- Test: `backend/auth_billing_service/tests/test_auth_api.py`

- [ ] **Step 1: Add failing tests for send-code endpoints**
```python
def test_send_code_accepts_email_and_phone(client):
    r1 = client.post("/auth/send-code", json={"channel": "email", "target": "a@b.com"})
    r2 = client.post("/auth/send-code", json={"channel": "phone", "target": "13800000000"})
    assert r1.status_code == 200 and r2.status_code == 200
```

- [ ] **Step 2: Add failing tests for login/refresh/logout lifecycle**
```python
def test_login_returns_access_and_refresh(client):
    resp = client.post("/auth/login", json={...})
    assert "access_token" in resp.json()
    assert "refresh_token" in resp.json()

def test_refresh_rotates_refresh_token(client):
    old = login_and_get_refresh(client)
    resp = client.post("/auth/refresh", json={"refresh_token": old})
    assert resp.status_code == 200
    assert resp.json()["refresh_token"] != old

def test_logout_revokes_refresh_token(client):
    rt = login_and_get_refresh(client)
    client.post("/auth/logout", json={"refresh_token": rt})
    again = client.post("/auth/refresh", json={"refresh_token": rt})
    assert again.status_code == 401
```

- [ ] **Step 3: Run auth tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_auth_api -v`  
Expected: FAIL on missing auth/session behavior

- [ ] **Step 4: Implement send-code verification storage and throttling in Redis**
- [ ] **Step 5: Implement login user upsert + token issue**
- [ ] **Step 6: Implement refresh rotation + max 3 active sessions enforcement**
- [ ] **Step 7: Implement logout revoke**

- [ ] **Step 8: Re-run auth tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_auth_api -v`  
Expected: PASS for send-code/login/refresh/logout + max-3-session behavior

- [ ] **Step 9: Commit**
```bash
git add backend/auth_billing_service/services/auth_service.py backend/auth_billing_service/services/session_service.py backend/auth_billing_service/main.py backend/auth_billing_service/models.py backend/auth_billing_service/tests/test_auth_api.py
git commit -m "feat: implement auth endpoints and session lifecycle"
```

### Task 3: Implement user-person binding migration bootstrap

**Files:**
- Create: `backend/auth_billing_service/services/migration_service.py`
- Modify: `backend/auth_billing_service/main.py`
- Test: `backend/auth_billing_service/tests/test_binding_migration.py`

- [ ] **Step 1: Write failing migration tests**
```python
def test_bootstrap_creates_one_owner_per_person():
    result = bootstrap_bindings(data_dir="data")
    assert result.created_bindings > 0

def test_bootstrap_is_idempotent():
    r1 = bootstrap_bindings(data_dir="data")
    r2 = bootstrap_bindings(data_dir="data")
    assert r2.created_bindings == 0

def test_each_person_has_single_owner_constraint():
    ...
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_binding_migration -v`  
Expected: FAIL with missing service/function

- [ ] **Step 3: Implement bootstrap logic (one person_id one owner)**
- [ ] **Step 4: Implement fail-release gate (migration fail blocks startup)**
- [ ] **Step 4: Re-run migration tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_binding_migration -v`  
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add backend/auth_billing_service/services/migration_service.py backend/auth_billing_service/main.py backend/auth_billing_service/tests/test_binding_migration.py
git commit -m "feat: add person binding bootstrap migration"
```

### Task 4: Implement entitlement reserve/finalize core with atomic semantics

**Files:**
- Create: `backend/auth_billing_service/services/entitlement_service.py`
- Modify: `backend/auth_billing_service/models.py`
- Modify: `backend/auth_billing_service/main.py`
- Test: `backend/auth_billing_service/tests/test_entitlement_reserve_finalize.py`
- Test: `backend/auth_billing_service/tests/test_entitlement_idempotency.py`

- [ ] **Step 1: Add failing tests for free/monthly-3 and member/weekly-50**
```python
def test_free_user_monthly_limit_3():
    assert reserve_n_times(user="u1", mode="platform_key", n=3).all_allowed
    assert reserve(user="u1", mode="platform_key").allow is False

def test_member_weekly_limit_50():
    assert reserve_n_times(user="m1", mode="platform_key", n=50).all_allowed
    assert reserve(user="m1", mode="platform_key").allow is False
```

- [ ] **Step 2: Add failing tests for Beijing reset boundaries (week/month)**
```python
def test_weekly_reset_uses_beijing_monday_boundary():
    ...
```

- [ ] **Step 3: Add failing tests for BYOK bypass and reserve request_id idempotency**
```python
def test_byok_not_counted():
    for _ in range(100):
        assert reserve(user="u1", mode="byok").allow is True

def test_same_request_id_returns_same_reservation():
    a = reserve(user="u1", request_id="req-1")
    b = reserve(user="u1", request_id="req-1")
    assert a.reservation_id == b.reservation_id
```

- [ ] **Step 4: Add failing tests for finalize idempotency replay**
```python
def test_finalize_replay_is_idempotent():
    rid = reserve(...).reservation_id
    f1 = finalize(reservation_id=rid, result="success", idempotency_key="id-1")
    f2 = finalize(reservation_id=rid, result="success", idempotency_key="id-1")
    assert f1 == f2
```

- [ ] **Step 5: Run entitlement tests and verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_reserve_finalize backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: FAIL on missing reserve/finalize atomic behavior

- [ ] **Step 6: Implement reserve transaction + create-on-first-use counter**
- [ ] **Step 7: Implement finalize(success/fail) transaction + idempotency replay**
- [ ] **Step 8: Re-run entitlement tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_reserve_finalize backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: PASS

- [ ] **Step 9: Commit**
```bash
git add backend/auth_billing_service/services/entitlement_service.py backend/auth_billing_service/models.py backend/auth_billing_service/main.py backend/auth_billing_service/tests/test_entitlement_reserve_finalize.py backend/auth_billing_service/tests/test_entitlement_idempotency.py
git commit -m "feat: implement atomic reserve finalize with quota boundaries"
```

### Task 5: Integrate generation flow with auth + reserve/finalize

**Files:**
- Modify: `web/server.py`
- Modify: `backend/auth_billing_service/main.py`
- Test: `backend/auth_billing_service/tests/test_entitlement_reserve_finalize.py` (integration stubs)

- [ ] **Step 1: Add failing integration test for reserve rejection**
```python
def test_generate_denied_when_reserve_rejects():
    resp = client.post("/api/generate", json={...})
    assert resp.status_code == 403
```

- [ ] **Step 2: Add failing integration test for finalize-timeout enqueue-to-retry**
```python
def test_finalize_timeout_enqueues_pending_finalize_job():
    resp = client.post("/api/generate", json={...})
    assert pending_finalize_job_created(resp.json()["request_id"])
```

- [ ] **Step 3: Add failing integration test for unauthorized person_id binding**
```python
def test_generate_rejects_unbound_person_id():
    resp = client.post("/api/generate", json={"person_id": "other_user_person", ...})
    assert resp.status_code == 403
```

- [ ] **Step 4: Add failing integration test for reserve timeout fail-closed**
```python
def test_generate_denied_when_reserve_times_out():
    resp = client.post("/api/generate", json={...})
    assert resp.status_code in (403, 503)
```

- [ ] **Step 5: Add failing integration test for spoofed user_id ignored**
```python
def test_body_user_id_is_ignored():
    resp = client.post("/api/generate", json={"user_id": "spoofed", ...})
    assert resp.status_code in (401, 403)
```

- [ ] **Step 6: Add failing tests for service-to-service trust contract**
```python
def test_entitlement_backend_rejects_missing_service_signature():
    ...

def test_entitlement_backend_rejects_invalid_service_signature():
    ...
```

- [ ] **Step 7: Run tests to verify failure**

Run: `python3 tests/test_import_resume_parser.py && python3 -m unittest backend.auth_billing_service.tests.test_entitlement_reserve_finalize -v`  
Expected: FAIL for missing gate/retry behavior

- [ ] **Step 8: Implement `/api/generate` pre/post hooks**
```python
# before generate: validate auth + person binding + reserve
# after success: finalize(success)
# after fail: finalize(fail)
# on finalize timeout: enqueue pending_finalize job
# ignore request body user_id; use validated auth context only
```

- [ ] **Step 9: Implement entitlement backend signature verification middleware**

- [ ] **Step 10: Re-run tests**

Run: `python3 tests/test_import_resume_parser.py && python3 -m unittest backend.auth_billing_service.tests.test_entitlement_reserve_finalize -v`  
Expected: PASS

- [ ] **Step 11: Commit**
```bash
git add web/server.py backend/auth_billing_service/main.py backend/auth_billing_service/tests/test_entitlement_reserve_finalize.py
git commit -m "feat: enforce reserve finalize with trusted service identity"
```

### Task 6: Implement finalize retry worker + reservation recycle worker

**Files:**
- Create: `backend/auth_billing_service/workers/finalize_retry_worker.py`
- Create: `backend/auth_billing_service/workers/reservation_recycle_worker.py`
- Modify: `backend/auth_billing_service/services/entitlement_service.py`
- Test: `backend/auth_billing_service/tests/test_entitlement_idempotency.py`

- [ ] **Step 1: Add failing worker tests**
```python
def test_finalize_retry_worker_retries_and_dead_letters_after_5():
    ...

def test_recycle_worker_skips_success_pending_finalize():
    ...

def test_recycle_worker_retries_failures_before_dead_letter():
    ...
```

- [ ] **Step 2: Run worker tests to confirm failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: FAIL on missing workers/recycle safeguards

- [ ] **Step 3: Implement finalize retry worker (1m, 5m, 15m, 1h, 6h)**
- [ ] **Step 4: Implement reservation recycle worker with pending-finalize and success-event guards**
- [ ] **Step 5: Implement recycle retry-to-dead-letter path**
- [ ] **Step 6: Re-run worker tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: PASS

- [ ] **Step 7: Commit**
```bash
git add backend/auth_billing_service/workers/finalize_retry_worker.py backend/auth_billing_service/workers/reservation_recycle_worker.py backend/auth_billing_service/services/entitlement_service.py backend/auth_billing_service/tests/test_entitlement_idempotency.py
git commit -m "feat: add finalize retry and reservation recycle workers"
```

---

## Chunk 2: Payment + BYOK + Generator Override + Ops/Docs

### Task 7: Implement billing order and webhook processing (WeChat + Alipay)

**Files:**
- Create: `backend/auth_billing_service/services/payment_service.py`
- Modify: `backend/auth_billing_service/main.py`
- Modify: `backend/auth_billing_service/models.py`
- Test: `backend/auth_billing_service/tests/test_billing_webhook.py`

- [ ] **Step 1: Add failing tests for create-order**
```python
def test_create_order_returns_provider_payload(client):
    resp = client.post("/billing/create-order", json={"plan": "member_weekly50", "channel": "wechat"})
    assert resp.status_code == 200
    assert "order_no" in resp.json()

def test_create_order_uses_default_price_and_currency(client):
    resp = client.post("/billing/create-order", json={"plan": "member_weekly50", "channel": "alipay"})
    data = resp.json()
    assert data["amount_cents"] == 2990
    assert data["currency"] == "CNY"
```

- [ ] **Step 2: Add failing tests for webhook idempotency and signature checks**
```python
def test_webhook_invalid_signature_rejected(client):
    resp = client.post("/billing/webhook/wechat", data="...", headers={"X-Signature": "bad"})
    assert resp.status_code == 401

def test_duplicate_webhook_is_idempotent(client):
    first = post_valid_webhook(client, order_no="o1")
    second = post_valid_webhook(client, order_no="o1")
    assert first.status_code == 200 and second.status_code == 200

def test_duplicate_payment_matches_order_and_provider_trade_no():
    ...

def test_order_expiry_after_30_minutes():
    ...

def test_subscription_renewal_extends_active_and_resets_from_now_when_expired():
    ...
```

- [ ] **Step 3: Run webhook tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_billing_webhook -v`  
Expected: FAIL on missing order/webhook logic

- [ ] **Step 4: Implement `create-order` for `member_weekly50` with 30-minute expiry**
- [ ] **Step 5: Implement webhook verification + idempotent status transition**
- [ ] **Step 6: Implement subscription activation/renewal/rollback policy from spec**

- [ ] **Step 7: Re-run billing tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_billing_webhook -v`  
Expected: PASS

- [ ] **Step 8: Commit**
```bash
git add backend/auth_billing_service/services/payment_service.py backend/auth_billing_service/main.py backend/auth_billing_service/models.py backend/auth_billing_service/tests/test_billing_webhook.py
git commit -m "feat: implement payment order and webhook lifecycle"
```

### Task 8: Implement BYOK management and secure resolution

**Files:**
- Create: `backend/auth_billing_service/services/byok_service.py`
- Modify: `backend/auth_billing_service/main.py`
- Modify: `backend/auth_billing_service/models.py`
- Test: `backend/auth_billing_service/tests/test_byok_api.py`

- [ ] **Step 1: Add failing tests for BYOK upsert/delete/get behavior**
```python
def test_upsert_byok_replaces_active_key(client):
    ...

def test_delete_byok_deactivates_key(client):
    ...
```

- [ ] **Step 2: Add failing tests for precedence (request key > stored active key)**
```python
def test_request_key_overrides_stored_active_key():
    ...
```

- [ ] **Step 3: Add failing tests for no-plaintext exposure**
```python
def test_api_never_returns_plaintext_key(client):
    ...
```

- [ ] **Step 4: Add failing tests for BYOK validation rules**
```python
def test_empty_key_rejected(client):
    ...

def test_provider_allowlist_enforced(client):
    ...

def test_key_length_and_charset_validation(client):
    ...
```

- [ ] **Step 5: Run BYOK tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_byok_api -v`  
Expected: FAIL on missing BYOK service

- [ ] **Step 6: Implement encrypted storage + fingerprint + active key constraint**
- [ ] **Step 7: Implement `/byok/upsert` and `/byok/{provider}` delete**
- [ ] **Step 8: Re-run BYOK tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_byok_api -v`  
Expected: PASS

- [ ] **Step 9: Commit**
```bash
git add backend/auth_billing_service/services/byok_service.py backend/auth_billing_service/main.py backend/auth_billing_service/models.py backend/auth_billing_service/tests/test_byok_api.py
git commit -m "feat: add byok secure storage and API management"
```

### Task 9: Integrate request-level AI config override in generator

**Files:**
- Modify: `tools/generate_resume.py`
- Modify: `web/server.py`
- Test: `backend/auth_billing_service/tests/test_byok_api.py`

- [ ] **Step 1: Add failing tests for generator config override path**
```python
def test_generate_resume_accepts_request_level_ai_config():
    ...
```

- [ ] **Step 2: Add failing tests for log redaction (fingerprint only)**
```python
def test_byok_api_key_not_logged_in_plaintext():
    ...
```

- [ ] **Step 3: Add failing integration tests for BYOK no-quota path**
```python
def test_byok_generation_skips_reserve_finalize_calls():
    ...

def test_byok_generation_does_not_write_usage_counter():
    ...
```

- [ ] **Step 4: Run tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_byok_api -v`  
Expected: FAIL on missing override/redaction

- [ ] **Step 5: Implement `generate_resume(..., ai_config_override=...)`**
- [ ] **Step 6: Wire `web/server.py` request mode (`platform_key|byok`) to override builder**
- [ ] **Step 7: Ensure BYOK path bypasses reserve/finalize and usage writes**
- [ ] **Step 8: Ensure logs never print raw BYOK key**
- [ ] **Step 9: Re-run tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_byok_api -v`  
Expected: PASS

- [ ] **Step 10: Commit**
```bash
git add tools/generate_resume.py web/server.py backend/auth_billing_service/tests/test_byok_api.py
git commit -m "feat: support request-level ai config override for byok"
```

### Task 10: Add observability and retry/recycle operations

**Files:**
- Modify: `backend/auth_billing_service/main.py`
- Modify: `backend/auth_billing_service/workers/finalize_retry_worker.py`
- Modify: `backend/auth_billing_service/workers/reservation_recycle_worker.py`
- Test: `backend/auth_billing_service/tests/test_entitlement_idempotency.py`

- [ ] **Step 1: Add failing tests for metrics emission**
```python
def test_reserve_finalize_metrics_emitted():
    ...
```

- [ ] **Step 2: Add failing tests for dead-letter alerts**
```python
def test_dead_letter_alert_triggered_after_max_retry():
    ...

def test_invalid_signature_alert_triggers_over_threshold():
    # >5 invalid signatures in 5-minute window
    ...
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: FAIL on missing metrics/alerts

- [ ] **Step 4: Implement counters/timers for reserve/finalize and dead-letter**
- [ ] **Step 5: Implement alert hook thresholds from spec section 11.5**
- [ ] **Step 6: Re-run tests**

Run: `python3 -m unittest backend.auth_billing_service.tests.test_entitlement_idempotency -v`  
Expected: PASS

- [ ] **Step 7: Commit**
```bash
git add backend/auth_billing_service/main.py backend/auth_billing_service/workers/finalize_retry_worker.py backend/auth_billing_service/workers/reservation_recycle_worker.py backend/auth_billing_service/tests/test_entitlement_idempotency.py
git commit -m "feat: add entitlement observability and alert hooks"
```

### Task 11: Documentation and rollout readiness

**Files:**
- Modify: `README.md`
- Create: `backend/auth_billing_service/README.md`
- Create: `backend/auth_billing_service/policies/retention_policy.md`
- Create: `backend/auth_billing_service/policies/key_rotation_policy.md`

- [ ] **Step 1: Document backend env vars and defaults**
- [ ] **Step 2: Document migration/bootstrap and fail-release behavior**
- [ ] **Step 3: Document reserve/finalize flow and failure semantics**
- [ ] **Step 4: Add local runbook for backend + existing web server integration**
- [ ] **Step 5: Document retention defaults (audit=180 days, PII=365 days) and cleanup job interface**
- [ ] **Step 6: Document KMS key rotation cadence (90 days) and rotation runbook**

- [ ] **Step 7: Commit**
```bash
git add README.md backend/auth_billing_service/README.md backend/auth_billing_service/policies/retention_policy.md backend/auth_billing_service/policies/key_rotation_policy.md
git commit -m "docs: add auth billing operations and security policies"
```

---

## Final Verification and Handoff

- [ ] Run syntax checks:
  - `python3 -m py_compile tools/*.py web/server.py backend/auth_billing_service/*.py backend/auth_billing_service/services/*.py backend/auth_billing_service/workers/*.py`
- [ ] Run existing repo tests:
  - `python3 tests/test_import_resume_parser.py`
- [ ] Run backend tests:
  - `python3 -m unittest discover -s backend/auth_billing_service/tests -p 'test_*.py' -v`
- [ ] Verify key acceptance criteria manually:
  - auth loop complete: send-code -> login -> refresh rotate -> logout revoke
  - payment success activates membership and extends renewal correctly
  - duplicate webhook remains idempotent by (`order_no`, `provider_trade_no`)
  - webhook replay attack with same signed payload is rejected or treated as no-op with audit marker
  - free user monthly 3 on platform key
  - member weekly 50 on platform key
  - BYOK unlimited with no plaintext key exposure
  - BYOK path does not call reserve/finalize and does not write usage counters
  - person binding authorization enforced before generation
  - reserve timeout fail-closed (deny generation)
  - generation success always eventually finalized (no漏扣), verified via pending_finalize retry path and audit events
  - auditability: request_id/reservation_id/idempotency_key trace exists for each generation
  - observability: reserve/finalize latency and success metrics emitted, dead-letter counter increments on forced retry exhaustion
  - alerts: finalize_success_rate and dead_letter thresholds trigger alert hooks in test mode

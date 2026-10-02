# Plan: Sign in with ChatGPT + plan-billed audits for EVM Bench

Status: implemented (Phases 1–7), Phase 0 pending a real ChatGPT account · Last updated: 2026-10-02

## 0. Implementation status

Phases 1–7 are implemented. Phase 0 (a real audit billed to a real ChatGPT plan) still needs a ChatGPT
account and must be done before relying on this in production.

Verified locally against a fake OpenAI server, with the real API, oai_proxy, secretsvc, resultsvc, worker
`init.py`, Codex CLI 0.155.1, and Postgres 16. Only RabbitMQ was stubbed. These worked:
- dynamic registration with PKCE, nonce, `ext_agent_host_id` and `agent_name_hint`;
- ID-token verification against JWKS;
- credential storage;
- returning sign-in with `id_token_hint` and the issued client ID;
- revocation on sign-out;
- plan model list merging;
- a plan-billed job: Codex ran a full turn through the broker, which injected and refreshed the real access token;
- a mid-stream `subscription_sharing_usage_limit_exceeded` recorded on the job and shown in the UI.

What the local run found, and what Phase 0 must still check against the real API:

| Finding | Status |
|---|---|
| Codex 0.155.1 accepts the `openai_chatgpt_plan` provider config with `codex exec` (no `app-server` needed). | Verified locally |
| Codex sends its function tools (`exec_command`, `write_stdin`, `view_image`, …) **top-level**, plus one `namespace` tool. The preview says function tools must be in namespaces or `additional_tools`. | **Top risk:** check against the real API. If rejected, either a newer Codex groups them, or the proxy must rewrite tools and tool-call names. |
| Codex also sends `include: ["reasoning.encrypted_content"]`, `prompt_cache_key`, `client_metadata`, `parallel_tool_calls`, `reasoning.summary`. Not on the documented disallowed list, so they are passed through. | Check against the real API |
| Codex warns "Model metadata … not found" for discovered (non-curated) slugs and uses fallback metadata. | Expected; curated models are unaffected |
| Codex never called `GET /v1/models` or `/responses/compact` through the proxy during a turn. | Verified locally; long-running auto-compaction is not exercised |
| `earliest_refresh_at` format is undocumented; both epoch seconds and ISO 8601 are accepted. | Check |
| Field names for reasoning levels in the plan model list are undocumented; `supported_reasoning_levels` (Codex-style) and `reasoning_efforts` are accepted, else curated levels or `low/medium/high`. | Check |
| The "Manage usage" URL is configurable (`SIWC_MANAGE_USAGE_URL`, default `https://chatgpt.com/#settings`) because the docs don't give a deep link. | Check |

## 1. Goal

Let users sign in to EVM Bench with their ChatGPT account and run audits billed to their
ChatGPT plan (Plus/Pro, or Business/Enterprise where the admin allows it) instead of pasting
an OpenAI API key. Along the way, replace the hand-maintained model list with a model picker
that automatically shows the latest models available to the user.

Non-goals: removing API-key mode (it stays as an explicit alternative), changing the detect
prompt, the report format, or the worker sandbox.

## 2. Constraints from the SIWC docs

These shape every design decision below.

| Constraint | Impact on EVM Bench |
|---|---|
| Plan billing ("token sharing") is open now only to **open-source / locally hosted** apps. They register dynamically (`client_id=dynamic_agent_client`) and must use an HTTP loopback callback on `127.0.0.1` (only the port may vary). | Works today for the local quickstart (`127.0.0.1:1337` backend). A **public, multi-user deployment** needs a commercial client ID via the [interest form](https://openai.com/form/sign-in-with-chatgpt-interest/). The design supports both modes. |
| Access tokens last **1 hour**; refresh tokens 30 days, **rotated on each refresh** and must be refreshed serially (`refresh_token_reused` otherwise). | Audits run up to 3 h (`EVM_BENCH_CODEX_TIMEOUT_SECONDS=10800`), and Codex can't refresh a custom-provider token. Refresh must happen **outside** the worker. |
| Tokens must never be in browser storage, logs or URLs. | Credentials live only in the backend DB (encrypted) and the proxy. The worker is untrusted (`SECURITY.md`), so it never gets a real token. |
| Inference only via `POST /v1/responses`, with `store:false`, `stream:true`. No `max_output_tokens`, `temperature`, `top_p`, `background`, `tool_search`, hosted tools or file/code-interpreter. Function tools must be namespaced or sent via `additional_tools`. | Codex must be configured to stay within this. Compatibility of `codex exec` must be proven (Phase 0). |
| Models come from `GET /v1/models` with the user's token; show `visibility=="list"`, display `display_name`, send `slug`. | The static model list is replaced by a per-user catalog. |
| Errors: `subscription_sharing_usage_limit_exceeded` (429, can arrive mid-stream), `_user_not_eligible` (403), `_unsupported_capability` (400), `_invalid_user` (401). **Never silently switch billing paths.** | Structured job error codes; no automatic fallback to an API key. |
| UX: "Continue with ChatGPT" button with logo; one-time "You're using your ChatGPT plan" notice; "Manage usage" link wherever usage/limits appear. | Frontend work in Phase 6. |

## 3. Current state (what we build on)

- **Credentials:** the API key is held in browser sessionStorage (`frontend/src/app/page.tsx:38`) and sent with each job
  (`frontend/src/lib/jobs.ts:104`). `backend/api/routers/v1/jobs.py` validates it against
  `/v1/models` and writes it, plaintext (`direct`) or AES-GCM-encrypted (`proxy`), into the
  per-job secret bundle (`backend/api/util/secrets_bundle.py`).
- **Worker:** `backend/docker/worker/init.py` unpacks the bundle; in proxy mode
  `_write_codex_proxy_config` points Codex at `oai_proxy`. `backend/worker_runner/run_codex_detect.sh`
  runs `codex login --with-api-key` then `codex exec`.
- **Proxy:** `backend/oai_proxy/routers/catch_all.py` decrypts the token and forwards **any path**
  to `api.openai.com`.
- **Auth:** `backend/api/auth/abc.py` + `github.py` provide GitHub OAuth only (no PKCE/nonce). The session is an HS256 JWT
  with `user_id`, `login`, `avatar_url` (`backend/api/core/tokens.py`).
- **Models:** a static list duplicated in `frontend/src/data/models.json`,
  `backend/api/core/const.py` (`MODEL_REASONING_EFFORTS`/`ALLOWED_MODELS`) and
  `backend/worker_runner/model_map.json`. The UI defaults to `codex-gpt-5.2`
  (`page.tsx:39`). `backend/tests/test_shared_keys.py` asserts against `ALLOWED_MODELS`.

## 4. Target architecture

```
Browser ──"Continue with ChatGPT"──► backend /v1/auth  (PKCE + nonce, ID token verified via JWKS)
                                        └─► chatgpt_credentials (encrypted refresh/access token, client_id, scopes)
Browser ──GET /v1/models──────────► backend: curated list ∪ user's catalog (cached)
Browser ──POST /v1/jobs/start─────► backend: validate model vs catalog; bundle gets an opaque
           (model, effort, no key)           job token {user_id, job_id, model, exp}, key_mode="siwc"
Worker (Codex) ──Bearer <job token>──► oai_proxy (token broker)
                                         ├─ job is running and token not expired
                                         ├─ request.model == job.model
                                         ├─ allow only POST /v1/responses (+ GET /v1/models)
                                         ├─ remove disallowed params; force store:false, stream:true
                                         ├─ load credential; refresh under per-user lock if near expiry
                                         ├─ inject Bearer <access_token>, stream upstream
                                         └─ watch the response stream for subscription_sharing_* → record jobs.error_code
```

Key properties:
- The worker never holds any OpenAI credential, only a job-bound token the proxy honours
  while that job is running.
- Refresh happens in one place (proxy, with a DB lock), which solves the 1 h vs 3 h problem
  without restarting Codex.
- API-key mode is unchanged and remains selectable.

## 5. Phases

Each phase is independently mergeable. Phase 1 ships value without SIWC at all.

### Phase 0: Feasibility test run (1–2 days, gate for everything else)

1. Script a loopback OAuth sign-in (`dynamic_agent_client`, scopes
   `openid profile email offline_access resource.invoke chatgpt.tokens.use.direct`,
   `resource=https://api.openai.com/v1`, PKCE S256).
2. In the existing worker image, run a full audit with `codex exec` using:
   ```toml
   model_provider = "openai_chatgpt_plan"
   [model_providers.openai_chatgpt_plan]
   name = "openai_chatgpt_plan"
   base_url = "https://api.openai.com/v1"
   wire_api = "responses"
   env_key = "ACCESS_TOKEN"
   requires_openai_auth = false
   supports_websockets = false
   ```
3. Record:
   - whether `codex exec` works (docs only describe `codex app-server`);
   - whether Codex's built-in tools pass the namespacing rule;
   - which disallowed params Codex sends;
   - every reasoning level, `xhigh`/`max` included;
   - the exact `/v1/models` response for a Plus and a Pro account, including whether reasoning levels are exposed.
4. Bump `CODEX_VERSION` (`backend/docker/base/Dockerfile:37`, now `0.155.1`) if required.

**Exit criteria:** one successful audit end-to-end on plan billing. If `exec` is incompatible,
switch the runner to `codex app-server` (newline-delimited JSON over stdio) before Phase 4.

### Phase 1: Model catalog and auto-populating picker (works with API keys too)

Goal: the existing picker keeps its curated entries and automatically adds the latest models.

**Backend**
- New `GET /v1/models` (plus `POST /v1/models` that takes an API key in the body, for key mode; never in the URL).
  It returns `[{id, label, reasoning_efforts, default_effort, source: "curated"|"discovered"}]`:
  1. **Curated:** today's entries, moved into a single backend file (e.g. `backend/api/core/models.json`)
     that replaces `MODEL_REASONING_EFFORTS` and `model_map.json`. These stay first, in their current order.
  2. **Discovered:** entries from OpenAI `GET /v1/models` not already curated, appended after them.
     - Plan users: keep `visibility=="list"`, label = `display_name`.
     - API-key users: filter by name, keeping `gpt-5*`, `gpt-6*`, `*-codex` and dropping `-audio`, `-realtime`,
       `-tts`, `-transcribe`, `-search`, `-image` and dated snapshots. Label = raw ID.
  - Reasoning levels for discovered models: taken from the model list if it includes them (Phase 0), else `low/medium/high`.
  - Cache per user or per key hash for about 10 min. On upstream failure, return the curated list only (today's behaviour).
  - Operator setting `BACKEND_MODEL_DISCOVERY=off|plan_only|all` (default `plan_only`; `all`
    turns on the name filter for API-key users).
- `_require_allowed_model` (`backend/api/routers/v1/jobs.py:69`) and `StartJobForm.validate_reasoning_effort`
  (`backend/api/schemas/job.py`) check against the user's combined list instead of the static set.
- `jobs` gains `model_display_name` (migration). `jobs.model` (String(64)) stores the ID/slug.

**Worker:** no change. `_resolve_codex_model` (`init.py`) already passes unknown IDs through; curated
`codex-gpt-*` keys resolve via the curated file.

**Frontend**
- `useModels()` hook replaces the `models.json` import in `page.tsx`.
  - Show the curated list immediately, then add discovered models when the fetch returns.
  - Fetch again when the API key changes or the user signs in.
- Discovered entries get a "New" badge with the tooltip "Not yet tested with EVM Bench".
- Remember the last model chosen via the existing `useLocalStorage` hook; if it's no longer in the list,
  fall back to the first curated model. Remove the hardcoded default.
- Show `model_display_name` in `results-header.tsx:77` and `run-status-panel.tsx:68`. Keep
  `models.json` only as an offline fallback and for labels on older jobs.

**Tests:** list merging and ordering, the name filter, cache, upstream-failure fallback, validation
of discovered models; update `test_shared_keys.py` assertions on `ALLOWED_MODELS`.

### Phase 2: ChatGPT sign-in

- Extend `AuthBackendABC` so the PKCE verifier, nonce and redirect URI flow through redirect → callback.
  Store them in a short-lived encrypted cookie (same pattern as `oauth_state` in
  `backend/api/routers/v1/auth.py`). Update `github.py` to the new signature.
- New `backend/api/auth/chatgpt.py`:
  - OIDC discovery (`https://auth.openai.com`), JWKS verification via `PyJWKClient`
    (check issuer, audience = issued `client_id`, expiry and nonce).
  - Check that `chatgpt.tokens.use.direct` was granted. If not, keep the user signed in with plan
    billing off (consent declined).
  - Two modes in `AUTH_BACKEND_ARGUMENTS`:
    - `dynamic` (default, OSS/local): first sign-in uses `dynamic_agent_client` + `agent_name_hint="evmbench"` +
      a stable `ext_agent_host_id` generated once per deployment and stored in the DB.
      Save the issued `oaiapp_…` client_id per account, and use `id_token_hint` on later sign-ins.
    - `registered` (hosted): commercial confidential client; `client_secret` sent via HTTP Basic auth.
- `user_id = "chatgpt:" + sub` (no collision with GitHub numeric IDs); `login` = name/email;
  `avatar_url` = picture.
- `/auth/logout` revokes the refresh token at the `revocation_endpoint` (retry with backoff)
  and deletes the stored credential.

### Phase 3: Credential store and token broker

- Migration: `chatgpt_credentials(user_id PK, client_id, ext_agent_host_id, refresh_token_enc,
  access_token_enc, expires_at, earliest_refresh_at, scopes, id_token_enc, needs_reauth, updated_at)`
  plus `jobs.billing_source ('api_key'|'chatgpt_plan')` and `jobs.error_code`.
- New `CREDENTIALS_AES_KEY` setting, reusing `backend/api/util/aes_gcm.py`.
- `BACKEND_OAI_KEY_MODE` gains `siwc` (requires the `proxy` compose profile). `_encode_openai_token`
  (`backend/api/routers/v1/jobs.py`) issues a job token: AES-GCM-encrypted `{user_id, job_id, model, exp}`, with `exp` set to job
  timeout plus queue allowance. `init.py:_unpack_bundle` accepts `key_mode="siwc"`.
- `oai_proxy` gains DB access and becomes the broker:
  - **Path allowlist:** `POST /v1/responses`, `GET /v1/models`; everything else is rejected with 403.
    Today's catch-all forwards anything.
  - **Job binding:** the job must be `running` and the token unexpired; `body.model` must equal the job's model.
  - **Request cleanup:** remove disallowed params; force `store:false`, `stream:true`.
  - **Refresh:** refresh when the token expires within 5 min (respecting `earliest_refresh_at`), under
    `SELECT … FOR UPDATE` or a per-user advisory lock so multiple proxy workers never spend the same
    refresh token. Save the replacement refresh token before using the new access token.
  - **Unusable credentials:** on `invalid_grant`, `token_expired` or `refresh_token_reused`, set `needs_reauth`
    and fail the job with `error_code=reauth_required`. On `invalid_client`, log an operator config error.
  - **Response stream:** parse the stream as it passes through, without buffering. Copy `subscription_sharing_*` codes and
    request IDs to `jobs.error_code`.
  - Bounded backoff on `503`; never fall back to another credential.
- The existing `direct`/`proxy` modes are unchanged.

### Phase 4: Worker integration

- `init.py`: add a SIWC variant of `_write_codex_proxy_config` with `requires_openai_auth = false`,
  `supports_websockets = false`, web search off, and anything else Phase 0 identified.
- `run_codex_detect.sh`: for `siwc`, skip `codex login --with-api-key` and the `OPENAI_API_KEY`
  requirement; export the job token as `ACCESS_TOKEN`.
- Fallback error capture: read `subscription_sharing_*` codes from Codex's `--json` output in
  `agent.log` and include `error_code` in the resultsvc payload (`backend/resultsvc/routers/v1.py`).
- If Phase 0 required it: replace `codex exec` with a small `codex app-server` driver
  (`initialize` → `thread/start` → `turn/start`).

### Phase 5: Plan-billed model selection

Builds on Phase 1:
- For plan users, `GET /v1/models` fetches the catalog through the broker's refresh logic.
- Curated models that are **not** in the user's catalog are hidden (or shown disabled with the tooltip "Not on your
  ChatGPT plan"), so users can't queue a job that will fail with `unsupported_capability`.
- If a model drops off the user's plan between queueing and running, the proxy fails the job with
  `error_code=model_unavailable`. It never substitutes another model.
- Empty catalog (user not eligible): picker shows the reason, Submit is disabled, and the UI offers
  API-key mode as an explicit alternative.

### Phase 6: Frontend UX

- `/v1/integration/frontend` (`backend/api/schemas/integration.py`) adds `auth_provider`, `plan_usage_available`,
  `api_key_mode_available`.
- "Continue with ChatGPT" button with logo in `auth-status.tsx` / `app-header.tsx`.
- Billing choice on the submit form: "Use my ChatGPT plan" (default when connected) or
  "Use an API key". The key field is shown only for the latter, and the choice is stored on the job as
  `billing_source`. There is never an automatic switch between them.
- One-time "You're using your ChatGPT plan" notice after first connection.
- "Manage usage" link (to ChatGPT settings) in `run-status-panel.tsx`, and as the main action on
  `usage_limit_exceeded`. Clear messages for each `error_code`; "Reconnect ChatGPT" on `reauth_required`.
- Warning when choosing `xhigh`/`max`: long audits can use a large share of a weekly plan cap.

### Phase 7: Hardening, tests, docs

- Tests (in `backend/tests/`):
  - ID-token verification against a mock OIDC server (bad signature, audience, nonce, expiry);
  - concurrent refreshes (only one refresh per rotation);
  - proxy allowlist, model lock, param stripping;
  - mid-stream error mapping;
  - job-token expiry after the job finishes.
- Logging review: no tokens in proxy, backend or worker logs, and none in exception messages.
- Docs: `SECURITY.md` (new SIWC tier: tokens only in DB + proxy, worker holds a job-bound
  token), README architecture/flow, `.env.example` (`AUTH_BACKEND=chatgpt`,
  `BACKEND_OAI_KEY_MODE=siwc`, `CREDENTIALS_AES_KEY`, `BACKEND_MODEL_DISCOVERY`).
- Hosted deployments: submit the interest form; switch `AUTH_BACKEND_ARGUMENTS.mode` to
  `registered` once a client ID is issued.

## 6. Configuration summary

| Setting | Values | Purpose |
|---|---|---|
| `AUTH_BACKEND` | `github` \| `chatgpt` | Sign-in provider |
| `AUTH_BACKEND_ARGUMENTS` | `{"mode":"dynamic"}` or `{"mode":"registered","client_id":…,"client_secret":…}` | SIWC client mode |
| `BACKEND_OAI_KEY_MODE` | `direct` \| `proxy` \| `siwc` | How the worker reaches OpenAI |
| `CREDENTIALS_AES_KEY` | secret | Encrypts stored ChatGPT tokens |
| `BACKEND_MODEL_DISCOVERY` | `off` \| `plan_only` \| `all` | Auto-populating the model picker |

## 7. Data model changes

- `chatgpt_credentials`: new table (Phase 3).
- `deployment_settings` (or similar): stores the generated `ext_agent_host_id` (Phase 2).
- `jobs`: `model_display_name` (Phase 1), `billing_source`, `error_code` (Phase 3).

## 8. Risks and open decisions

| Item | Notes |
|---|---|
| **Deployment target** | Local/self-hosted works now; public hosted needs commercial approval. Decide early and submit the form in parallel. |
| **Codex compatibility** | Unverified for `codex exec` under the preview limits. Phase 0 settles this; fallback is `codex app-server`. |
| **Reasoning levels in the model list** | Unknown whether `/v1/models` exposes them; curated table + `low/medium/high` default as fallback. |
| **API-key model discovery** | Name filter is a guess; `BACKEND_MODEL_DISCOVERY` lets operators turn it off. |
| **Usage caps** | A 3 h `xhigh` audit may use up a weekly cap mid-run. The existing one-active-job-per-user rule (`_require_no_active_job`) helps, and the UI warns. |
| **Still in preview** | Per-host usage tracking and revoking transferred sessions are not available yet; APIs may change. |
| **Keep API-key mode?** | Recommended yes, as an explicit per-job choice. |

## 9. Suggested milestones

1. **M1:** Phase 0 test run + Phase 1 model picker (Phase 1 is shippable on its own).
2. **M2:** Phases 2–4: ChatGPT sign-in, broker and worker; plan-billed audits work end-to-end locally.
3. **M3:** Phases 5–7: plan-aware model selection, UX and hardening; ready for self-hosters.
4. **M4:** Hosted deployment once a commercial client ID is issued.

## Sources

[SIWC overview](https://developers.openai.com/siwc) ·
[Quickstart](https://developers.openai.com/siwc/quickstart) ·
[Website sign-in](https://developers.openai.com/siwc/website) ·
[Request a client ID](https://developers.openai.com/siwc/request-client-id) ·
[Plan usage for open-source apps](https://developers.openai.com/siwc/token-sharing-open-source) ·
[Registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in) ·
[Accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions) ·
[Models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference) ·
[Codex app-server](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) ·
[Self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) ·
[Token reference](https://developers.openai.com/siwc/token-sharing-open-source/token-reference) ·
[Errors and recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery) ·
[Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) ·
[UI/UX guidelines](https://developers.openai.com/siwc/ui-ux-guidelines)

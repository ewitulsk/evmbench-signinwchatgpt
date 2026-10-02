# Security

`evmbench` executes an LLM-driven agent against uploaded, untrusted code in an isolated worker environment. Treat the worker and anything it touches (filesystem, logs, outputs) as adversarial.

This document describes the trust boundaries and the two supported approaches for handling OpenAI credentials.

## Trust boundaries

- Uploaded code is untrusted and may be intentionally malicious.
- The agent can run tools and read files in its workspace.
- The worker runtime should be treated as an untrusted environment for any secret material.

## OpenAI credential handling

### Direct BYOK (default)

The user provides an OpenAI API key and the worker uses it directly (exported as `OPENAI_API_KEY` / `CODEX_API_KEY`).

This is designed for local use and straightforward self-hosting where the operator accepts that secrets are present in the worker environment.

### Proxy-token mode (optional)

The worker does not receive a plaintext OpenAI API key.

Instead:

- the backend encrypts the user-provided key and places an opaque token in the secret bundle
- the worker routes requests through `oai_proxy`
- `oai_proxy` decrypts the token and forwards requests upstream

This reduces the blast radius of a compromised worker by keeping plaintext API keys out of the agent container.

### Sign in with ChatGPT (optional)

Users sign in with ChatGPT (OAuth 2.0 + OpenID Connect with PKCE; ID tokens are verified against OpenAI's JWKS) and may bill audits to their ChatGPT plan.

- Access and refresh tokens are stored AES-GCM encrypted (`CREDENTIALS_AES_KEY`) in Postgres and are only ever decrypted by the backend and `oai_proxy`. They never reach the browser, the worker, logs, or URLs.
- The worker receives a **job-bound token**: an encrypted `{user, job, model, expiry}` claim that only `oai_proxy` can read. The proxy accepts it only while that job is queued or running, only for `POST /v1/responses` and `GET /v1/models`, and only for the model the user selected, so a compromised worker cannot use the plan for anything else or after the job ends.
- Refresh tokens rotate on every use; refreshes are serialized with a Postgres row lock across API and proxy workers. Unusable refresh tokens mark the credential as needing re-authentication; billing never falls back to another credential.
- Signing out revokes the refresh token with OpenAI.
- In this mode `oai_proxy` needs database access. It sits on the network shared with workers, so treat it as a sensitive service: keep it minimal and avoid logging request bodies or headers.

## Operational guidance

- Run workers with minimal privileges and scoped access; do not mount host credentials or unrelated secrets.
- Avoid logging request headers and environment variables in production environments.
- Assume agent output is untrusted input; validate and sanitize before using it in other systems.



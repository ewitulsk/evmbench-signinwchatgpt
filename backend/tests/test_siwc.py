# ruff: noqa: PT009, PT027, SLF001, S106, PLR2004, EM101
import asyncio
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_reasoning import consumer
from test_shared_keys import REPO_ROOT, TEST_ENV, upload_zip, worker


SIWC_ENV = {
    **TEST_ENV,
    'AUTH_BACKEND': 'chatgpt',
    'AUTH_BACKEND_ARGUMENTS': '{"mode":"dynamic"}',
    'BACKEND_OAI_KEY_MODE': 'siwc',
    'CREDENTIALS_AES_KEY': 'test-credentials-key',
}

with patch.dict(os.environ, TEST_ENV, clear=True):
    api_config = import_module('api.core.config')
    proxy_config = import_module('oai_proxy.core.config')
    const = import_module('api.core.const')
    catalog = import_module('api.core.model_catalog')
    oauth = import_module('api.siwc.oauth')
    job_token = import_module('api.siwc.job_token')
    credentials = import_module('api.siwc.credentials')
    errors = import_module('api.siwc.errors')
    chatgpt_backend = import_module('api.auth.chatgpt')
    auth_abc = import_module('api.auth.abc')
    tokens = import_module('api.core.tokens')
    deps = import_module('api.core.deps')
    jobs = import_module('api.routers.v1.jobs')
    models_router = import_module('api.routers.v1.models')
    auth_router = import_module('api.routers.v1.auth')
    integration = import_module('api.routers.v1.integration')
    job_schema = import_module('api.schemas.job')
    proxy_siwc = import_module('oai_proxy.siwc')
    proxy = import_module('oai_proxy.routers.catch_all')
    job_model = import_module('api.models.job')
    credential_model = import_module('api.models.chatgpt_credential')

ISSUER = 'https://auth.test'
ISSUED_CLIENT_ID = 'oaiapp_test123'
PROXY_SECRET = TEST_ENV['OAI_PROXY_AES_KEY']


def siwc_settings(**overrides: str) -> object:
    with patch.dict(os.environ, {**SIWC_ENV, **overrides}, clear=True):
        return api_config.Settings(_env_file=None)


class FakeAuthServer:
    """Minimal OpenAI authorization server: discovery, JWKS, token and revocation endpoints."""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        self.jwks = {'keys': [{**jwk, 'kid': 'key-1', 'alg': 'RS256', 'use': 'sig'}]}
        self.token_requests: list[dict[str, list[str]]] = []
        self.token_auth_headers: list[str | None] = []
        self.revocations: list[dict[str, list[str]]] = []
        self.nonce = ''
        self.audience = ISSUED_CLIENT_ID
        self.token_response: dict | None = None
        self.token_status = 200

    def id_token(self, *, nonce: str | None = None, audience: str | None = None, kid: str = 'key-1') -> str:
        now = int(time.time())
        return jwt.encode(
            {
                'iss': ISSUER,
                'aud': audience or self.audience,
                'sub': 'user-sub-1',
                'iat': now,
                'exp': now + 600,
                'nonce': self.nonce if nonce is None else nonce,
                'email': 'auditor@example.com',
                'name': 'Auditor',
                'picture': 'https://example.com/a.png',
            },
            self.private_key,
            algorithm='RS256',
            headers={'kid': kid},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == '/.well-known/openid-configuration':
            return httpx.Response(
                200,
                json={
                    'issuer': ISSUER,
                    'authorization_endpoint': f'{ISSUER}/api/accounts/authorize',
                    'token_endpoint': f'{ISSUER}/api/accounts/oauth/token',
                    'jwks_uri': f'{ISSUER}/.well-known/jwks.json',
                    'revocation_endpoint': f'{ISSUER}/api/accounts/oauth/revoke',
                },
            )
        if path == '/.well-known/jwks.json':
            return httpx.Response(200, json=self.jwks)
        form = parse_qs(request.content.decode())
        if path == '/api/accounts/oauth/token':
            self.token_requests.append(form)
            self.token_auth_headers.append(request.headers.get('authorization'))
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={'error': 'invalid_grant'})
            return httpx.Response(
                200,
                json=self.token_response
                or {
                    'access_token': 'access-1',
                    'refresh_token': 'refresh-1',
                    'id_token': self.id_token(),
                    'token_type': 'Bearer',
                    'expires_in': 3600,
                    'scope': 'chatgpt.tokens.use.direct email offline_access openid profile resource.invoke',
                },
            )
        if path == '/api/accounts/oauth/revoke':
            self.revocations.append(form)
            return httpx.Response(200)
        return httpx.Response(404)

    def client(self, **kwargs: str) -> object:
        return oauth.SiwcOAuthClient(
            issuer=ISSUER,
            http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            **kwargs,
        )


class OAuthClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeAuthServer()
        self.backend = chatgpt_backend.ChatGPTAuthBackend({'mode': 'dynamic'})
        self.backend.oauth = self.server.client()

    async def _begin(self, hint: object = None) -> tuple[dict[str, list[str]], dict[str, str]]:
        request = await self.backend.begin(
            auth_abc.AuthContext(
                redirect_uri='http://127.0.0.1:1337/v1/auth/callback',
                state='state-1',
                account_hint=hint,
                ext_agent_host_id='urn:uuid:host-1',
            ),
        )
        query = parse_qs(urlparse(request.url).query)
        self.server.nonce = request.transaction['nonce']
        return query, request.transaction

    async def test_first_sign_in_uses_dynamic_registration_with_pkce(self) -> None:
        query, transaction = await self._begin()
        self.assertEqual(query['client_id'], ['dynamic_agent_client'])
        self.assertEqual(query['agent_name_hint'], ['evmbench'])
        self.assertEqual(query['ext_agent_host_id'], ['urn:uuid:host-1'])
        self.assertEqual(query['resource'], ['https://api.openai.com/v1'])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(
            set(query['scope'][0].split()),
            {'openid', 'profile', 'email', 'offline_access', 'resource.invoke', 'chatgpt.tokens.use.direct'},
        )
        self.assertNotIn('id_token_hint', query)
        self.assertNotEqual(query['code_challenge'][0], transaction['code_verifier'])
        self.assertEqual(query['nonce'], [transaction['nonce']])

    async def test_returning_account_reuses_issued_client_id(self) -> None:
        query, _ = await self._begin(auth_abc.AccountHint(client_id=ISSUED_CLIENT_ID, id_token='old-id-token'))
        self.assertEqual(query['client_id'], [ISSUED_CLIENT_ID])
        self.assertEqual(query['id_token_hint'], ['old-id-token'])
        self.assertNotIn('agent_name_hint', query)
        self.assertEqual(query['ext_agent_host_id'], ['urn:uuid:host-1'])

    async def test_callback_exchanges_code_and_verifies_id_token(self) -> None:
        _, transaction = await self._begin()
        result = await self.backend.complete(
            code='code-1',
            params={'client_id': ISSUED_CLIENT_ID, 'state': 'state-1'},
            transaction=transaction,
            redirect_uri='http://127.0.0.1:1337/v1/auth/callback',
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.token.user_id, 'chatgpt:user-sub-1')
        self.assertEqual(result.token.provider, 'chatgpt')
        self.assertEqual(result.token.login, 'Auditor')
        self.assertEqual(result.chatgpt.client_id, ISSUED_CLIENT_ID)
        self.assertEqual(result.chatgpt.ext_agent_host_id, 'urn:uuid:host-1')
        self.assertTrue(result.chatgpt.tokens.has_plan_usage)
        form = self.server.token_requests[0]
        self.assertEqual(form['grant_type'], ['authorization_code'])
        self.assertEqual(form['client_id'], [ISSUED_CLIENT_ID])
        self.assertEqual(form['code_verifier'], [transaction['code_verifier']])
        self.assertEqual(form['resource'], ['https://api.openai.com/v1'])
        self.assertNotIn('client_secret', form)

    async def test_callback_rejects_bad_id_tokens(self) -> None:
        cases = {
            'nonce': {'nonce': 'other-nonce'},
            'audience': {'audience': 'oaiapp_someone_else'},
            'unknown key': {'kid': 'missing'},
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                _, transaction = await self._begin()
                self.server.token_response = {
                    'access_token': 'a',
                    'id_token': self.server.id_token(**kwargs),
                    'expires_in': 3600,
                    'scope': 'openid',
                }
                result = await self.backend.complete(
                    code='code',
                    params={'client_id': ISSUED_CLIENT_ID},
                    transaction=transaction,
                    redirect_uri='http://127.0.0.1:1337/v1/auth/callback',
                )
                self.assertIsNone(result)

    async def test_callback_without_issued_client_id_or_with_error_is_rejected(self) -> None:
        _, transaction = await self._begin()
        for params in ({}, {'error': 'access_denied', 'client_id': ISSUED_CLIENT_ID}):
            with self.subTest(params=params):
                result = await self.backend.complete(
                    code='code',
                    params=params,
                    transaction=transaction,
                    redirect_uri='http://127.0.0.1:1337/v1/auth/callback',
                )
                self.assertIsNone(result)
        self.assertEqual(self.server.token_requests, [])

    async def test_declined_plan_scope_is_reported(self) -> None:
        _, transaction = await self._begin()
        self.server.token_response = {
            'access_token': 'a',
            'id_token': self.server.id_token(),
            'expires_in': 3600,
            'scope': 'openid profile email',
        }
        result = await self.backend.complete(
            code='code',
            params={'client_id': ISSUED_CLIENT_ID},
            transaction=transaction,
            redirect_uri='http://127.0.0.1:1337/v1/auth/callback',
        )
        self.assertFalse(result.chatgpt.tokens.has_plan_usage)

    async def test_registered_confidential_client_uses_basic_auth(self) -> None:
        client = self.server.client(client_id='oaiapp_registered', client_secret='s3cret')
        await client.refresh(client_id='oaiapp_registered', refresh_token='r')
        form = self.server.token_requests[-1]
        self.assertNotIn('client_id', form)
        self.assertNotIn('client_secret', form)
        self.assertTrue(self.server.token_auth_headers[-1].startswith('Basic '))

    async def test_refresh_and_revoke_requests(self) -> None:
        client = self.server.client()
        tokens_ = await client.refresh(client_id=ISSUED_CLIENT_ID, refresh_token='refresh-0')
        self.assertEqual(tokens_.access_token, 'access-1')
        form = self.server.token_requests[-1]
        self.assertEqual(form['grant_type'], ['refresh_token'])
        self.assertEqual(form['refresh_token'], ['refresh-0'])
        self.assertNotIn('scope', form)
        self.assertTrue(await client.revoke(client_id=ISSUED_CLIENT_ID, refresh_token='refresh-1'))
        self.assertEqual(self.server.revocations[0]['token_type_hint'], ['refresh_token'])

        self.server.token_status = 400
        with self.assertRaises(oauth.OAuthError) as ctx:
            await client.refresh(client_id=ISSUED_CLIENT_ID, refresh_token='refresh-0')
        self.assertEqual(ctx.exception.code, 'invalid_grant')
        self.server.token_status = 503
        with self.assertRaises(oauth.TemporaryOAuthError):
            await client.refresh(client_id=ISSUED_CLIENT_ID, refresh_token='refresh-0')

    def test_earliest_refresh_at_formats(self) -> None:
        for value in (1790036132, '1790036132', '2026-09-20T00:00:00Z'):
            with self.subTest(value=value):
                self.assertIsNotNone(oauth._parse_timestamp(value))
        self.assertIsNone(oauth._parse_timestamp('not a time'))

    def test_registered_mode_requires_client_id(self) -> None:
        with self.assertRaises(ValueError):
            oauth.oauth_client_from_args({'mode': 'registered'})
        with self.assertRaises(ValueError):
            oauth.oauth_client_from_args({'mode': 'weird'})


class JobTokenTests(unittest.TestCase):
    def test_round_trip_and_rejections(self) -> None:
        claims = job_token.JobTokenClaims(user_id='u', job_id=uuid.uuid4(), model='gpt-5.5', exp=int(time.time()) + 60)
        token = job_token.issue_job_token(claims, secret='secret')
        self.assertTrue(job_token.is_job_token(token))
        self.assertEqual(job_token.parse_job_token(token, secret='secret'), claims)
        self.assertIsNone(job_token.parse_job_token(token, secret='other'))
        self.assertIsNone(job_token.parse_job_token(token[:-4] + 'AAAA', secret='secret'))
        self.assertIsNone(job_token.parse_job_token(token, secret='secret', now=claims.exp + 1))
        self.assertIsNone(job_token.parse_job_token('not-a-job-token', secret='secret'))


class CatalogTests(unittest.TestCase):
    def test_plan_catalog_marks_missing_curated_models_and_appends_new_ones(self) -> None:
        payload = {
            'models': [
                {'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'visibility': 'list'},
                {
                    'slug': 'gpt-6-astra',
                    'display_name': 'GPT-6 Astra',
                    'visibility': 'list',
                    'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}, {'effort': 'ultra'}],
                    'default_reasoning_level': 'high',
                },
                {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1 Sol', 'visibility': 'list'},
                {'slug': 'internal-model', 'display_name': 'Hidden', 'visibility': 'hide'},
            ],
        }
        merged = catalog.merge_plan_catalog(catalog.parse_plan_models(payload))
        by_id = {model.id: model for model in merged}
        self.assertEqual([m.id for m in merged[: len(const.CURATED_MODELS)]], [m.id for m in const.CURATED_MODELS])
        self.assertTrue(by_id['codex-gpt-5.5'].available)
        self.assertFalse(by_id['codex-gpt-5.4'].available)
        self.assertEqual(by_id['codex-gpt-5.4'].unavailable_reason, catalog.NOT_ON_PLAN_REASON)
        self.assertEqual(by_id['codex-gpt-6-astra'].reasoning_efforts, ('low', 'high'))
        self.assertEqual(by_id['codex-gpt-6-astra'].default_reasoning_effort, 'high')
        self.assertEqual(by_id['gpt-6.1-sol'].source, 'discovered')
        self.assertEqual(by_id['gpt-6.1-sol'].codex_model, 'gpt-6.1-sol')
        self.assertEqual(by_id['gpt-6.1-sol'].label, 'GPT-6.1 Sol')
        self.assertEqual(by_id['gpt-6.1-sol'].reasoning_efforts, const.DEFAULT_DISCOVERED_REASONING_EFFORTS)
        self.assertNotIn('internal-model', by_id)

    def test_api_key_discovery_filters_by_name(self) -> None:
        payload = {
            'data': [
                {'id': model_id}
                for model_id in (
                    'gpt-6.1-sol',
                    'gpt-5.5',
                    'gpt-5.7-codex',
                    'gpt-5-mini',
                    'gpt-realtime',
                    'gpt-5-chat-latest',
                    'gpt-5-search-api',
                    'gpt-4o-audio-preview',
                    'gpt-5.2-2025-12-11',
                    'text-embedding-3-large',
                    'gpt-image-1',
                )
            ],
        }
        ids = catalog.parse_api_models(payload)
        self.assertEqual(ids, ['gpt-5-mini', 'gpt-5.5', 'gpt-5.7-codex', 'gpt-6.1-sol'])
        merged = catalog.merge_api_catalog(ids)
        discovered = [m.id for m in merged if m.source == 'discovered']
        # gpt-5.5 is already curated (as codex-gpt-5.5)
        self.assertEqual(discovered, ['gpt-5-mini', 'gpt-5.7-codex', 'gpt-6.1-sol'])

    def test_bad_catalog_shapes(self) -> None:
        for payload in ({}, {'models': 'x'}, [], None):
            with self.subTest(payload=payload), self.assertRaises(catalog.CatalogUnavailableError):
                catalog.parse_plan_models(payload)

    def test_curated_models_match_frontend_fallback_and_worker_map(self) -> None:
        frontend = json.loads((REPO_ROOT / 'frontend/src/data/models.json').read_text())
        model_map = json.loads((REPO_ROOT / 'backend/worker_runner/model_map.json').read_text())
        self.assertEqual(
            [(m['id'], m['label'], tuple(m['reasoningEfforts'])) for m in frontend],
            [(m.id, m.label, m.reasoning_efforts) for m in const.CURATED_MODELS],
        )
        self.assertEqual(model_map, {m.id: m.codex_model for m in const.CURATED_MODELS})


class FakeCredentialSession:
    def __init__(self, credential: object) -> None:
        self.credential = credential
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def get(self, _model: object, _key: object) -> object:
        return self.credential

    async def scalar(self, _stmt: object) -> object:
        return self.credential


class CredentialStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = credentials.CredentialStore('secret')

    def make_credential(self, *, expires_in: timedelta, scopes: str = 'chatgpt.tokens.use.direct openid') -> object:
        credential = credential_model.ChatGPTCredential(user_id='chatgpt:u', subject='u', client_id=ISSUED_CLIENT_ID)
        self.store._apply_tokens(
            credential,
            oauth.TokenSet(
                access_token='access-old',
                refresh_token='refresh-old',
                expires_at=datetime.now(tz=UTC) + expires_in,
                scopes=tuple(scopes.split()),
            ),
        )
        return credential

    async def test_fresh_token_is_returned_without_refresh(self) -> None:
        session = FakeCredentialSession(self.make_credential(expires_in=timedelta(minutes=30)))
        client = AsyncMock()
        self.assertEqual(await self.store.get_access_token(session, user_id='chatgpt:u', oauth=client), 'access-old')
        client.refresh.assert_not_awaited()

    async def test_near_expiry_refreshes_and_saves_rotated_tokens(self) -> None:
        credential = self.make_credential(expires_in=timedelta(minutes=2))
        session = FakeCredentialSession(credential)
        client = AsyncMock()
        client.refresh.return_value = oauth.TokenSet(
            access_token='access-new',
            refresh_token='refresh-new',
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            scopes=(),
        )
        self.assertEqual(await self.store.get_access_token(session, user_id='chatgpt:u', oauth=client), 'access-new')
        client.refresh.assert_awaited_once_with(client_id=ISSUED_CLIENT_ID, refresh_token='refresh-old')
        self.assertEqual(self.store._decrypt(credential.refresh_token_enc), 'refresh-new')
        # Scopes are kept when the refresh response omits them.
        self.assertIn('chatgpt.tokens.use.direct', credential.scopes)

    async def test_earliest_refresh_at_defers_refresh_while_token_is_valid(self) -> None:
        credential = self.make_credential(expires_in=timedelta(minutes=2))
        credential.earliest_refresh_at = datetime.now(tz=UTC) + timedelta(minutes=1)
        client = AsyncMock()
        token = await self.store.get_access_token(FakeCredentialSession(credential), user_id='chatgpt:u', oauth=client)
        self.assertEqual(token, 'access-old')
        client.refresh.assert_not_awaited()

    async def test_unusable_refresh_token_requires_reauth(self) -> None:
        for code in ('invalid_grant', 'token_expired', 'refresh_token_reused'):
            with self.subTest(code=code):
                credential = self.make_credential(expires_in=timedelta(minutes=-5))
                client = AsyncMock()
                client.refresh.side_effect = oauth.OAuthError(code)
                with self.assertRaises(credentials.ReauthRequiredError):
                    await self.store.get_access_token(
                        FakeCredentialSession(credential),
                        user_id='chatgpt:u',
                        oauth=client,
                    )
                self.assertTrue(credential.needs_reauth)
                self.assertEqual(self.store.plan_usage_status(credential).state, 'reauth_required')

    async def test_temporary_failure_keeps_credential(self) -> None:
        credential = self.make_credential(expires_in=timedelta(minutes=-5))
        client = AsyncMock()
        client.refresh.side_effect = oauth.TemporaryOAuthError('network_error')
        with self.assertRaises(oauth.TemporaryOAuthError):
            await self.store.get_access_token(FakeCredentialSession(credential), user_id='chatgpt:u', oauth=client)
        self.assertFalse(credential.needs_reauth)

    async def test_plan_usage_status(self) -> None:
        self.assertEqual(self.store.plan_usage_status(None).state, 'unavailable')
        declined = self.make_credential(expires_in=timedelta(hours=1), scopes='openid email')
        self.assertEqual(self.store.plan_usage_status(declined).state, 'declined')
        with self.assertRaises(credentials.PlanUsageUnavailableError):
            await self.store.get_access_token(FakeCredentialSession(declined), user_id='chatgpt:u', oauth=AsyncMock())
        self.assertEqual(
            self.store.plan_usage_status(self.make_credential(expires_in=timedelta(hours=1))).state,
            'connected',
        )


def plan_catalog_fixture() -> list:
    return catalog.merge_plan_catalog(
        catalog.parse_plan_models(
            {
                'models': [
                    {'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'visibility': 'list'},
                    {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1 Sol', 'visibility': 'list'},
                ],
            },
        ),
    )


class PlanJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = siwc_settings()
        for module in (jobs, job_schema, models_router, integration):
            self.enterContext(patch.object(module, 'settings', self.settings))
        self.session = MagicMock()
        self.session.commit = AsyncMock()
        self.publisher = AsyncMock()
        self.storage = AsyncMock()
        self.enterContext(patch.object(jobs, 'secret_storage', self.storage))
        self.plan_catalog = self.enterContext(
            patch.object(jobs, 'plan_catalog', AsyncMock(return_value=plan_catalog_fixture())),
        )
        self.token = tokens.Token(user_id='chatgpt:u', login='Auditor', avatar_url=None, provider='chatgpt')
        app = FastAPI()
        app.include_router(jobs.router, prefix='/v1')
        app.include_router(integration.router, prefix='/v1')
        app.dependency_overrides[jobs.get_db] = lambda: self.session
        app.dependency_overrides[jobs.get_rabbitmq_publisher] = lambda: self.publisher
        app.dependency_overrides[deps.get_token] = lambda: self.token
        self.client = self.enterContext(TestClient(app))

    def start(self, **data: str) -> httpx.Response:
        return self.client.post('/v1/jobs/start', data=data, files={'file': ('example.zip', upload_zip())})

    def bundle_payload(self) -> dict:
        bundle = self.storage.save_secret.await_args.args[1]
        with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
            return json.load(archive.extractfile('key.json'))

    def test_frontend_config_advertises_plan_usage(self) -> None:
        config = self.client.get('/v1/integration/frontend').json()
        self.assertEqual(config['auth_provider'], None)  # auth_backend is not initialised in tests
        self.assertTrue(config['plan_usage_available'])
        self.assertTrue(config['api_key_mode_available'])
        self.assertEqual(config['model_discovery'], 'plan_only')

    def test_plan_job_gets_job_bound_token_pinned_to_model(self) -> None:
        for model_id, slug in (('codex-gpt-5.5', 'gpt-5.5'), ('gpt-6.1-sol', 'gpt-6.1-sol')):
            with self.subTest(model=model_id):
                response = self.start(model=model_id, billing_source='chatgpt_plan')
                self.assertEqual(response.status_code, 200, response.text)
                payload = self.bundle_payload()
                self.assertEqual(payload['key_mode'], 'siwc')
                claims = job_token.parse_job_token(payload['openai_token'], secret=PROXY_SECRET)
                job = self.session.add.call_args.args[0]
                self.assertEqual(claims.model, slug)
                self.assertEqual(claims.user_id, 'chatgpt:u')
                self.assertEqual(claims.job_id, job.id)
                self.assertEqual(job.billing_source, 'chatgpt_plan')
                self.assertEqual(job.model, model_id)
                self.assertEqual(self.publisher.publish_job_start.await_args.kwargs['model'], model_id)

    def test_no_key_defaults_to_plan_billing(self) -> None:
        response = self.start(model='codex-gpt-5.5')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bundle_payload()['key_mode'], 'siwc')

    def test_plan_job_rejections(self) -> None:
        cases = [
            ({'model': 'codex-gpt-5.4', 'billing_source': 'chatgpt_plan'}, 412),  # not on plan
            ({'model': 'gpt-unknown', 'billing_source': 'chatgpt_plan'}, 401),
            ({'model': 'codex-gpt-5.5', 'billing_source': 'chatgpt_plan', 'reasoning_effort': 'max'}, 412),
            ({'model': 'codex-gpt-5.5', 'billing_source': 'chatgpt_plan', 'openai_key': 'sk-x'}, 412),
            ({'model': 'codex-gpt-5.5', 'billing_source': 'bogus'}, 412),
        ]
        for data, status in cases:
            with self.subTest(data=data):
                self.assertEqual(self.start(**data).status_code, status)
        self.publisher.publish_job_start.assert_not_awaited()

    def test_plan_errors_never_fall_back_to_another_billing_source(self) -> None:
        for error, status in (
            (credentials.ReauthRequiredError(), 401),
            (credentials.PlanUsageUnavailableError(), 403),
            (catalog.CatalogUnavailableError('down'), 503),
        ):
            with self.subTest(error=type(error).__name__):
                self.plan_catalog.side_effect = error
                self.assertEqual(self.start(model='codex-gpt-5.5', billing_source='chatgpt_plan').status_code, status)
        self.storage.save_secret.assert_not_awaited()

    def test_non_chatgpt_users_cannot_use_plan_billing(self) -> None:
        self.token = tokens.Token(user_id='1', login='gh', avatar_url=None, provider='github')
        self.assertEqual(self.start(model='codex-gpt-5.5', billing_source='chatgpt_plan').status_code, 403)

    def test_api_key_jobs_go_through_proxy_tokens_in_siwc_mode(self) -> None:
        with patch.object(jobs, 'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value.get.return_value = httpx.Response(200)
            response = self.start(model='codex-gpt-5.4', billing_source='api_key', openai_key='sk-user')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.bundle_payload()['key_mode'], 'proxy')
        self.assertEqual(self.session.add.call_args.args[0].billing_source, 'api_key')

    def test_api_key_mode_can_be_disabled(self) -> None:
        self.settings.BACKEND_API_KEY_MODE_ENABLED = False
        self.assertEqual(self.start(model='codex-gpt-5.4', openai_key='sk-user').status_code, 412)

    def test_plan_billing_rejected_when_deployment_lacks_it(self) -> None:
        with patch.dict(os.environ, TEST_ENV, clear=True):
            plain = api_config.Settings(_env_file=None)
        with patch.object(job_schema, 'settings', plain), patch.object(jobs, 'settings', plain):
            self.assertEqual(self.start(model='codex-gpt-5.5', billing_source='chatgpt_plan').status_code, 412)


class ApiKeyDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(os.environ, {**TEST_ENV, 'BACKEND_MODEL_DISCOVERY': 'all'}, clear=True):
            self.settings = api_config.Settings(_env_file=None)
        for module in (jobs, job_schema, models_router):
            self.enterContext(patch.object(module, 'settings', self.settings))
        catalog.catalog_cache._entries.clear()
        self.addCleanup(catalog.catalog_cache._entries.clear)
        self.session = MagicMock()
        self.session.commit = AsyncMock()
        self.publisher = AsyncMock()
        self.enterContext(patch.object(jobs, 'secret_storage', AsyncMock()))
        app = FastAPI()
        app.include_router(jobs.router, prefix='/v1')
        app.include_router(models_router.router, prefix='/v1')
        app.dependency_overrides[jobs.get_db] = lambda: self.session
        app.dependency_overrides[jobs.get_rabbitmq_publisher] = lambda: self.publisher
        self.client = self.enterContext(TestClient(app))
        upstream = {'data': [{'id': 'gpt-6.1-sol'}, {'id': 'gpt-5.5'}, {'id': 'whisper-1'}]}
        self.fetch = self.enterContext(patch.object(models_router, 'fetch_models', AsyncMock(return_value=upstream)))

    def test_models_endpoint_appends_discovered_models_and_caches(self) -> None:
        for _ in range(2):
            response = self.client.post('/v1/models', json={'openai_key': 'sk-user'})
            self.assertEqual(response.status_code, 200, response.text)
        models = response.json()['models']
        self.assertEqual([m['id'] for m in models if m['source'] == 'discovered'], ['gpt-6.1-sol'])
        self.fetch.assert_awaited_once_with('sk-user')

    def test_discovered_model_can_start_a_job(self) -> None:
        with patch.object(jobs, 'AsyncClient') as factory:
            factory.return_value.__aenter__.return_value.get.return_value = httpx.Response(200)
            response = self.client.post(
                '/v1/jobs/start',
                data={'model': 'gpt-6.1-sol', 'openai_key': 'sk-user', 'reasoning_effort': 'high'},
                files={'file': ('example.zip', upload_zip())},
            )
        self.assertEqual(response.status_code, 200, response.text)
        job = self.session.add.call_args.args[0]
        self.assertEqual((job.model, job.model_display_name), ('gpt-6.1-sol', 'gpt-6.1-sol'))
        # Discovered models get a conservative default set of reasoning levels.
        with patch.object(jobs, 'AsyncClient'):
            response = self.client.post(
                '/v1/jobs/start',
                data={'model': 'gpt-6.1-sol', 'openai_key': 'sk-user', 'reasoning_effort': 'max'},
                files={'file': ('example.zip', upload_zip())},
            )
        self.assertEqual(response.status_code, 412)

    def test_upstream_failure_falls_back_to_curated(self) -> None:
        self.fetch.side_effect = catalog.CatalogUnavailableError('down')
        response = self.client.post('/v1/models', json={'openai_key': 'sk-other'})
        self.assertEqual({m['source'] for m in response.json()['models']}, {'curated'})


class ModelsEndpointPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = siwc_settings()
        self.enterContext(patch.object(models_router, 'settings', self.settings))
        self.token = tokens.Token(user_id='chatgpt:u', login='A', avatar_url=None, provider='chatgpt')
        app = FastAPI()
        app.include_router(models_router.router, prefix='/v1')
        app.dependency_overrides[models_router.get_db] = lambda: MagicMock()
        app.dependency_overrides[deps.get_token] = lambda: self.token
        self.client = self.enterContext(TestClient(app))

    def test_plan_catalog_states(self) -> None:
        with patch.object(models_router, 'plan_catalog', AsyncMock(return_value=plan_catalog_fixture())):
            models = self.client.get('/v1/models', params={'billing_source': 'chatgpt_plan'}).json()['models']
        self.assertTrue(next(m for m in models if m['id'] == 'codex-gpt-5.5')['available'])
        self.assertEqual(next(m for m in models if m['id'] == 'gpt-6.1-sol')['source'], 'discovered')

        for error, reason in (
            (credentials.ReauthRequiredError(), models_router.RECONNECT_REASON),
            (credentials.PlanUsageUnavailableError(), models_router.PLAN_UNAVAILABLE_REASON),
        ):
            with patch.object(models_router, 'plan_catalog', AsyncMock(side_effect=error)):
                models = self.client.get('/v1/models', params={'billing_source': 'chatgpt_plan'}).json()['models']
            self.assertTrue(all(not m['available'] and m['unavailable_reason'] == reason for m in models))

        with patch.object(models_router, 'plan_catalog', AsyncMock(side_effect=catalog.CatalogUnavailableError())):
            response = self.client.get('/v1/models', params={'billing_source': 'chatgpt_plan'})
        self.assertEqual(response.status_code, 503)


class FakeDb:
    def __init__(self, job: object) -> None:
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=job)

    @asynccontextmanager
    async def acquire(self) -> object:
        yield self.session


class ProxyBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        with patch.dict(
            os.environ,
            {**SIWC_ENV, 'DATABASE_DSN': 'postgresql+asyncpg://localhost/test'},
            clear=True,
        ):
            self.proxy_settings = proxy_config.Settings(_env_file=None)
        self.enterContext(patch.object(proxy_siwc, 'settings', self.proxy_settings))
        self.enterContext(patch.object(proxy, 'settings', self.proxy_settings))
        self.job_id = uuid.uuid4()
        self.job = SimpleNamespace(
            user_id='chatgpt:u',
            billing_source='chatgpt_plan',
            status=job_model.JobStatus.running,
        )
        self.enterContext(patch.object(proxy_siwc, '_db', return_value=FakeDb(self.job)))
        self.store = MagicMock()
        self.store.get_access_token = AsyncMock(return_value='chatgpt-access-token')
        self.enterContext(patch.object(proxy_siwc, '_store', return_value=self.store))
        self.enterContext(patch.object(proxy_siwc, '_oauth', return_value=MagicMock()))
        self.record = self.enterContext(patch.object(proxy_siwc, 'record_job_error_code', AsyncMock()))
        app = FastAPI()
        app.include_router(proxy.router)
        self.client = self.enterContext(TestClient(app))
        self.requests: list[httpx.Request] = []
        self.upstream_body = b'data: {"type":"response.completed"}\n\n'
        self.upstream_status = 200

    def token(self, **overrides: object) -> str:
        claims = {
            'user_id': 'chatgpt:u',
            'job_id': self.job_id,
            'model': 'gpt-5.5',
            'exp': int(time.time()) + 600,
            **overrides,
        }
        return job_token.issue_job_token(job_token.JobTokenClaims(**claims), secret=PROXY_SECRET)

    def send(self, method: str, path: str, *, token: str | None = None, body: object = None) -> httpx.Response:
        def upstream(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.upstream_status, stream=httpx.ByteStream(self.upstream_body))

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with patch.object(proxy_siwc.httpx, 'AsyncClient', return_value=mock_client):
            return self.client.request(
                method,
                path,
                headers={'Authorization': f'Bearer {token or self.token()}', 'Accept-Encoding': 'gzip'},
                json=body,
            )

    def test_forwards_with_access_token_and_sanitized_body(self) -> None:
        body = {
            'model': 'gpt-5.5',
            'store': True,
            'stream': False,
            'temperature': 0.2,
            'max_output_tokens': 10,
            'tools': [{'type': 'function', 'name': 'shell'}, {'type': 'web_search'}],
            'input': [{'type': 'message', 'role': 'system', 'content': 'x'}, {'role': 'user', 'content': 'y'}],
        }
        response = self.send('POST', '/v1/responses', body=body)
        self.assertEqual(response.status_code, 200, response.text)
        sent = self.requests[0]
        self.assertEqual(str(sent.url), 'https://api.openai.com/v1/responses')
        self.assertEqual(sent.headers['authorization'], 'Bearer chatgpt-access-token')
        self.assertEqual(sent.headers['accept-encoding'], 'identity')
        forwarded = json.loads(sent.content)
        self.assertEqual((forwarded['store'], forwarded['stream']), (False, True))
        self.assertNotIn('temperature', forwarded)
        self.assertNotIn('max_output_tokens', forwarded)
        self.assertEqual(forwarded['tools'], [{'type': 'function', 'name': 'shell'}])
        self.assertEqual(forwarded['input'][0]['role'], 'developer')
        self.record.assert_not_awaited()

    def test_model_list_is_allowed(self) -> None:
        self.assertEqual(self.send('GET', '/v1/models').status_code, 200)

    def test_rejections_never_reach_upstream(self) -> None:
        cases = [
            ('POST', '/v1/responses', {'model': 'gpt-6-astra'}, None, 403),  # model pinned
            ('POST', '/v1/chat/completions', {'model': 'gpt-5.5'}, None, 403),  # route allowlist
            ('POST', '/v1/files', {'model': 'gpt-5.5'}, None, 403),
            ('POST', '/v1/responses', {'model': 'gpt-5.5'}, self.token(exp=int(time.time()) - 1), 401),
            ('POST', '/v1/responses', {'model': 'gpt-5.5'}, 'siwc.garbage', 401),
            ('POST', '/v1/responses', ['not', 'an', 'object'], None, 400),
        ]
        for method, path, body, token, status in cases:
            with self.subTest(path=path, body=body):
                self.assertEqual(self.send(method, path, token=token, body=body).status_code, status)
        self.assertEqual(self.requests, [])

    def test_finished_or_foreign_jobs_are_rejected(self) -> None:
        for field, value in (
            ('status', job_model.JobStatus.succeeded),
            ('status', job_model.JobStatus.failed),
            ('user_id', 'chatgpt:someone-else'),
            ('billing_source', 'api_key'),
        ):
            original = getattr(self.job, field)
            setattr(self.job, field, value)
            with self.subTest(field=field, value=value):
                self.assertEqual(self.send('POST', '/v1/responses', body={'model': 'gpt-5.5'}).status_code, 401)
            setattr(self.job, field, original)
        self.assertEqual(self.requests, [])

    def test_reauth_is_recorded_on_the_job(self) -> None:
        self.store.get_access_token.side_effect = credentials.ReauthRequiredError
        response = self.send('POST', '/v1/responses', body={'model': 'gpt-5.5'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'reauth_required')
        self.assertEqual(self.record.await_args.kwargs['code'], 'reauth_required')

    def test_temporary_refresh_failure_is_retryable(self) -> None:
        self.store.get_access_token.side_effect = oauth.TemporaryOAuthError('network_error')
        self.assertEqual(self.send('POST', '/v1/responses', body={'model': 'gpt-5.5'}).status_code, 503)
        self.record.assert_not_awaited()

    def test_usage_limit_errors_are_recorded_mid_stream_and_pre_stream(self) -> None:
        mid_stream = (
            b'data: {"type":"response.created"}\n\n'
            b'data: {"type":"response.failed","response":{"error":'
            b'{"code":"subscription_sharing_usage_limit_exceeded"}}}\n\n'
        )
        for status, body in (
            (200, mid_stream),
            (429, b'{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"limit"}}'),
        ):
            with self.subTest(status=status):
                self.record.reset_mock()
                self.upstream_status, self.upstream_body = status, body
                response = self.send('POST', '/v1/responses', body={'model': 'gpt-5.5'})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, body)
                self.assertEqual(self.record.await_args.kwargs['code'], 'usage_limit_exceeded')

    def test_api_key_proxy_tokens_are_unaffected(self) -> None:
        self.assertFalse(job_token.is_job_token('STATIC'))

    def test_error_code_split_across_chunks_is_found(self) -> None:
        async def chunks() -> object:
            yield b'data: {"code":"subscription_sha'
            yield b'ring_user_not_eligible"}\n\n'

        async def collect() -> list[bytes]:
            claims = job_token.JobTokenClaims(user_id='u', job_id=self.job_id, model='m', exp=0)
            with patch.object(proxy_siwc, '_record_error_code', AsyncMock()) as record:
                out = [chunk async for chunk in proxy_siwc._scan_stream(chunks(), claims)]
            self.assertEqual(record.await_args.args[1], 'user_not_eligible')
            return out

        self.assertEqual(len(asyncio.run(collect())), 2)


class AuthRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = siwc_settings(BACKEND_DEV='true')
        self.enterContext(patch.object(auth_router, 'settings', self.settings))
        self.backend = chatgpt_backend.ChatGPTAuthBackend({'mode': 'dynamic'})
        self.server = FakeAuthServer()
        self.backend.oauth = self.server.client()
        self.store = credentials.CredentialStore('secret')
        self.store.save = AsyncMock()
        self.enterContext(patch.object(auth_router, 'auth_backend', self.backend))
        self.enterContext(patch.object(auth_router, 'chatgpt_auth', return_value=(self.backend, self.store)))
        self.enterContext(
            patch.object(auth_router, 'get_ext_agent_host_id', AsyncMock(return_value='urn:uuid:host-1')),
        )
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=None)
        app = FastAPI()
        app.include_router(auth_router.router, prefix='/v1')
        app.dependency_overrides[auth_router.get_db] = lambda: self.session
        self.client = self.enterContext(TestClient(app, base_url='http://127.0.0.1:1337', follow_redirects=False))

    def test_full_sign_in_round_trip(self) -> None:
        response = self.client.get('/v1/auth/')
        self.assertEqual(response.status_code, 307)
        query = parse_qs(urlparse(response.headers['location']).query)
        self.assertEqual(query['redirect_uri'], ['http://127.0.0.1:1337/v1/auth/callback'])
        transaction = auth_router._decode_transaction(response.cookies['oauth_tx'])
        self.assertEqual(transaction['state'], query['state'][0])
        self.server.nonce = transaction['nonce']

        response = self.client.get(
            '/v1/auth/callback',
            params={'code': 'c', 'state': query['state'][0], 'client_id': ISSUED_CLIENT_ID},
        )
        self.assertEqual(response.status_code, 307)
        session_token = tokens.decode_token(response.cookies['session'])
        self.assertEqual(session_token.user_id, 'chatgpt:user-sub-1')
        self.assertEqual(session_token.provider, 'chatgpt')
        self.assertEqual(auth_router._decode_account_hint(response.cookies['chatgpt_account']), 'chatgpt:user-sub-1')
        saved = self.store.save.await_args.kwargs
        self.assertEqual(saved['client_id'], ISSUED_CLIENT_ID)
        self.assertEqual(saved['ext_agent_host_id'], 'urn:uuid:host-1')
        self.assertEqual(saved['tokens'].refresh_token, 'refresh-1')

    def test_state_mismatch_or_missing_transaction_creates_no_session(self) -> None:
        response = self.client.get('/v1/auth/')
        self.assertNotIn('session', self.client.get('/v1/auth/callback', params={'code': 'c', 'state': 'x'}).cookies)
        self.client.cookies.clear()
        state = parse_qs(urlparse(response.headers['location']).query)['state'][0]
        self.assertNotIn('session', self.client.get('/v1/auth/callback', params={'code': 'c', 'state': state}).cookies)
        self.store.save.assert_not_awaited()

    def test_expired_transaction_is_rejected(self) -> None:
        with patch.object(auth_router, 'TRANSACTION_TTL_SECONDS', -1):
            self.assertIsNone(auth_router._decode_transaction(auth_router._encode_transaction({'state': 's'})))

    def test_github_sessions_without_provider_still_decode(self) -> None:
        legacy = jwt.encode(
            {'user_id': '1', 'login': 'gh', 'avatar_url': None, 'exp': int(time.time()) + 60},
            TEST_ENV['BACKEND_JWT_SECRET'],
            algorithm='HS256',
        )
        self.assertEqual(tokens.decode_token(legacy).provider, 'github')


class WorkerSiwcTests(unittest.TestCase):
    def test_bundle_accepts_siwc_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode='w') as tar:
                for name, data in (
                    ('upload.zip', upload_zip()),
                    ('key.json', json.dumps({'openai_token': 'siwc.x', 'key_mode': 'siwc'}).encode()),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            _, token, mode = worker._unpack_bundle(buffer.getvalue(), Path(tmp))
        self.assertEqual((token, mode), ('siwc.x', 'siwc'))

    def test_runner_uses_plan_provider_without_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            codex = bin_dir / 'codex'
            codex.write_text(
                f'#!{sys.executable}\n'
                'import json, os, sys\n'
                'from pathlib import Path\n'
                'logs = Path(os.environ["LOGS_DIR"])\n'
                'if sys.argv[1] == "login":\n'
                '    (logs / "login-called").write_text("yes")\n'
                'else:\n'
                '    keys = ["ACCESS_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"]\n'
                '    (logs / "env.json").write_text(json.dumps({k: os.environ.get(k) for k in keys}))\n'
                '    Path(os.environ["SUBMISSION_DIR"], "audit.md").write_text("{\\"vulnerabilities\\": []}")\n',
            )
            codex.chmod(0o755)
            timeout = bin_dir / 'timeout'
            timeout.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
            timeout.chmod(0o755)
            runner_dir = REPO_ROOT / 'backend/worker_runner'
            runner = root / 'run_codex_detect.sh'
            runner.write_bytes((runner_dir / 'run_codex_detect.sh').read_bytes())
            runner.chmod(0o755)
            for name, value in {
                'AGENT_DIR': root,
                'SUBMISSION_DIR': root / 'submission',
                'LOGS_DIR': root / 'logs',
                'DETECT_MD_PATH': runner_dir / 'detect.md',
                'CODEX_RUNNER_SH': runner,
                'MODEL_MAP_PATH': runner_dir / 'model_map.json',
                'MODEL_KEY': 'gpt-6.1-sol',
                'OAI_PROXY_BASE_URL': 'http://oai.loc:8084',
            }.items():
                self.enterContext(patch.object(worker, name, value))

            env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'OPENAI_API_KEY': 'leaked-from-host'}
            with patch.dict(os.environ, env, clear=True):
                worker._run_codex_detect(openai_token='siwc.job-token', key_mode='siwc')

            seen = json.loads((root / 'logs/env.json').read_text())
            self.assertEqual(seen, {'ACCESS_TOKEN': 'siwc.job-token', 'OPENAI_API_KEY': None, 'CODEX_API_KEY': None})
            self.assertFalse((root / 'logs/login-called').exists())
            config = (root / '.codex/config.toml').read_text()
            self.assertIn('model_provider = "openai_chatgpt_plan"', config)
            self.assertIn('base_url = "http://oai.loc:8084/v1"', config)
            self.assertIn('env_key = "ACCESS_TOKEN"', config)
            self.assertIn('requires_openai_auth = false', config)
            self.assertIn('supports_websockets = false', config)

    def test_failure_summary_reads_codex_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            (logs / 'agent.log').write_text(
                '\n'.join(
                    json.dumps(event)
                    for event in (
                        {'type': 'thread.started'},
                        {'type': 'item.completed', 'item': {'type': 'error', 'message': 'Model metadata for x'}},
                        {'type': 'error', 'message': 'unexpected status 400 Bad Request: tools must be namespaced'},
                        {'type': 'turn.failed', 'error': {'message': 'unexpected status 400 Bad Request: tools'}},
                    )
                ),
            )
            with patch.object(worker, 'LOGS_DIR', logs):
                summary = worker._agent_failure_summary()
            self.assertIn('tools must be namespaced', summary)
            self.assertNotIn('Model metadata', summary)

            (logs / 'agent.log').write_text('Error: config.toml: unknown field\n')
            with patch.object(worker, 'LOGS_DIR', logs):
                self.assertEqual(worker._agent_failure_summary(), 'Error: config.toml: unknown field')

    def test_error_code_from_agent_log(self) -> None:
        self.assertEqual(
            worker._find_error_code('stream error: subscription_sharing_usage_limit_exceeded: limit'),
            'usage_limit_exceeded',
        )
        self.assertIsNone(worker._find_error_code('ordinary failure'))


class ConsumerTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovered_models_are_queued_with_supported_effort(self) -> None:
        for effort, accepted in (('high', True), ('max', True), ('ultra', False)):
            with self.subTest(effort=effort):
                body = json.dumps(
                    {
                        'type': 'job.start',
                        'job_id': 'test',
                        'secret_ref': 'ref',
                        'model': 'gpt-6.1-sol',
                        'result_token': 'token',
                        'reasoning_effort': effort,
                    },
                ).encode()
                message = MagicMock(body=body, ack=AsyncMock(), reject=AsyncMock(), nack=AsyncMock())
                backend = AsyncMock()
                backend.start_worker.return_value = MagicMock(error=None)
                with (
                    patch.object(consumer, 'workers_backend', backend),
                    patch.object(consumer, '_effective_max_concurrent_jobs', return_value=None),
                    patch.object(consumer, 'run_job_status_update', new_callable=AsyncMock),
                ):
                    await consumer.handle_job_start_message(message)
                if accepted:
                    message.ack.assert_awaited_once()
                else:
                    message.reject.assert_awaited_once_with(requeue=False)


class ErrorCodeTests(unittest.TestCase):
    def test_find_and_normalize(self) -> None:
        self.assertEqual(errors.find_error_code(b'{"code":"subscription_sharing_invalid_user"}'), 'invalid_user')
        self.assertIsNone(errors.find_error_code(b'{"code":"rate_limit_exceeded"}'))


TEST_DATABASE_DSN = os.environ.get('EVMBENCH_TEST_DATABASE_DSN')


@unittest.skipUnless(TEST_DATABASE_DSN, 'set EVMBENCH_TEST_DATABASE_DSN to a migrated Postgres database')
class PostgresCredentialTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the real row lock: concurrent refreshes must spend a rotating refresh token only once."""

    async def asyncSetUp(self) -> None:
        database = import_module('api.core.database')
        self.db = database.DatabaseManager(database_url=TEST_DATABASE_DSN, pool_size=5)
        self.store = credentials.CredentialStore('secret')
        self.user_id = f'chatgpt:{uuid.uuid4()}'
        async with self.db.acquire() as session:
            await self.store.save(
                session,
                user_id=self.user_id,
                subject='s',
                email=None,
                client_id=ISSUED_CLIENT_ID,
                ext_agent_host_id='urn:uuid:h',
                tokens=oauth.TokenSet(
                    access_token='access-0',
                    refresh_token='refresh-0',
                    expires_at=datetime.now(tz=UTC) - timedelta(minutes=1),
                    scopes=('chatgpt.tokens.use.direct',),
                ),
            )

    async def asyncTearDown(self) -> None:
        await self.db.engine.dispose()

    async def test_concurrent_refreshes_are_serialized(self) -> None:
        spent: list[str] = []

        async def refresh(*, client_id: str, refresh_token: str) -> object:  # noqa: ARG001
            if refresh_token in spent:
                raise oauth.OAuthError('refresh_token_reused')
            spent.append(refresh_token)
            await asyncio.sleep(0.2)
            n = len(spent)
            return oauth.TokenSet(
                access_token=f'access-{n}',
                refresh_token=f'refresh-{n}',
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
                scopes=(),
            )

        client = MagicMock()
        client.refresh = refresh

        async def get_token() -> str:
            async with self.db.acquire() as session:
                return await self.store.get_access_token(session, user_id=self.user_id, oauth=client)

        results = await asyncio.gather(*(get_token() for _ in range(5)))
        self.assertEqual(results, ['access-1'] * 5)
        self.assertEqual(spent, ['refresh-0'])

    async def test_host_id_is_stable_and_error_codes_first_wins(self) -> None:
        async with self.db.acquire() as session:
            first = await credentials.get_ext_agent_host_id(session)
        async with self.db.acquire() as session:
            self.assertEqual(await credentials.get_ext_agent_host_id(session), first)
        self.assertTrue(first.startswith('urn:uuid:'))

        job_id = uuid.uuid4()
        async with self.db.acquire() as session:
            session.add(
                job_model.Job(
                    id=job_id,
                    user_id=self.user_id,
                    model='codex-gpt-5.5',
                    file_name='x.zip',
                    billing_source='chatgpt_plan',
                ),
            )
            await session.commit()
        for code in ('usage_limit_exceeded', 'reauth_required'):
            async with self.db.acquire() as session:
                await credentials.record_job_error_code(session, job_id=job_id, code=code)
        async with self.db.acquire() as session:
            job = await session.get(job_model.Job, job_id)
            self.assertEqual(job.error_code, 'usage_limit_exceeded')


if __name__ == '__main__':
    unittest.main()

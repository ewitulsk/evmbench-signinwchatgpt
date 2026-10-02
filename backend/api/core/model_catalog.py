"""Model catalog for the picker: curated models plus models discovered from OpenAI's model list."""

import hashlib
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import httpx
import orjson

from api.core.const import (
    CURATED_MODELS,
    DEFAULT_DISCOVERED_REASONING_EFFORTS,
    SUPPORTED_REASONING_EFFORTS,
    CuratedModel,
)


MODELS_URL = 'https://api.openai.com/v1/models'
NOT_ON_PLAN_REASON = 'Not on your ChatGPT plan'

# API-key discovery: the standard model list has every model and no labels, so filter by name.
_API_MODEL_RE = re.compile(r'^(gpt-[56](\.\d+)?(-[a-z0-9]+)*|[a-z0-9.-]+-codex(-[a-z0-9]+)*)$')
_API_MODEL_EXCLUDED_PARTS = ('audio', 'realtime', 'tts', 'transcribe', 'search', 'image', 'chat')
_DATED_SNAPSHOT_RE = re.compile(r'-\d{4}-\d{2}-\d{2}$')


class CatalogUnavailableError(Exception):
    """The upstream model list couldn't be loaded."""


@dataclass(frozen=True)
class CatalogModel:
    id: str
    label: str
    # Upstream model slug the worker will request (job tokens are locked to it).
    codex_model: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None
    source: str  # curated | discovered
    available: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class PlanModel:
    slug: str
    display_name: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None


def _default_effort(efforts: tuple[str, ...], preferred: str | None = None) -> str | None:
    if preferred in efforts:
        return preferred
    if 'medium' in efforts:
        return 'medium'
    return efforts[0] if efforts else None


def _curated_entry(model: CuratedModel) -> CatalogModel:
    return CatalogModel(
        id=model.id,
        label=model.label,
        codex_model=model.codex_model,
        reasoning_efforts=model.reasoning_efforts,
        default_reasoning_effort=_default_effort(model.reasoning_efforts),
        source='curated',
    )


def curated_catalog() -> list[CatalogModel]:
    return [_curated_entry(model) for model in CURATED_MODELS]


def _parse_reasoning_levels(entry: dict) -> tuple[str, ...]:
    # Field name isn't documented for the SIWC catalog; accept Codex-style and plain lists.
    raw = entry.get('supported_reasoning_levels') or entry.get('reasoning_efforts') or []
    levels: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            value = item.get('effort') if isinstance(item, dict) else item
            if isinstance(value, str) and value in SUPPORTED_REASONING_EFFORTS and value not in levels:
                levels.append(value)
    return tuple(sorted(levels, key=SUPPORTED_REASONING_EFFORTS.index))


def parse_plan_models(payload: Any) -> list[PlanModel]:  # noqa: ANN401 - untrusted JSON
    """Parse the SIWC catalog: `{"models": [{"slug", "display_name", "visibility", ...}]}`."""
    entries = payload.get('models') if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        msg = 'Unexpected model catalog shape'
        raise CatalogUnavailableError(msg)

    models: list[PlanModel] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('visibility') != 'list':
            continue
        slug = entry.get('slug')
        if not isinstance(slug, str) or not slug.strip():
            continue
        display_name = entry.get('display_name')
        default = entry.get('default_reasoning_level') or entry.get('default_reasoning_effort')
        models.append(
            PlanModel(
                slug=slug.strip(),
                display_name=display_name.strip() if isinstance(display_name, str) and display_name.strip() else slug,
                reasoning_efforts=_parse_reasoning_levels(entry),
                default_reasoning_effort=default if isinstance(default, str) else None,
            ),
        )
    return models


def merge_plan_catalog(plan_models: Iterable[PlanModel]) -> list[CatalogModel]:
    """Curated models first (disabled when not on the plan), then the plan's other models."""
    by_slug = {model.slug: model for model in plan_models}
    catalog: list[CatalogModel] = []
    for curated in CURATED_MODELS:
        plan_model = by_slug.pop(curated.codex_model, None)
        entry = _curated_entry(curated)
        if plan_model is None:
            entry = replace(entry, available=False, unavailable_reason=NOT_ON_PLAN_REASON)
        elif plan_model.reasoning_efforts:
            efforts = tuple(e for e in curated.reasoning_efforts if e in plan_model.reasoning_efforts)
            efforts = efforts or plan_model.reasoning_efforts
            entry = replace(
                entry,
                reasoning_efforts=efforts,
                default_reasoning_effort=_default_effort(efforts, plan_model.default_reasoning_effort),
            )
        catalog.append(entry)

    for plan_model in by_slug.values():
        efforts = plan_model.reasoning_efforts or DEFAULT_DISCOVERED_REASONING_EFFORTS
        catalog.append(
            CatalogModel(
                id=plan_model.slug,
                label=plan_model.display_name,
                codex_model=plan_model.slug,
                reasoning_efforts=efforts,
                default_reasoning_effort=_default_effort(efforts, plan_model.default_reasoning_effort),
                source='discovered',
            ),
        )
    return catalog


def is_discoverable_api_model(model_id: str) -> bool:
    if not _API_MODEL_RE.match(model_id) or _DATED_SNAPSHOT_RE.search(model_id):
        return False
    parts = model_id.split('-')
    return not any(part in parts for part in _API_MODEL_EXCLUDED_PARTS)


def parse_api_models(payload: Any) -> list[str]:  # noqa: ANN401 - untrusted JSON
    """Parse the standard API model list: `{"data": [{"id": ...}]}`, keeping likely agentic models."""
    entries = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        msg = 'Unexpected model list shape'
        raise CatalogUnavailableError(msg)
    ids = {entry.get('id') for entry in entries if isinstance(entry, dict)}
    return sorted(model_id for model_id in ids if isinstance(model_id, str) and is_discoverable_api_model(model_id))


def merge_api_catalog(model_ids: Iterable[str]) -> list[CatalogModel]:
    catalog = curated_catalog()
    known = {model.codex_model for model in CURATED_MODELS} | {model.id for model in CURATED_MODELS}
    catalog.extend(
        CatalogModel(
            id=model_id,
            label=model_id,
            codex_model=model_id,
            reasoning_efforts=DEFAULT_DISCOVERED_REASONING_EFFORTS,
            default_reasoning_effort=_default_effort(DEFAULT_DISCOVERED_REASONING_EFFORTS),
            source='discovered',
        )
        for model_id in model_ids
        if model_id not in known
    )
    return catalog


async def fetch_models(bearer: str) -> object:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(MODELS_URL, headers={'Authorization': f'Bearer {bearer}'})
    except httpx.HTTPError as err:
        msg = f'Model list request failed ({type(err).__name__})'
        raise CatalogUnavailableError(msg) from err
    if response.status_code != httpx.codes.OK:
        msg = f'Model list request returned {response.status_code}'
        raise CatalogUnavailableError(msg)
    try:
        return orjson.loads(response.content)
    except orjson.JSONDecodeError as err:
        msg = 'Model list response was not JSON'
        raise CatalogUnavailableError(msg) from err


class CatalogCache:
    """Per-process TTL cache keyed by user (plan catalogs) or a hash of the API key."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, list[CatalogModel]]] = {}

    @staticmethod
    def api_key_cache_key(api_key: str) -> str:
        return 'key:' + hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def plan_cache_key(user_id: str) -> str:
        return f'plan:{user_id}'

    async def get(
        self,
        key: str,
        loader: Callable[[], Awaitable[list[CatalogModel]]],
        *,
        ttl_seconds: int,
    ) -> list[CatalogModel]:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
        catalog = await loader()
        if len(self._entries) > 1024:  # noqa: PLR2004
            self._entries.clear()
        self._entries[key] = (now, catalog)
        return catalog

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)


catalog_cache = CatalogCache()


def find_model(catalog: Iterable[CatalogModel], model_id: str) -> CatalogModel | None:
    return next((model for model in catalog if model.id == model_id), None)

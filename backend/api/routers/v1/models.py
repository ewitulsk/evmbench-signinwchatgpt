from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from api.core.config import settings
from api.core.deps import TokenDep, get_db
from api.core.impl import chatgpt_auth
from api.core.model_catalog import (
    CatalogModel,
    CatalogUnavailableError,
    catalog_cache,
    curated_catalog,
    fetch_models,
    merge_api_catalog,
    merge_plan_catalog,
    parse_api_models,
    parse_plan_models,
)
from api.core.tokens import Token
from api.siwc.credentials import PlanUsageUnavailableError, ReauthRequiredError
from api.siwc.oauth import OAuthError


router = APIRouter(prefix='/models', tags=['models'])
DbSessionDep = Annotated[AsyncSession, Depends(get_db)]
BillingSource = Literal['api_key', 'chatgpt_plan']

RECONNECT_REASON = 'Reconnect ChatGPT to load your plan models'
PLAN_UNAVAILABLE_REASON = 'ChatGPT plan usage is not enabled for your account'


class ModelOut(BaseModel):
    id: str
    label: str
    reasoning_efforts: list[str]
    default_reasoning_effort: str | None
    source: Literal['curated', 'discovered']
    available: bool
    unavailable_reason: str | None

    @classmethod
    def from_catalog(cls, model: CatalogModel) -> 'ModelOut':
        return cls(
            id=model.id,
            label=model.label,
            reasoning_efforts=list(model.reasoning_efforts),
            default_reasoning_effort=model.default_reasoning_effort,
            source='discovered' if model.source == 'discovered' else 'curated',
            available=model.available,
            unavailable_reason=model.unavailable_reason,
        )


class ModelsResponse(BaseModel):
    billing_source: BillingSource
    models: list[ModelOut]


class ApiKeyModelsRequest(BaseModel):
    openai_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


async def plan_catalog(session: AsyncSession, token: Token) -> list[CatalogModel]:
    """The signed-in user's ChatGPT plan catalog, merged with the curated list.

    Raises PlanUsageUnavailableError, ReauthRequiredError, CatalogUnavailableError or OAuthError.
    """
    chatgpt = chatgpt_auth()
    if chatgpt is None or not settings.plan_usage_enabled or token.provider != 'chatgpt':
        raise PlanUsageUnavailableError
    backend, store = chatgpt

    async def load() -> list[CatalogModel]:
        access_token = await store.get_access_token(session, user_id=token.user_id, oauth=backend.oauth)
        catalog = merge_plan_catalog(parse_plan_models(await fetch_models(access_token)))
        if settings.BACKEND_MODEL_DISCOVERY == 'off':
            catalog = [model for model in catalog if model.source == 'curated']
        return catalog

    return await catalog_cache.get(
        catalog_cache.plan_cache_key(token.user_id),
        load,
        ttl_seconds=settings.BACKEND_MODEL_CATALOG_TTL_SECONDS,
    )


async def api_key_catalog(api_key: str | None) -> list[CatalogModel]:
    """Curated models, plus name-filtered models from the API key's model list when discovery is `all`."""
    if settings.BACKEND_MODEL_DISCOVERY != 'all' or not api_key:
        return curated_catalog()

    async def load() -> list[CatalogModel]:
        return merge_api_catalog(parse_api_models(await fetch_models(api_key)))

    try:
        return await catalog_cache.get(
            catalog_cache.api_key_cache_key(api_key),
            load,
            ttl_seconds=settings.BACKEND_MODEL_CATALOG_TTL_SECONDS,
        )
    except CatalogUnavailableError as err:
        logger.info(f'API-key model discovery unavailable: {err}')
        return curated_catalog()


def _all_unavailable(reason: str) -> list[ModelOut]:
    return [
        ModelOut.from_catalog(model).model_copy(update={'available': False, 'unavailable_reason': reason})
        for model in curated_catalog()
    ]


@router.get('')
async def list_models(
    session: DbSessionDep,
    token: TokenDep,
    billing_source: BillingSource = 'api_key',
) -> ModelsResponse:
    if billing_source == 'api_key':
        return ModelsResponse(
            billing_source=billing_source,
            models=[ModelOut.from_catalog(model) for model in curated_catalog()],
        )

    try:
        catalog = await plan_catalog(session, token)
    except ReauthRequiredError:
        return ModelsResponse(billing_source=billing_source, models=_all_unavailable(RECONNECT_REASON))
    except PlanUsageUnavailableError:
        return ModelsResponse(billing_source=billing_source, models=_all_unavailable(PLAN_UNAVAILABLE_REASON))
    except (CatalogUnavailableError, OAuthError) as err:
        logger.warning(f'Unable to load ChatGPT plan catalog for user_id={token.user_id}: {err}')
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail='Unable to load your ChatGPT plan models; try again shortly',
        ) from err
    return ModelsResponse(billing_source=billing_source, models=[ModelOut.from_catalog(m) for m in catalog])


@router.post('')
async def list_api_key_models(body: ApiKeyModelsRequest, token: TokenDep) -> ModelsResponse:  # noqa: ARG001
    catalog = await api_key_catalog(body.openai_key)
    return ModelsResponse(billing_source='api_key', models=[ModelOut.from_catalog(m) for m in catalog])

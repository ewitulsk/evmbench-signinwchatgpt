from fastapi import APIRouter

from api.core.config import settings
from api.core.impl import auth_backend
from api.schemas.integration import FrontendConfig


router = APIRouter(prefix='/integration', tags=['integration'])


@router.get('/frontend')
async def frontend_config() -> FrontendConfig:
    return FrontendConfig(
        auth_enabled=bool(auth_backend),
        key_predefined=settings.BACKEND_STATIC_OAI_KEY is not None or settings.BACKEND_USE_PROXY_STATIC_KEY,
        auth_provider=auth_backend.provider if auth_backend else None,
        plan_usage_available=settings.plan_usage_enabled,
        api_key_mode_available=not settings.plan_usage_enabled or settings.BACKEND_API_KEY_MODE_ENABLED,
        model_discovery=settings.BACKEND_MODEL_DISCOVERY,
        manage_usage_url=settings.SIWC_MANAGE_USAGE_URL,
        plan_usage_learn_more_url=settings.SIWC_LEARN_MORE_URL,
    )

from typing import Literal

from pydantic import BaseModel


class FrontendConfig(BaseModel):
    auth_enabled: bool
    key_predefined: bool
    auth_provider: str | None = None
    plan_usage_available: bool = False
    api_key_mode_available: bool = True
    model_discovery: Literal['off', 'plan_only', 'all'] = 'off'
    manage_usage_url: str = ''
    plan_usage_learn_more_url: str = ''

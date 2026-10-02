from typing import Literal

from pydantic import BaseModel


class UserObject(BaseModel):
    avatar_url: str | None
    username: str
    provider: str = 'github'
    # ChatGPT-plan billing for this user: connected | declined | reauth_required | unavailable
    plan_usage: Literal['connected', 'declined', 'reauth_required', 'unavailable'] = 'unavailable'

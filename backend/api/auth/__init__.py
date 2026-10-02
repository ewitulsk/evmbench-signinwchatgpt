from .abc import AuthBackendABC
from .chatgpt import ChatGPTAuthBackend
from .github import GithubAuthBackend


auth_backends: dict[str, type[AuthBackendABC]] = {
    'chatgpt': ChatGPTAuthBackend,
    'github': GithubAuthBackend,
}

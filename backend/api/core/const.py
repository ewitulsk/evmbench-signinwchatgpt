from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedModel:
    id: str
    label: str
    # Upstream model slug; keep aligned with backend/worker_runner/model_map.json.
    codex_model: str
    reasoning_efforts: tuple[str, ...]


# Models tested with EVM Bench. Keep aligned with frontend/src/data/models.json (the frontend's offline fallback).
# Non-reasoning mode is disabled for every model.
CURATED_MODELS: tuple[CuratedModel, ...] = (
    CuratedModel('codex-gpt-6-astra', 'GPT-6 Astra', 'gpt-6-astra', ('low', 'medium', 'high', 'xhigh', 'max')),
    CuratedModel('codex-gpt-5.6-sol', 'GPT-5.6 Sol', 'gpt-5.6-sol', ('low', 'medium', 'high', 'xhigh', 'max')),
    CuratedModel('codex-gpt-5.6-terra', 'GPT-5.6 Terra', 'gpt-5.6-terra', ('low', 'medium', 'high', 'xhigh', 'max')),
    CuratedModel('codex-gpt-5.6-luna', 'GPT-5.6 Luna', 'gpt-5.6-luna', ('low', 'medium', 'high', 'xhigh', 'max')),
    CuratedModel('codex-gpt-5.5', 'GPT-5.5', 'gpt-5.5', ('low', 'medium', 'high', 'xhigh')),
    CuratedModel('codex-gpt-5.4', 'GPT-5.4', 'gpt-5.4', ('low', 'medium', 'high', 'xhigh')),
    CuratedModel('codex-gpt-5.3-codex', 'GPT-5.3 Codex', 'gpt-5.3-codex', ('low', 'medium', 'high', 'xhigh')),
    CuratedModel('codex-gpt-5.2', 'GPT-5.2', 'gpt-5.2-2025-12-11', ('low', 'medium', 'high', 'xhigh')),
)

CURATED_MODELS_BY_ID = {model.id: model for model in CURATED_MODELS}
MODEL_REASONING_EFFORTS = {model.id: model.reasoning_efforts for model in CURATED_MODELS}
ALLOWED_MODELS = set(MODEL_REASONING_EFFORTS)

# Every level the worker runner accepts (see worker_runner/run_codex_detect.sh).
SUPPORTED_REASONING_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
# Used for discovered models whose catalog entry doesn't list reasoning levels.
DEFAULT_DISCOVERED_REASONING_EFFORTS = ('low', 'medium', 'high')

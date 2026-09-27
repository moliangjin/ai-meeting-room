"""Product-owned Codex model selection for the ChatGPT-authenticated CLI."""

from __future__ import annotations

import os
from collections.abc import Mapping


CODEX_MODEL_ENV = "AI_MEETING_ROOM_CODEX_MODEL"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
SUPPORTED_CODEX_MODELS = frozenset({DEFAULT_CODEX_MODEL})


class CodexModelConfigurationError(ValueError):
    """An explicit Codex model is incompatible with the supported login path."""

    code = "CODEX_MODEL_UNSUPPORTED_FOR_AUTH"


def resolve_codex_model(environ: Mapping[str, str] | None = None) -> str:
    """Return the product-owned model or fail closed on unsupported overrides."""
    values = os.environ if environ is None else environ
    configured = values.get(CODEX_MODEL_ENV)
    model = DEFAULT_CODEX_MODEL if configured is None else configured.strip()
    if model not in SUPPORTED_CODEX_MODELS:
        raise CodexModelConfigurationError(
            f"当前 ChatGPT 登录方式不支持 Codex 模型“{model or '（空值）'}”；"
            f"请使用受支持的模型 {DEFAULT_CODEX_MODEL}。"
        )
    return model

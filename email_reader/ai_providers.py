"""Presets for LLM provider connections.

Anthropic is native (own SDK). Everything else is OpenAI-compatible (base_url + key),
mirroring how opencode/goose add LiteLLM. base_url is editable in the UI.
"""

from __future__ import annotations

AI_PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "kind": "anthropic",
        "base_url": "",  # native SDK, not used
        "model_hint": "claude-opus-4-8",
        "key_label": "API-ключ Anthropic",
        "deeplink": "https://console.anthropic.com/settings/keys",
        "deeplink_label": "Получить ключ в Anthropic Console",
        "note": "Нативный путь Claude. base_url не нужен.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model_hint": "anthropic/claude-sonnet-4.6",
        "key_label": "API-ключ OpenRouter",
        "deeplink": "https://openrouter.ai/keys",
        "deeplink_label": "Получить ключ в OpenRouter",
        "note": "Сотни моделей через один ключ. id модели в формате provider/model.",
    },
    "routerai": {
        "label": "RouterAI",
        "kind": "openai",
        "base_url": "https://routerai.ru/api/v1",
        "model_hint": "openai/gpt-4o-mini",
        "key_label": "API-ключ RouterAI",
        "deeplink": "https://routerai.ru",
        "deeplink_label": "Получить ключ в RouterAI",
        "note": "Доступ к LLM из РФ. OpenAI-совместимый.",
    },
    "litellm": {
        "label": "LiteLLM",
        "kind": "openai",
        "base_url": "",
        "model_hint": "provider/model",
        "key_label": "API-ключ LiteLLM",
        "deeplink": "",
        "deeplink_label": "",
        "note": "Свой шлюз LiteLLM (OpenAI-совместимый).",
    },
    "openai_custom": {
        "label": "Другой OpenAI-совместимый",
        "kind": "openai",
        "base_url": "",
        "model_hint": "",
        "key_label": "API-ключ",
        "deeplink": "",
        "deeplink_label": "",
        "note": "Любой OpenAI-совместимый шлюз: укажи base_url, ключ и id модели.",
    },
}

"""Registry of settings sections — single source of truth for the tabs + routing.

Adding a section later = drop `coming_soon`, add a `templates/settings/<key>.html`
body, and a data-loading branch in the `/settings/{section}` route.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    icon: str
    coming_soon: bool = False


SECTIONS: list[Section] = [
    Section("spaces", "Пространства", "globe"),
    Section("boards", "Доски", "check"),
    Section("labels", "Метки и правила", "tag"),
    Section("identities", "Идентичности", "user"),
    Section("signatures", "Подписи", "pen"),
    Section("profile", "Память", "star"),  # key kept: /settings/section/profile is linked
    Section("prompts", "Промпты", "prompt"),
    Section("meetings", "Встречи", "mic"),
    Section("general", "Общие", "gear"),
    Section("storage", "Хранилище", "database"),
    Section("ai", "AI / извлечение", "star"),
    Section("security", "Безопасность", "lock"),
    Section("notifications", "Уведомления", "bell", coming_soon=True),
    Section("appearance", "Оформление", "appearance", coming_soon=True),
]

ACTIVE_KEYS = {s.key for s in SECTIONS if not s.coming_soon}
DEFAULT_SECTION = "spaces"

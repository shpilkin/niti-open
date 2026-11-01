"""Доски пространства: что показывать, как называть и в каком порядке.

Доска — это **запрос по колонкам**, а не промпт: типы, статусы, дополнительное условие,
сортировка. Все эти колонки индексированы, поэтому набор параметров можно отдать в
настройки, не трогая разбор писем (docs/ux-tasks.md §17.5). Модель решает только, какой
тип получит карточка; всё, что происходит после, детерминированно.

Пресеты и настраиваемые доски различаются **видом** (`BoardSpec.kind`):

- у пресетов предикат и секции живут в Python (`views.py`) — у них выверенные вручную
  секции и порядок, которые общий фильтр не воспроизводит; в записи пользователь меняет
  имя, иконку, порядок и видимость;
- `filter` — доска, собранная пользователем: типы, условие и сортировка берутся из записи.

Пресет можно скопировать как настраиваемую (`clone_as_filter`) — тогда типы наследуются, а
секции становятся общими. Честный обмен: настраиваемость в цене рукодельных секций.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader.enums import ActionType
from email_reader.models import BoardSpec, Space

# Сколько своих досок разрешаем на пространство. Сайдбар просит счётчики по одной на доску
# при каждом действии с карточкой, поэтому число не бесконечное.
MAX_CUSTOM = 12

FILTER = "filter"


@dataclass(frozen=True)
class Preset:
    kind: str
    name: str
    icon: str
    types: tuple[ActionType, ...] = ()


# Пресеты в порядке появления в сайдбаре. Ключ записи совпадает с видом: старые ссылки
# вида /view/today продолжают работать, а cookie группировки (`board_group_<key>`) не
# сбрасывается у существующих пользователей.
PRESETS: tuple[Preset, ...] = (
    Preset("today", "Сегодня", "sun"),
    Preset("triage", "Разобрать", "inbox"),
    # Доски названы действием, а не состоянием: на «Ответить» каждая карточка просит
    # ровно одного — ответа. «Делегировано» осталось состоянием намеренно: две трети
    # его карточек уже переданы и от владельца не требуют ничего.
    Preset("needreply", "Ответить", "mail", (ActionType.NEED_MY_REPLY,)),
    Preset("waiting", "Жду ответа", "clock", (ActionType.WAITING_FOR_REPLY,)),
    Preset(
        "delegated", "Делегировано", "forward",
        (ActionType.TO_DELEGATE, ActionType.DELEGATED_TASK),
    ),
    Preset(
        "commitments", "Мои задачи", "check",
        (ActionType.MY_COMMITMENT, ActionType.TASK),
    ),
    Preset("snoozed", "Отложенные", "clock"),
    Preset("done", "Архив", "archive"),
)
PRESET_KINDS = tuple(p.kind for p in PRESETS)
BY_KIND = {p.kind: p for p in PRESETS}

# Условия, которые доска может добавить к типам. Закрытый список: условие исполняется
# запросом, а не языковой моделью, поэтому произвольный текст здесь невозможен.
CONDITIONS: dict[str, str] = {
    "": "без условия",
    "due_soon": "срок сегодня или просрочен",
    "no_due": "без срока",
    "candidate": "предложено закрыть",
    "no_owner": "передать некому — ответственный не назначен",
}

SORTS: dict[str, str] = {
    "due_asc": "срок раньше — выше",
    "created_desc": "новые сверху",
    "created_asc": "старые сверху",
}
DEFAULT_SORT = "due_asc"

# Иконки, которые можно выбрать для доски: выбор из списка, а не поле для опечаток
# (`email_reader/icons.py` — источник имён).
ICON_CHOICES = (
    "sun", "inbox", "mail", "clock", "forward", "check", "star", "flag", "tag", "folder",
    "calendar", "mic", "user", "database", "archive", "dot",
)


# Тип, который получает карточка, если её перенести мышью на доску. У пресетов выбор
# исторический (доска на два типа принимает один из них); у своей доски — её единственный
# тип. Доска на несколько типов целью переноса не становится: выбор был бы произвольным.
PRESET_DROP = {
    "needreply": ActionType.NEED_MY_REPLY,
    "waiting": ActionType.WAITING_FOR_REPLY,
    "delegated": ActionType.DELEGATED_TASK,
    "commitments": ActionType.MY_COMMITMENT,
}


def drop_type(spec) -> str:
    """Что означает перенос строки на эту доску: имя типа, «snoozed», «done» или пусто."""
    if spec.kind in PRESET_DROP:
        return str(PRESET_DROP[spec.kind])
    if spec.kind in ("snoozed", "done"):
        return spec.kind
    types = list(spec.types or [])
    return types[0] if len(types) == 1 else ""


def slug() -> str:
    """Ключ своей доски. Не транслитерация имени: ключ уходит в URL и в имя cookie, а имя
    доски пользователь меняет когда хочет — переименование не должно ломать ссылку."""
    return f"b-{uuid.uuid4().hex[:8]}"


async def list_specs(
    session: AsyncSession, space_id: uuid.UUID, *, visible_only: bool = False
) -> list[BoardSpec]:
    q = select(BoardSpec).where(BoardSpec.space_id == space_id)
    if visible_only:
        q = q.where(BoardSpec.hidden.is_(False))
    return list(
        (await session.execute(q.order_by(BoardSpec.position, BoardSpec.name)))
        .scalars().all()
    )


async def get_spec(
    session: AsyncSession, space_id: uuid.UUID, key: str
) -> BoardSpec | None:
    if not key:
        return None
    return (
        await session.execute(
            select(BoardSpec).where(
                BoardSpec.space_id == space_id, BoardSpec.key == key
            )
        )
    ).scalar_one_or_none()


async def seed(session: AsyncSession, space_id: uuid.UUID) -> int:
    """Разложить пресеты в новом пространстве. Идемпотентно: уже существующие не трогаем."""
    have = {s.key for s in await list_specs(session, space_id)}
    added = 0
    for n, preset in enumerate(PRESETS):
        if preset.kind in have:
            continue
        session.add(BoardSpec(
            space_id=space_id, key=preset.kind, kind=preset.kind, name=preset.name,
            icon=preset.icon, position=n, builtin=True,
            types=[str(t) for t in preset.types], sort=DEFAULT_SORT,
        ))
        added += 1
    if added:
        await session.flush()
    return added


async def seed_all(session: AsyncSession) -> int:
    total = 0
    for (space_id,) in await session.execute(select(Space.id)):
        total += await seed(session, space_id)
    return total


async def create_filter(
    session: AsyncSession, space_id: uuid.UUID, *, name: str, icon: str = "dot",
    types: list[str] | None = None, condition: str = "", sort: str = DEFAULT_SORT,
) -> BoardSpec | None:
    """Своя доска. None, когда упёрлись в лимит или не заданы типы — доска без типов
    показывала бы всё и ничего не значила."""
    types = [t for t in (types or []) if t in {str(x) for x in ActionType}]
    if not types:
        return None
    custom = [s for s in await list_specs(session, space_id) if not s.builtin]
    if len(custom) >= MAX_CUSTOM:
        return None
    n = (
        await session.execute(
            select(func.count()).select_from(BoardSpec).where(
                BoardSpec.space_id == space_id
            )
        )
    ).scalar_one()
    spec = BoardSpec(
        space_id=space_id, key=slug(), kind=FILTER, name=name.strip() or "Своя доска",
        icon=icon, position=n, types=types,
        condition=condition if condition in CONDITIONS else "",
        sort=sort if sort in SORTS else DEFAULT_SORT,
    )
    session.add(spec)
    await session.flush()
    return spec


async def clone_as_filter(
    session: AsyncSession, space_id: uuid.UUID, key: str
) -> BoardSpec | None:
    """Скопировать пресет как настраиваемую доску: типы наследуются, секции становятся
    общими. Сам пресет остаётся на месте — его можно потом спрятать."""
    src = await get_spec(session, space_id, key)
    if src is None:
        return None
    types = src.types or [str(t) for t in BY_KIND[src.kind].types] if src.kind in BY_KIND \
        else src.types
    return await create_filter(
        session, space_id, name=f"{src.name} — своя", icon=src.icon, types=list(types or []),
        sort=src.sort or DEFAULT_SORT,
    )


async def update_spec(
    session: AsyncSession, space_id: uuid.UUID, key: str, **fields
) -> BoardSpec | None:
    spec = await get_spec(session, space_id, key)
    if spec is None:
        return None
    if (name := fields.get("name")) is not None and name.strip():
        spec.name = name.strip()
    if (icon := fields.get("icon")) is not None and icon.strip():
        spec.icon = icon.strip()
    if (hidden := fields.get("hidden")) is not None:
        spec.hidden = bool(hidden)
    if (position := fields.get("position")) is not None:
        spec.position = int(position)
    # Предикат пресета живёт в Python — типы, условие и сортировка там не читаются.
    if spec.kind == FILTER:
        if (types := fields.get("types")) is not None:
            allowed = {str(t) for t in ActionType}
            kept = [t for t in types if t in allowed]
            if kept:
                spec.types = kept
        if (condition := fields.get("condition")) is not None:
            spec.condition = condition if condition in CONDITIONS else ""
        if (sort := fields.get("sort")) is not None and sort in SORTS:
            spec.sort = sort
    await session.flush()
    return spec


async def delete_spec(session: AsyncSession, space_id: uuid.UUID, key: str) -> bool:
    """Удалить можно только свою доску. Пресет прячут: он часть словаря приложения — бот,
    MCP и старые ссылки знают его ключ."""
    spec = await get_spec(session, space_id, key)
    if spec is None or spec.builtin:
        return False
    await session.delete(spec)
    await session.flush()
    return True


async def reorder(session: AsyncSession, space_id: uuid.UUID, keys: list[str]) -> None:
    specs = {s.key: s for s in await list_specs(session, space_id)}
    for n, key in enumerate(keys):
        if key in specs:
            specs[key].position = n
    await session.flush()


async def landing_key(session: AsyncSession, space_id: uuid.UUID) -> str:
    """Куда вести, когда запрошенной доски в этом пространстве нет (спрятали, удалили,
    ссылка из другого пространства): первая видимая доска."""
    visible = await list_specs(session, space_id, visible_only=True)
    return visible[0].key if visible else PRESETS[0].kind

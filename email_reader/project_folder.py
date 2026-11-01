"""The on-disk shape of a project folder.

Three writers share this folder: the mail exporter, the meeting exporter and the user. So
ownership is split hard and stated in every generated file:

    <проект>/
      README.md          [авто]  описание проекта, оглавление
      Задачи.md          [авто]  открытые задачи — из встреч и из писем
      Решения.md         [авто]  журнал решений
      Информация.md      [авто]  факты, цифры, контекст
      Встречи/           [авто]  по файлу на встречу
      Переписка/         [авто]  письма и вложения
      Заметки/           [ТЫ]    не читается и не пишется машиной
      Файлы/             [ТЫ]    не читается и не пишется машиной

The three summaries are a projection of the database and are rewritten whole. Meeting
notes are not: when a re-classification drops a project, its note is marked stale rather
than deleted — removing files stays the user's call.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import settings_store
from email_reader.enums import ACTIONABLE_TYPES, LIVE_STATUSES, ActionType
from email_reader.models import (
    ActionItem,
    Meeting,
    Project,
    ProjectMeeting,
    ProjectThread,
)

# Root under which projects without an explicit folder live.
PROJECTS_ROOT_KEY = "projects_root"

MEETINGS_DIR = "Встречи"
MAIL_DIR = "Переписка"
# Never read, never written. Listed so the rule is greppable, not just documented.
USER_DIRS = ("Заметки", "Файлы")

_NOTICE = (
    "> Файл формируется автоматически. Правки здесь будут перезаписаны — "
    "правьте в приложении Niti."
)


class ExportBlocked(RuntimeError):
    """The target folder is not ours to write into (see _ensure_writable)."""


# --- Where a project lives ---


async def get_projects_root(session: AsyncSession) -> str | None:
    return await settings_store.get_value(session, PROJECTS_ROOT_KEY)


def validate_root(raw: str) -> Path:
    """Resolve a projects root typed (or picked) by the user, or raise ExportBlocked.

    Checked when it is SET rather than at the first export: the root is used by every
    project without its own folder, so a bad one fails N exports later, far from the
    action that caused it."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ExportBlocked("Нужен абсолютный путь, например /Users/you/Documents/Проекты.")
    if path.exists() and not path.is_dir():
        raise ExportBlocked(f"{path} — это файл, а не папка.")
    # Not _ensure_writable: that guards ONE project folder. Here a repo at the root would
    # put every project inside it, and the per-folder check would never notice.
    if (path / ".git").exists():
        raise ExportBlocked(
            f"В {path} лежит git-репозиторий — тогда каждый проект окажется внутри него. "
            "Выберите отдельную папку."
        )
    return path


def folder_for(project: Project, root: str | None) -> Path | None:
    """Explicit folder wins; otherwise <projects_root>/<name>. None when neither is set —
    the project simply isn't exported yet."""
    if (project.folder_path or "").strip():
        return Path(project.folder_path).expanduser()
    if (root or "").strip():
        return Path(root).expanduser() / _safe_component(project.name)
    return None


async def project_dir(
    session: AsyncSession, project: Project, *, create: bool = False
) -> Path | None:
    base = folder_for(project, await get_projects_root(session))
    if base is None:
        return None
    if create:
        _ensure_writable(base)
        base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_component(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", (name or "").strip()) or "Проект"
    return cleaned[:80]


def _ensure_writable(base: Path) -> None:
    """Refuse to generate files inside a source repository.

    Project folders sit next to code repositories in practice, and this writer overwrites
    README.md unconditionally — pointed at a repo it would silently eat the repo's own
    README on the next export."""
    if (base / ".git").exists():
        raise ExportBlocked(
            f"В папке {base} лежит git-репозиторий — Niti туда не пишет. "
            "Выберите для проекта отдельную папку."
        )


# --- Generated files ---


def write_auto(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{_NOTICE}\n\n{body.rstrip()}\n", encoding="utf-8")


def write_readme(project: Project, base: Path) -> None:
    body = (
        f"## Описание (промт)\n\n{project.prompt or '(пусто)'}\n\n"
        "## Что где лежит\n\n"
        "- `Задачи.md` — открытые задачи и поручения (из писем и встреч)\n"
        f"- `Решения.md` — что решили\n"
        f"- `Информация.md` — факты, цифры, контекст\n"
        f"- `{MEETINGS_DIR}/` — заметки о встречах\n"
        f"- `{MAIL_DIR}/` — письма и вложения\n"
        f"- `{USER_DIRS[0]}/`, `{USER_DIRS[1]}/` — ваши файлы, Niti их не трогает\n"
    )
    write_auto(base / "README.md", f"Проект: {project.name}", body)


def _fmt_date(value: datetime | None) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M") if value else "?"


def _fmt_item(item: ActionItem, *, checkbox: bool) -> str:
    bits = [item.title]
    if item.counterparty:
        bits.append(f"— {item.counterparty}")
    if item.due_date:
        bits.append(f"(срок {item.due_date.isoformat()})")
    mark = "- [ ] " if checkbox else "- "
    line = mark + " ".join(bits)
    at = re.match(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", item.reason or "")
    return f"{line} `{at.group(1)}`" if at else line


async def items_for_project(
    session: AsyncSession, project_id: uuid.UUID
) -> list[ActionItem]:
    """Everything extracted from this project's threads and meetings, newest first."""
    threads = select(ProjectThread.thread_id).where(ProjectThread.project_id == project_id)
    meetings_ = (
        select(ProjectMeeting.meeting_id)
        .join(Meeting, Meeting.id == ProjectMeeting.meeting_id)
        .where(ProjectMeeting.project_id == project_id, Meeting.deleted_at.is_(None))
    )
    rows = await session.execute(
        select(ActionItem)
        .where(
            or_(
                ActionItem.source_thread_id.in_(threads),
                ActionItem.source_meeting_id.in_(meetings_),
            )
        )
        .order_by(ActionItem.created_at.desc())
    )
    return list(rows.scalars().all())


async def write_summaries(session: AsyncSession, project: Project, base: Path) -> None:
    """Rewrite the three summaries from the database. They are a projection, not storage —
    which is exactly why they carry the «will be overwritten» notice."""
    items = await items_for_project(session, project.id)

    open_tasks = [
        i for i in items
        if i.type in ACTIONABLE_TYPES and i.status in LIVE_STATUSES
    ]
    open_tasks.sort(key=lambda i: (i.due_date is None, i.due_date or i.created_at))
    decisions = [i for i in items if i.type == ActionType.DECISION]
    infos = [i for i in items if i.type == ActionType.INFO]

    write_auto(
        base / "Задачи.md", "Задачи",
        "\n".join(_fmt_item(i, checkbox=True) for i in open_tasks) or "_Пока пусто._",
    )
    write_auto(
        base / "Решения.md", "Решения",
        "\n".join(_fmt_item(i, checkbox=False) for i in decisions) or "_Пока пусто._",
    )
    write_auto(
        base / "Информация.md", "Информация",
        "\n".join(_fmt_item(i, checkbox=False) for i in infos) or "_Пока пусто._",
    )


# --- Meeting notes ---


def note_name(meeting: Meeting) -> str:
    when = meeting.started_at.astimezone().strftime("%Y-%m-%d %H%M") if meeting.started_at else "?"
    title = _safe_component(meeting.title or "Встреча")[:60]
    return f"{when} {title}.md"


def _yaml_list(values) -> str:
    return "[" + ", ".join(str(v).replace("\n", " ") for v in values) + "]"


def render_meeting_note(
    meeting: Meeting,
    project: Project,
    membership: ProjectMeeting,
    items: list[ActionItem],
    project_names: list[str],
    digest: str | None,
    links: dict[str, str] | None = None,
) -> str:
    """One note per (meeting, project): same front matter, different text — each says what
    the meeting means for THAT project. The transcript itself is never copied here; the
    archive stays the single original (§4.4)."""
    duration = f"{meeting.duration_sec // 60}m" if meeting.duration_sec else "?"
    front = [
        "---",
        f"meeting_id: {meeting.ext_id}",
        f"date: {_fmt_date(meeting.started_at)}",
        f"duration: {duration}",
        f"participants: {_yaml_list(meeting.participants or [])}",
        f"projects: {_yaml_list(project_names)}",
        f"source: {membership.source}",
    ]
    if membership.confidence is not None:
        front.append(f"confidence: {membership.confidence:.2f}")
    if membership.reason:
        front.append(f'reason: "{membership.reason.replace(chr(34), chr(39))}"')
    if meeting.transcript_path:
        front.append(f"transcript: {meeting.transcript_path}")
    front.append("---")

    body = [
        "",
        f"# {meeting.title or 'Встреча'} — {_fmt_date(meeting.started_at)}",
        "",
        _NOTICE,
    ]
    # Два разных блока, а не один. Общее — чтобы понять, куда этот фрагмент попал;
    # фрагмент — то единственное, ради чего заметка лежит в ЭТОЙ папке. Расшифровка
    # сюда не копируется: оригинал остаётся в архиве, здесь только ссылка на него.
    if meeting.summary:
        body.extend(["", "## О чём была встреча целиком", "", meeting.summary.strip()])
    body.extend([
        "",
        f"## Что из этого относится к проекту «{project.name}»",
        "",
        (digest or "_Модель не выделила фрагмент по этому проекту._").strip(),
    ])

    def section(header: str, rows: list[ActionItem], *, checkbox: bool) -> None:
        if not rows:
            return
        body.extend(["", f"## {header}", ""])
        body.extend(_fmt_item(i, checkbox=checkbox) for i in rows)

    section("Задачи", [i for i in items if i.type in ACTIONABLE_TYPES], checkbox=True)
    section("Решения", [i for i in items if i.type == ActionType.DECISION], checkbox=False)
    section("Информация", [i for i in items if i.type == ActionType.INFO], checkbox=False)

    if links:
        body.extend(["", " · ".join(f"[{name}]({href})" for name, href in links.items())])
    return "\n".join(front + body).rstrip() + "\n"


_STALE_WARNING = (
    "> Эта встреча больше не относится к проекту. Файл оставлен как есть — удалите его "
    "вручную, если он не нужен."
)


def mark_note_stale(path: Path) -> bool:
    """A re-classification that drops a project leaves its note in place, flagged. Deleting
    a file the user may have come to rely on is not the classifier's call (§9.3)."""
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if "stale: true" in text:
        return False
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            text = text[:end] + "\nstale: true" + text[end:]
    path.write_text(text.replace(_NOTICE, f"{_STALE_WARNING}\n\n{_NOTICE}", 1), encoding="utf-8")
    return True

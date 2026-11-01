"""Score extraction against ground truth.

A case = rendered thread messages + the expected actionable items. The golden set
(hand-labelled JSON) plus user corrections harvested into eval_samples both feed this.
Matching key is (type, normalized counterparty) — recall on commitments/need-reply is
weighted as the costly miss (a dropped commitment is worse than a false positive).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from email_reader import profiles, prompts
from email_reader import spaces as spaces_svc
from email_reader.ai.extractor import extract
from email_reader.ai.prompt import profile_block
from email_reader.commitment.engine import render_messages
from email_reader.enums import ACTIONABLE_TYPES
from email_reader.identities import get_identity_set
from email_reader.memory import context as memory_ctx
from email_reader.models import Email, EvalSample, Thread


@dataclass
class Case:
    name: str
    messages: list[dict]
    today: date
    expected: list[dict]  # [{type, counterparty}]
    # The system prompt the extractor would see in production for this thread (space
    # prompt + profile + memory); None = the bare default prompt.
    system_prompt: str | None = None


@dataclass
class TypeScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class Report:
    per_type: dict[str, TypeScore] = field(default_factory=dict)
    overall: TypeScore = field(default_factory=TypeScore)

    def render(self) -> str:
        lines = ["type                 prec   recall  f1     tp fp fn"]
        for t, s in sorted(self.per_type.items()):
            lines.append(
                f"{t:<20} {s.precision:.2f}   {s.recall:.2f}    {s.f1:.2f}   "
                f"{s.tp:>2} {s.fp:>2} {s.fn:>2}"
            )
        o = self.overall
        lines.append(
            f"{'OVERALL':<20} {o.precision:.2f}   {o.recall:.2f}    {o.f1:.2f}   "
            f"{o.tp:>2} {o.fp:>2} {o.fn:>2}"
        )
        return "\n".join(lines)


def _key(type_: str, counterparty) -> tuple[str, str]:
    return (str(type_), (counterparty or "").strip().lower())


def _actionable(items: list[dict]) -> set[tuple[str, str]]:
    out = set()
    for it in items:
        t = it.get("type")
        if t in {x.value for x in ACTIONABLE_TYPES}:
            out.add(_key(t, it.get("counterparty")))
    return out


async def score_cases(cases: list[Case], rm) -> Report:
    """`rm` — a resolved model connection (model_connections.ResolvedModel)."""
    report = Report()
    for case in cases:
        result, _ = await extract(
            case.messages, case.today, kind=rm.kind, base_url=rm.base_url,
            api_key=rm.api_key, model=rm.model, system_prompt=case.system_prompt,
            no_thinking=rm.no_thinking,
        )
        predicted = _actionable([i.model_dump() for i in result.items])
        expected = _actionable(case.expected)

        for key in predicted | expected:
            ts = report.per_type.setdefault(key[0], TypeScore())
            in_pred, in_exp = key in predicted, key in expected
            if in_pred and in_exp:
                ts.tp += 1
                report.overall.tp += 1
            elif in_pred:
                ts.fp += 1
                report.overall.fp += 1
            else:
                ts.fn += 1
                report.overall.fn += 1
    return report


def load_golden(directory: str | Path) -> list[Case]:
    """Load hand-labelled cases.

    Each JSON file: {name, today, messages:[{is_me, who, sent_at, subject, body}],
    expected:[{type, counterparty}]}.
    """
    cases: list[Case] = []
    for path in sorted(Path(directory).glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        msgs = []
        for m in raw["messages"]:
            msgs.append({**m, "sent_at": datetime.fromisoformat(m["sent_at"])})
        cases.append(
            Case(
                name=raw.get("name", path.stem),
                messages=msgs,
                today=date.fromisoformat(raw["today"]),
                expected=raw.get("expected", []),
            )
        )
    return cases


async def load_db_samples(session: AsyncSession, *, memory: bool = True) -> list[Case]:
    """Cases harvested from the user's corrections (`eval_samples`).

    The latest sample per thread is the current ground truth (each correction re-snapshots
    the whole thread); samples whose thread was purged (thread_id NULL) are skipped; the
    thread is rendered exactly as the extractor sees it, with the system prompt it would
    get in production (space prompt + profile + memory block; `memory=False` drops the
    block so the two can be compared). `today` is the correction date, so relative
    deadlines resolve as they did for the user.
    """
    identities = await get_identity_set(session)
    rows = await session.execute(
        select(EvalSample)
        .where(EvalSample.thread_id.isnot(None))
        .order_by(EvalSample.created_at.desc())
    )
    latest: dict = {}
    for sample in rows.scalars().all():
        latest.setdefault(sample.thread_id, sample)
    cases: list[Case] = []
    for thread_id, sample in latest.items():
        emails = list(
            (
                await session.execute(
                    select(Email).where(Email.thread_id == thread_id).order_by(Email.sent_at)
                )
            ).scalars().all()
        )
        if not emails:
            continue
        thread = await session.get(Thread, thread_id)
        space_id = await spaces_svc.space_of_account(
            session, thread.account_id if thread else None
        )
        system = await prompts.get_commitment_prompt(session, space_id)
        system += profile_block(await profiles.core(session, space_id))
        if memory:
            ctx = await memory_ctx.for_thread(
                session, thread_id=thread_id, emails=emails, identities=identities,
                space_id=space_id,
            )
            system += ctx.render("extract")
        cases.append(
            Case(
                name=f"db:{thread_id}",
                messages=render_messages(emails),
                today=sample.created_at.date(),
                expected=(sample.expected or {}).get("items", []),
                system_prompt=system,
            )
        )
    return cases

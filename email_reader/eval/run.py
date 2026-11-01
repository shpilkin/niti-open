"""Run the offline eval and print precision/recall.

Usage: python -m email_reader.eval.run [golden_dir] [--db] [--no-memory]

  --db          also score the user's own corrections harvested into `eval_samples`. Every
                card the user fixed becomes a regression case, scored with the system prompt
                the extractor gets in production (space prompt + profile + memory).
  --no-memory   build those prompts without the memory block — run both ways to see what
                the context builder changes.

The model is the active connection from Settings → AI (keys live in the app database).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from email_reader.eval.scorer import load_db_samples, load_golden, score_cases

DEFAULT_DIR = Path(__file__).resolve().parent / "golden"


async def main() -> None:
    from email_reader.db import session_scope
    from email_reader.model_connections import resolve_active

    argv = sys.argv[1:]
    positional = [a for a in argv if not a.startswith("--")]
    directory = Path(positional[0]) if positional else DEFAULT_DIR
    cases = load_golden(directory)
    print(f"{len(cases)} golden case(s) from {directory}")
    async with session_scope() as session:
        rm = await resolve_active(session)
        if "--db" in argv:
            db_cases = await load_db_samples(session, memory="--no-memory" not in argv)
            print(f"{len(db_cases)} case(s) from eval_samples")
            cases += db_cases
    if not cases:
        print("Nothing to score")
        return
    print(f"model: {rm.model}\n")
    report = await score_cases(cases, rm)
    print(report.render())


if __name__ == "__main__":
    asyncio.run(main())

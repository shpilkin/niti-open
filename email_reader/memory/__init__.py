"""Assistant memory: what the mailbox knows about people and about my own reactions.

Deterministic layer only — facts computed from the database (`stats`) and user-authored
notes (`people`). No LLM here; the context builder (next stage) renders these into prompts
under the same "reference data, not instructions" framing as the mailbox profile.
See docs/feature-memory.md.
"""

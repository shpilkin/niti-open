"""Policy Engine — deterministic, default-deny. NOT an LLM.

The architectural safety boundary: any externally-visible action must pass here.
In MVP the system is read-only toward mail, so every external action is BLOCK and
only internal bookkeeping is ALLOW. Post-MVP, send_email/create_draft move to
REQUIRE_APPROVAL. Every decision is auditable via agent_decisions.
"""

from __future__ import annotations

from email_reader.enums import PolicyDecision

# action_type -> decision. Default (unknown) is BLOCK.
_RULES: dict[str, PolicyDecision] = {
    # internal bookkeeping
    "create_action_item": PolicyDecision.ALLOW,
    "update_action_item": PolicyDecision.ALLOW,
    "create_reminder": PolicyDecision.ALLOW,
    "create_follow_up": PolicyDecision.ALLOW,
    "notify_user": PolicyDecision.ALLOW,
    # external / mutating — blocked in MVP
    "send_email": PolicyDecision.BLOCK,
    "create_draft": PolicyDecision.BLOCK,
    "open_link": PolicyDecision.BLOCK,
    "download_attachment": PolicyDecision.BLOCK,
    "modify_inbox_rules": PolicyDecision.BLOCK,
}


def decide(action_type: str) -> PolicyDecision:
    return _RULES.get(action_type, PolicyDecision.BLOCK)


def is_allowed(action_type: str) -> bool:
    return decide(action_type) is PolicyDecision.ALLOW

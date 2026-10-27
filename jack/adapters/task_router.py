"""Task-turn router for adapter v2 (Door 3 hybrid).

Pure logic — no network. Decides whether a call turn should ALSO run on the
full Hermes agent (via the Mac bridge) and extracts the task payload.

Two detection layers (in order):
1. Self-tag: the small brain ends a task-intent turn with ``[TASK: ...]``
   (injected via the system-prompt suffix). Zero extra latency, brain-owned.
2. Heuristic fallback: imperative-verb patterns on the caller's transcript,
   for when the brain forgets to tag. Conservative — false positives cost a
   ~15-60s Hermes detour, false negatives cost a "can't do that".
"""

from __future__ import annotations

import re

TASK_TAG_RE = re.compile(r"\[TASK\s*[:：]\s*(.+?)\]", re.DOTALL)

# Conservative imperative heuristics. Matched against the caller's text,
# case-insensitively, as whole-ish words.
_HEURISTIC_PATTERNS = [
    r"\bwhat(?:'s| is) the (?:time|date)\b",
    r"\bwhat time\b",
    r"\bwhat(?:'s| is) (?:today|the day)\b",
    r"\bremind me\b",
    r"\bset (?:an? )?(?:alarm|timer|reminder)\b",
    r"\blook ?up\b",
    r"\bsearch (?:for|the web)\b",
    r"\bgoogle\b",
    r"\bcheck (?:my |the )?(?:calendar|mail|email|inbox|battery|disk)\b",
    r"\bsend (?:an? )?(?:email|mail|message|text)\b",
    r"\bwrite (?:and send )?an? (?:email|mail)\b",
    r"\bcalculate\b",
    r"\bconvert\b",
    r"\bhow (?:much|many) .*(?:cost|left|free|used)\b",
    r"\bweather\b",
    r"\brun (?:the )?(?:command|script)\b",
    r"\bon the (?:mac|server|computer)\b",
]

_HEURISTIC_RE = re.compile("|".join(_HEURISTIC_PATTERNS), re.IGNORECASE)


def extract_task(reply: str) -> tuple[str | None, str]:
    """Split a brain reply into (task, spoken_prefix).

    Returns (None, reply-unchanged) when no tag is present. The tag is
    stripped from the spoken text either way; leading/trailing whitespace
    around the removal is collapsed.
    """
    m = TASK_TAG_RE.search(reply)
    if not m:
        return None, reply
    task = m.group(1).strip()
    spoken = re.sub(r"\s+", " ", reply[: m.start()] + " " + reply[m.end():]).strip()
    return (task or None), spoken


def heuristic_is_task(text: str) -> bool:
    """True if the caller's transcript looks like an actionable request."""
    return bool(_HEURISTIC_RE.search(text or ""))


def build_context_envelope(history: list[dict], turns: int = 6) -> str:
    """Serialize the last ``turns`` call turns for the Hermes prompt.

    ``history`` is the adapter's message list (system first, then user/
    assistant alternation). The system prompt is excluded; only the tail is
    kept so the envelope stays small. Output is plain "Caller:"/"Agent:" lines.
    """
    msgs = [m for m in history if m.get("role") in ("user", "assistant")]
    tail = msgs[-turns:]
    lines = []
    for m in tail:
        who = "Caller" if m["role"] == "user" else "Agent"
        content = re.sub(r"\[TASK\s*[:：].*?\]", "", m.get("content", ""), flags=re.DOTALL).strip()
        if content:
            lines.append(f"{who}: {content}")
    return "\n".join(lines)


def build_task_prompt(task: str, envelope: str, caller: str = "the user") -> str:
    """Compose the one-shot Hermes query for a call task."""
    parts = [
        "You are the agent's full brain, briefly stepping in on a live phone call with "
        f"{caller}. The caller asked for something that needs REAL tools on this "
        "Mac — do it now, then answer for speech.",
        "",
        "Recent call context (for pronouns and follow-ups only):",
        envelope or "(call just started — no prior turns)",
        "",
        f"Current request from {caller}: {task}",
        "",
        "Rules: use the terminal/files as needed to get a VERIFIED answer. "
        "Reply with 1-3 short spoken sentences (no markdown, no lists, no emoji) "
        "containing the concrete result. If a tool fails, say what failed in "
        "plain words — never claim success without the result in hand.",
    ]
    return "\n".join(parts)

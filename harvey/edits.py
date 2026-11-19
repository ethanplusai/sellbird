"""Learning writing rules from the owner's Outbox edits.

When the owner rewrites a draft in the Outbox, the dashboard logs the text it
replaced (an ``outbox_edited`` action) and the outbox row keeps the new text.
Once per heartbeat, each new edit is compared with what Harvey wrote, and
the difference becomes a rule: a new one, or an existing rule updated
(sharpened, widened, or replaced when the owner changed their mind).

The rules live in ``skills/learned_rules.md``, a plain file the owner can
read, edit or delete. Each rule says which emails it's for (every email,
email 1, 2 or 3, or replies) and how many edits back it up. The writer and
the reply handler read the file every time they write.

Trivial edits (a typo, one word) say nothing about style and are skipped.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from pathlib import Path

from harvey.paths import PROJECT_ROOT
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger(__name__)

RULES_PATH = PROJECT_ROOT / "skills" / "learned_rules.md"
MARKER_KEY = "learned_edits_until"   # rowid of the last edit learned from
BATCH = 5                            # edits learned from per heartbeat
MAX_RULES = 30
EXAMPLE_CHARS = 1500
# Bodies at least this similar (0..1) with the same subject are a typo fix.
TRIVIAL_SIMILARITY = 0.97
APPLIES = ("all", "email 1", "email 2", "email 3", "reply")

HEADER = """# Learned from your Outbox edits

Harvey writes these rules itself: each time you edit a draft in the Outbox,
it compares your version with its own and adds a rule here or updates one.
The writer and the reply handler follow them. Edit, reword or delete any
line; Harvey keeps your wording. Your own email rules (writer.email_rules)
win where the two disagree.

Format: - [which emails] the rule (edits: how many backed it up)

"""
_LINE = re.compile(r"^\s*-\s*\[([^\]]+)\]\s*(.+?)\s*(?:\(edits?:\s*(\d+)\))?\s*$")


def is_trivial(old_subject: str, old_body: str, new_subject: str, new_body: str) -> bool:
    """True when an edit changed too little to say anything about style."""
    if (old_subject or "").strip() != (new_subject or "").strip():
        return False
    ratio = difflib.SequenceMatcher(None, old_body or "", new_body or "").ratio()
    return ratio >= TRIVIAL_SIMILARITY


# ── the rules file ───────────────────────────────────────────────────────


def parse_rules(text: str) -> list[dict]:
    """[{"applies", "rule", "edits"}] from the rules file's bullet lines."""
    out = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        applies = m.group(1).strip().lower()
        out.append({"applies": applies if applies in APPLIES else "all",
                    "rule": m.group(2).strip(),
                    "edits": int(m.group(3) or 1)})
    return out


def render_rules(rules: list[dict]) -> str:
    lines = [f"- [{r['applies']}] {r['rule']} (edits: {int(r.get('edits') or 1)})"
             for r in rules]
    return HEADER + "\n".join(lines) + ("\n" if lines else "")


def load_rules(path: Path = RULES_PATH) -> list[dict]:
    try:
        return parse_rules(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning(f"Could not read {path.name}: {e}")
        return []


def save_rules(rules: list[dict], path: Path = RULES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render_rules(rules), encoding="utf-8")
    tmp.replace(path)


def rules_block(for_what: str, path: Path = RULES_PATH) -> str:
    """Prompt text with the learned rules that apply ("" if none).

    ``for_what``: "email 1", "email 2", "email 3", "sequence" (all three
    emails) or "reply".
    """
    if for_what == "sequence":
        wanted = {"all", "email 1", "email 2", "email 3"}
    elif for_what == "reply":
        wanted = {"all", "reply"}
    else:
        wanted = {"all", for_what}
    rules = [r for r in load_rules(path) if r["applies"] in wanted]
    if not rules:
        return ""
    rules.sort(key=lambda r: -int(r.get("edits") or 1))
    lines = "\n".join(f"- [{r['applies']}] {r['rule']}" for r in rules)
    return (
        "\n\nRULES LEARNED FROM THE OWNER'S EDITS — the owner rewrote earlier "
        "drafts this way. Follow them so the new draft needs no edit. The "
        "OPERATOR'S EMAIL RULES and STRICT EMAIL RULES win where they differ.\n"
        + lines
    )


# ── reading new edits ────────────────────────────────────────────────────


async def _marker(state) -> int:
    try:
        return int((await state.get_setting(MARKER_KEY)) or 0)
    except (TypeError, ValueError):
        return 0


async def pending_edits(state, limit: int = BATCH) -> tuple[list[dict], int]:
    """Edits not learned from yet, oldest first, and the marker after them.

    Each edit: {"kind", "step", "old_subject", "old_body", "new_subject",
    "new_body"}; trivial ones are left out but still move the marker on.
    """
    marker = await _marker(state)
    async with state._connect() as db:
        async with db.execute(
            "SELECT details_json, rowid FROM actions "
            "WHERE action_type = 'outbox_edited' AND rowid > ? "
            "ORDER BY rowid ASC LIMIT ?", (marker, int(limit)),
        ) as cursor:
            logged = await cursor.fetchall()
        edits, last = [], marker
        for details_json, rowid in logged:
            last = rowid
            try:
                details = json.loads(details_json or "{}")
            except ValueError:
                continue
            outbox_id = details.get("outbox_id")
            if not outbox_id:
                continue
            async with db.execute(
                "SELECT kind, step, subject, body FROM outbox WHERE id = ?",
                (outbox_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                continue
            kind, step, subject, body = row
            old_subject = details.get("old_subject", "")
            old_body = details.get("old_body", "")
            if is_trivial(old_subject, old_body, subject, body):
                continue
            edits.append({"kind": kind or "sequence", "step": step,
                          "old_subject": old_subject, "old_body": old_body,
                          "new_subject": subject or "", "new_body": body or ""})
    return edits, last


async def has_pending_edits(state) -> bool:
    try:
        marker = await _marker(state)
        async with state._connect() as db:
            async with db.execute(
                "SELECT 1 FROM actions WHERE action_type = 'outbox_edited' "
                "AND rowid > ? LIMIT 1", (marker,),
            ) as cursor:
                return await cursor.fetchone() is not None
    except Exception:
        return False


# ── learning ─────────────────────────────────────────────────────────────


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EXAMPLE_CHARS else text[:EXAMPLE_CHARS] + " [...]"


def learning_prompt(rules: list[dict], edits: list[dict]) -> str:
    current = "\n".join(
        f"{i}. [{r['applies']}] {r['rule']} (edits: {r['edits']})"
        for i, r in enumerate(rules, 1)) or "(none yet)"
    shown = []
    for i, e in enumerate(edits, 1):
        which = "a reply" if e["kind"] == "reply" else f"email {e['step'] or 1} of a sequence"
        before = f"Subject: {e['old_subject']}\n\n{_clip(e['old_body'])}"
        after = f"Subject: {e['new_subject']}\n\n{_clip(e['new_body'])}"
        shown.append(f"Edit {i} ({which})\nWhat Harvey wrote:\n"
                     f"{fence(before, 'draft before the owner edit')}\n"
                     f"What the owner changed it to:\n{fence(after, 'the owner edit')}")
    return f"""You maintain the writing rules a sales email writer follows. The
owner just edited some drafts before sending them. Work out what each edit
says about how the owner wants emails written, and update the rules.

CURRENT RULES (the owner may have edited these by hand: keep their wording
unless an edit clearly changes it):
{current}

{chr(10).join(shown)}

{UNTRUSTED_RULE}

How to update:
- For each edit, find the general lesson: length, opening, tone, wording,
  structure, the question, what was cut or added. Ignore typo fixes.
- If an existing rule covers it, update that rule (sharpen or widen it) and
  add 1 to its edits count. If the edit contradicts a rule, rewrite the rule
  to match the owner's latest choice and reset its count to 1. Otherwise add
  a new rule with edits 1.
- Rules must be general and reusable: never include a person's name, a
  company's name, a place, a date or a fact about one prospect.
- Each rule is one short imperative sentence.
- "applies" is one of: all, email 1, email 2, email 3, reply. Use "all"
  only when the lesson clearly holds for every email.
- Keep every current rule the edits don't touch, unchanged. At most
  {MAX_RULES} rules: merge near-duplicates.

Return ONLY JSON: {{"rules": [{{"applies": "...", "rule": "...", "edits": 1}}]}}"""


def _clean_rules(result) -> list[dict] | None:
    items = result.get("rules") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rule = " ".join(str(item.get("rule") or "").split())
        if not rule or len(rule) > 300:
            continue
        applies = str(item.get("applies") or "all").strip().lower()
        try:
            n = max(1, int(item.get("edits") or 1))
        except (TypeError, ValueError):
            n = 1
        out.append({"applies": applies if applies in APPLIES else "all",
                    "rule": rule, "edits": n})
    return out[:MAX_RULES]


async def learn_from_edits(state, brain, path: Path = RULES_PATH) -> int:
    """Turn new Outbox edits into rules. Returns how many edits were learned.

    The marker only moves on when the rules were saved (or there was nothing
    worth learning), so an edit is never lost to a failed model call.
    """
    before = await _marker(state)
    edits, last = await pending_edits(state)
    if not edits:
        if last != before:
            await state.set_setting(MARKER_KEY, str(last))
        return 0
    rules = load_rules(path)
    result = await brain.think_json(
        learning_prompt(rules, edits), session_id="harvey-learn-edits",
        agent="writer", task="learn_from_edits")
    updated = _clean_rules(result)
    if updated is None:
        logger.warning("Edits: couldn't turn the latest Outbox edits into rules; "
                       "will try again next cycle.")
        return 0
    save_rules(updated, path)
    await state.set_setting(MARKER_KEY, str(last))
    logger.info(f"Edits: learned from {len(edits)} Outbox edit(s); "
                f"{len(updated)} rule(s) in {path.name}.")
    return len(edits)

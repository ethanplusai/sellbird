"""Is this inbox ready to send? One check, with one plain answer.

A check signs in (the provider's own test_connection: no email is sent),
reads the sending domain's DNS (MX, SPF, DKIM, DMARC) and looks at where
the inbox is in its warmup. Each line says what passed or what is wrong and
how to fix it, and the result is kept so the Inboxes tab can show it
without checking again on every load.

The verdict folds into one state per inbox (``inbox_state``):

- ``not_connected``: Sellbird has no working connection to build.
- ``paused``: you or Sellbird paused it; nothing goes out from it.
- ``needs_attention``: the last check found something that stops or hurts
  sending (a refused sign-in, a missing SPF or DMARC record...).
- ``warming``: everything passed, and it is still in its warmup days, so
  it sends no cold email yet.
- ``ready``: the last check passed (warnings allowed) and it can send.
- ``not_checked``: never checked, or the last check couldn't finish.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

from sellbird import deliverability as d

HEALTH_KEY = "inbox_health:{}"
SIGN_IN_TIMEOUT = 30
NO_ANSWER = "The mail server didn't answer within 30 seconds."

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"

_DNS_LABELS = {
    "MX": "Replies can arrive (MX)",
    "SPF": "Allowed to send (SPF)",
    "DKIM": "Signed mail (DKIM)",
    "DMARC": "Domain policy (DMARC)",
}


def health_key(mailbox_id: str) -> str:
    return HEALTH_KEY.format(mailbox_id)


def _line(key: str, label: str, status: str, detail: str, fix: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail,
            "fix": fix if status in (WARN, FAIL, UNKNOWN) else ""}


def sign_in_fix(connection: str, provider: str) -> str:
    if connection == "oauth":
        who = "Google" if provider == "google" else "Microsoft"
        return (f"Choose Edit settings, then Sign in again with {who}, as this address. "
                "A sign-in stops working when its password changes or access is removed.")
    if connection == "main":
        return {
            "gmail": "Sign in again with `sellbird gmail auth`, as this address.",
            "microsoft": "Sign in again with `sellbird microsoft auth`, as this address.",
        }.get(provider, "Check the SMTP settings under channels.email in sellbird.yaml and the "
                        "password in .env, then run `sellbird doctor`.")
    return "Choose Edit settings. " + FORM_SIGN_IN_FIX


# Shown in the add and edit forms, where the fields are already in front of you.
FORM_SIGN_IN_FIX = ("Check the server, port, username and password. Gmail and Outlook need an "
                    "app password here, not your usual one.")


def _dns_fix(name: str, status: str, domain: str) -> str:
    if name == "MX":
        return (f"Add the MX records your email provider gives you for {domain} at your DNS "
                "host. Without them, replies and bounces to this address are lost.")
    if name == "SPF":
        return ("At your DNS host, keep one TXT record starting v=spf1 that includes your "
                "email provider (its setup page gives the exact text), ending in ~all.")
    if name == "DKIM":
        return ("Turn on DKIM signing in your email provider's admin and add the TXT record it "
                "gives you. If it uses a custom selector, set channels.email.warmup."
                "dkim_selector in sellbird.yaml so this check finds it.")
    if status == WARN:
        return ("Fine while warming up. Once DMARC reports look clean, change p=none to "
                "p=quarantine.")
    return (f"Add a TXT record named _dmarc.{domain} with v=DMARC1; p=none; "
            f"rua=mailto:postmaster@{domain} and tighten it later.")


def _dns_lines(domain: str, selector: str) -> list[dict]:
    status_of = {d.OK: PASS, d.WARN: WARN, d.FAIL: FAIL, d.INFO: UNKNOWN}
    out = []
    for check in (d.check_mx(domain), d.check_spf(domain), d.check_dkim(domain, selector),
                  d.check_dmarc(domain)):
        status = UNKNOWN if check.unknown else status_of.get(check.status, WARN)
        fix = ("Your computer couldn't reach DNS just now. Check the connection and try again."
               if status == UNKNOWN else _dns_fix(check.name, status, domain))
        out.append(_line(check.name.lower(), _DNS_LABELS[check.name], status,
                         check.detail.replace(" - ", ". "), fix))
    return out


def warmup_line(started: str, min_days: int, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    if not started:
        return _line("warmup", "Warmup", PASS,
                     "No warmup date, so it sends at full volume. Set one in Edit settings if "
                     "this address is new.")
    try:
        day = date.fromisoformat(started[:10])
    except ValueError:
        return _line("warmup", "Warmup", WARN, f"The warmup date {started!r} isn't a date.",
                     "Set it again in Edit settings as a date.")
    days = (today - day).days
    if days < min_days:
        starts = day + timedelta(days=min_days)
        return _line("warmup", "Warmup", WARN,
                     f"Day {max(days, 0)} of {min_days}. No cold email from it until "
                     f"{starts.strftime('%b')} {starts.day}.",
                     "Nothing to fix: keep your warmup service running. Cold email starts on "
                     "its own once the warmup days are over.") | {"warming": True}
    return _line("warmup", "Warmup", PASS, f"Warmed up: day {days} since {day.isoformat()}.")


def verdict(lines: list[dict]) -> str:
    statuses = {line["key"]: line["status"] for line in lines}
    if FAIL in statuses.values():
        return "needs_attention"
    if UNKNOWN in statuses.values():
        return "not_checked"
    if any(line.get("warming") for line in lines):
        return "warming"
    return "ready"


def summary(result: dict) -> str:
    """One line for the list and the pill's tooltip."""
    lines = result.get("checks") or []
    bad = [line["label"] for line in lines if line["status"] == FAIL]
    if bad:
        return "Failed: " + ", ".join(bad) + "."
    unknown = [line["label"] for line in lines if line["status"] == UNKNOWN]
    if unknown:
        return "Couldn't finish: " + ", ".join(unknown) + "."
    if result.get("verdict") == "warming":
        return next(line["detail"] for line in lines if line["key"] == "warmup")
    warns = [line["label"] for line in lines if line["status"] == WARN]
    return "Passed" + (", with a note on " + ", ".join(warns) if warns else "") + "."


async def run_check(sign_in, *, address: str, connection: str, provider_name: str,
                    warmup_started: str, min_days: int, dkim_selector: str = "") -> dict:
    """Check one inbox. ``sign_in`` is an async callable that signs in with
    its provider and returns (ok, detail), never raising, and never sending."""
    try:
        ok, detail = await sign_in()
    except Exception as e:   # a network failure must not 500 the page
        ok, detail = False, f"Couldn't check the sign-in ({type(e).__name__})."
    lines = [_line("signin", "Signs in", PASS if ok else FAIL, detail or "",
                   sign_in_fix(connection, provider_name))]
    domain = (address or "").rpartition("@")[2].strip().lower()
    if domain and d.DOMAIN_RE.match(domain):
        lines += await asyncio.to_thread(_dns_lines, domain, dkim_selector)
    lines.append(warmup_line(warmup_started, min_days))
    result = {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
              "checks": lines}
    result["verdict"] = verdict(lines)
    result["summary"] = summary(result)
    return result


async def save_result(store, mailbox_id: str, result: dict) -> None:
    await store.set_setting(health_key(mailbox_id), json.dumps(result))


async def load_result(store, mailbox_id: str) -> dict | None:
    raw = await store.get_setting(health_key(mailbox_id))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("checks"), list) else None


def inbox_state(*, connected: bool, paused: bool, health: dict | None) -> str:
    if not connected:
        return "not_connected"
    if paused:
        return "paused"
    if not health:
        return "not_checked"
    return health.get("verdict") or "not_checked"

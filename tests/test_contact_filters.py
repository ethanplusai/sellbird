"""People page email-status filter: 'To write' holds only who the Writer can draft for
(a new contact with a verified or catch-all address), the same count the loop
uses. A guessed or missing address is 'Need an email', a draft waiting in the
Outbox is 'Drafted', and an invalid address is closed out.

The filtering runs in app.js; these tests run it under node."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "sellbird" / "web" / "app.js"

PEOPLE = [
    {"id": "v", "status": "new", "email": "v@example.com", "email_status": "verified"},
    {"id": "r", "status": "new", "email": "r@example.com", "email_status": "risky"},
    {"id": "g", "status": "new", "email": "g@example.com", "email_status": "guess"},
    {"id": "n", "status": "new", "email": "", "email_status": ""},
    {"id": "x", "status": "new", "email": "x@example.com", "email_status": "invalid"},
    {"id": "q", "status": "queued", "email": "q@example.com", "email_status": "verified"},
    {"id": "c", "status": "contacted", "email": "c@example.com", "email_status": "verified"},
    {"id": "l", "status": "lost", "email": "l@example.com", "email_status": "verified"},
]


def _ids(filter_key: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    parts = []
    for pattern in (r"^const CONTACT_STATUSES = \[.*?^\];\n",
                    r"^function writable\(.*?\n",
                    r"^function contactStatusMatches\(.*?^}\n"):
        match = re.search(pattern, source, re.S | re.M)
        assert match, pattern
        parts.append(match.group(0))
    script = ("\n".join(parts) + f"\nconst people = {json.dumps(PEOPLE)};\n"
              f"process.stdout.write(JSON.stringify(people.filter(p => "
              f"contactStatusMatches(p, {json.dumps(filter_key)})).map(p => p.id)));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         encoding="utf-8", check=True, timeout=30).stdout
    return json.loads(out)


def test_to_write_is_only_a_verified_or_catch_all_address():
    assert _ids("todo") == ["v", "r"]


def test_a_guessed_or_missing_address_needs_an_email():
    assert _ids("email") == ["g", "n"]


def test_a_draft_in_the_outbox_is_drafted_not_to_write():
    assert _ids("drafted") == ["q"]


def test_an_invalid_address_is_closed_out():
    assert _ids("closed") == ["x", "l"]


def test_every_contact_lands_in_exactly_one_filter():
    keys = ["email", "todo", "drafted", "contacted", "replied", "closed"]
    seen = [pid for key in keys for pid in _ids(key)]
    assert sorted(seen) == sorted(p["id"] for p in PEOPLE)

"""The dashboard's Inboxes routes (sellbird.mailboxes).

Reading every inbox and pausing one are open to the coding agent
(sellbird.agent_policy); resuming, caps, names, removing and choosing a
campaign's inboxes are the user's, and so is connecting one: a Gmail or
Microsoft sign-in opens in the browser on the computer running Sellbird
(the same flow as `sellbird inboxes connect`), and an SMTP password goes
straight to the system keychain. None of these routes is on the agent's
allowlist. No response here carries a credential, a keychain ref or SMTP
settings: rows are built from explicit fields.

The main mailbox has no row; in a path it is `main`.

Each inbox shows one state (sellbird.inbox_health): not connected, paused,
needs attention, warming up, ready, or not checked. "Check connection" runs
the full check (sign-in, the domain's DNS, warmup) and keeps the result; no
check ever sends an email. Editing an inbox's settings checks the new
sign-in before anything is saved, and keeps the same row, so its sent mail
and threads stay with it.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sellbird import inbox_health
from sellbird.mailboxes import (
    HARD_MAX_INBOX_CAP,
    MAIN,
    ConnectRefused,
    MailboxStore,
    TokenTooLarge,
    build_provider,
    check_provider_allows_inboxes,
    check_warmup_start,
    connect_oauth_mailbox,
    connect_smtp_mailbox,
    keychain_token,
    load_inboxes,
    oauth_ref,
    smtp_ref,
)
from sellbird.secrets import SecretStore, SecretStoreUnavailable

logger = logging.getLogger("sellbird.inbox_routes")

router = APIRouter()
MAIN_PATH = "main"


def _refuse(message: str, code: int = 400):
    return JSONResponse({"success": False, "message": message}, status_code=code)


def _store() -> MailboxStore:
    from sellbird import dashboard
    return MailboxStore(db_path=str(dashboard.DB_PATH))


def _secrets() -> SecretStore:
    return SecretStore()


# How to connect the main mailbox, by provider, when it isn't connected.
MAIN_CONNECT_HINTS = {
    "gmail": "Sign in with Google below as this address (or run `sellbird gmail auth`).",
    "microsoft": "Sign in with Microsoft below as this address (or run `sellbird microsoft auth`).",
    "smtp": "Fill in the SMTP settings under channels.email in sellbird.yaml and the "
            "password in .env, then run `sellbird doctor`.",
}


def _config_env():
    from sellbird.config import load_config, load_env
    return load_config(), load_env()


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


async def _row(store: MailboxStore, mailbox_id: str) -> dict | None:
    row = await store.get_mailbox(mailbox_id)
    return row if row and row.get("state") != "retired" else None


@router.get("/api/inboxes")
async def list_inboxes():
    from sellbird.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider

    store = _store()
    await store.init_db()
    config, env = _config_env()
    email = config.channels.email
    native = (email.provider or "").strip().lower() in NATIVE_PROVIDERS
    main = get_mail_provider(config, env) if native else None
    inboxes = await load_inboxes(store, config, env, main)
    rows = {r["id"]: r for r in await store.list_mailboxes()}
    out = []
    for inbox in inboxes.inboxes.values():
        row = rows.get(inbox.id, {})
        health = await inbox_health.load_result(store, MAIN_PATH if inbox.main else inbox.id)
        state = inbox_health.inbox_state(connected=inbox.connected,
                                         paused=bool(inbox.paused_reason), health=health)
        out.append({
            "id": MAIN_PATH if inbox.main else inbox.id,
            "main": inbox.main,
            "address": inbox.address,
            "name": inbox.name,
            "connection": "main" if inbox.main else row.get("connection", ""),
            "provider": email.provider if inbox.main else row.get("provider", ""),
            "state": state,
            "paused_reason": inbox.paused_reason,
            "cold_cap": inbox.cap,
            "cap_is_yours": row.get("cap_override") is not None,
            "cold_sent_today": inbox.sent_today,
            "first_emails_waiting": max(0, inbox.queued_first),
            "warmup_started_at": (_warmup_shape(email).start_date if inbox.main
                                  else row.get("warmup_started_at")) or "",
            "health": health,
            "connect_hint": (MAIN_CONNECT_HINTS.get((email.provider or "").strip().lower(), "")
                             if inbox.main and not inbox.connected else ""),
        })
    paused = await store.get_setting("sending_paused") or ""
    return {
        "inboxes": out,
        "several": inboxes.several,
        "native": native,
        "max_daily_sends": email.max_daily_sends,
        "per_inbox_daily_cap": getattr(email, "per_inbox_daily_cap", 30),
        "pools": {cid: sorted(MAIN_PATH if m == MAIN else m for m in ids)
                  for cid, ids in (await store.campaign_pools()).items()},
        "campaigns": await store._rows(
            "SELECT id, name, status FROM campaigns ORDER BY created_at DESC LIMIT 200"),
        "sending_paused": paused,
        "connect_command": "sellbird inboxes connect gmail|microsoft|smtp --address you@domain",
        "can_connect": _can_connect(env),
        "connect_problems": _connect_problems(env),
        "connecting": _connect_job_view(),
    }


@router.post("/api/inboxes/{mailbox_id}/pause")
async def pause_inbox(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id. The main mailbox pauses with all "
                       "sending, on the Outbox tab.", 404)
    principal = getattr(request.state, "principal", "user")
    reason = ((await _body(request)).get("reason") or "").strip()[:200]
    reason = reason or ("paused by your coding agent" if principal == "agent" else "paused by you")
    await store.set_pause(row["id"], reason)
    await store.log_action(action_type="inbox_paused", agent="agent" if principal == "agent" else "you",
                           details={"inbox": row["address"], "reason": reason})
    return {"success": True, "paused": True, "address": row["address"]}


@router.post("/api/inboxes/{mailbox_id}/resume")
async def resume_inbox(mailbox_id: str):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    await store.set_pause(row["id"], "")
    await store.log_action(action_type="inbox_resumed", agent="you",
                           details={"inbox": row["address"]})
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/cap")
async def set_inbox_cap(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id. The main mailbox's cap is "
                       "channels.email.per_inbox_daily_cap in sellbird.yaml.", 404)
    cap = (await _body(request)).get("cap")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int)
                            or not 1 <= cap <= HARD_MAX_INBOX_CAP):
        return _refuse(f"A cap is a whole number from 1 to {HARD_MAX_INBOX_CAP}, "
                       "or null for the default.")
    await store.set_cap(row["id"], cap)
    await store.log_action(action_type="inbox_cap", agent="you",
                           details={"inbox": row["address"], "cap": cap})
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/name")
async def set_inbox_name(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    name = (await _body(request)).get("name")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in "\r\n<>\""):
        return _refuse("A name is up to 100 characters, on one line, without < > or \".")
    await store.set_display_name(row["id"], name)
    return {"success": True}


@router.post("/api/inboxes/{mailbox_id}/remove")
async def remove_inbox(mailbox_id: str, request: Request):
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    if (await _body(request)).get("confirm") != row["address"]:
        return _refuse("Type the inbox's address to confirm: threads it started end.")
    await store.retire(row["id"])
    await store.delete_setting(inbox_health.health_key(row["id"]))
    await store.log_action(action_type="inbox_removed", agent="you",
                           details={"inbox": row["address"]})
    return {"success": True}


@router.post("/api/campaigns/{campaign_id}/inboxes")
async def set_campaign_inboxes(campaign_id: str, request: Request):
    store = _store()
    await store.init_db()
    if not await store._rows("SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)):
        return _refuse("No campaign with that id.", 404)
    ids = (await _body(request)).get("inboxes")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return _refuse('Expected {"inboxes": ["main", "<inbox id>", ...]} ([] = every inbox).')
    chosen = []
    for i in ids:
        if i == MAIN_PATH:
            chosen.append(MAIN)
        elif await _row(store, i) is not None:
            chosen.append(i)
        else:
            return _refuse(f"No connected inbox {i!r}.", 404)
    await store.set_campaign_pool(campaign_id, chosen)
    return {"success": True}


# ── Connecting an inbox from the dashboard ──
#
# A Gmail or Microsoft sign-in blocks for up to five minutes while the user
# finishes it in the browser, so it runs as one background job; the page
# polls /api/inboxes/connect/status. One sign-in at a time: both flows
# listen on a fixed local port.

_connect_job: dict = {"state": "idle"}
_connect_task: asyncio.Task | None = None


# A real Google OAuth client id: digits, a dash, then .apps.googleusercontent.com.
_GOOGLE_CLIENT_ID = re.compile(r"^\d+-[0-9a-z]+\.apps\.googleusercontent\.com$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _connect_problem(kind: str, env) -> str:
    """Why a browser sign-in can't start yet, or "" when it can. Checked
    before the browser opens, so a missing or mistyped client never sends the
    user to a provider error page."""
    if kind == "gmail":
        client_id = (getattr(env, "gmail_client_id", "") or "").strip()
        if not client_id or not (getattr(env, "gmail_client_secret", "") or "").strip():
            return ("Google sign-in isn't set up yet. Sellbird signs in to Gmail through "
                    "your own Google OAuth client, so it needs its Client ID and secret first.")
        if not _GOOGLE_CLIENT_ID.match(client_id):
            return ("The Google Client ID in Settings isn't a real one. It looks like "
                    "123456789-abc123.apps.googleusercontent.com.")
        return ""
    if kind == "microsoft":
        client_id = (getattr(env, "m365_client_id", "") or "").strip()
        if not client_id:
            return ("Microsoft sign-in isn't set up yet. Sellbird signs in through your own "
                    "Microsoft app registration, so it needs its Application (client) ID first.")
        if not _GUID.match(client_id):
            return ("The Microsoft Application (client) ID in Settings isn't a real one. It "
                    "looks like 1b2c3d4e-5f60-7182-93a4-b5c6d7e8f901.")
        return ""
    return ""


def _can_connect(env) -> dict:
    return {k: not _connect_problem(k, env) for k in ("gmail", "microsoft")}


def _connect_problems(env) -> dict:
    return {k: _connect_problem(k, env) for k in ("gmail", "microsoft")}


def _connect_job_view() -> dict:
    return {k: _connect_job.get(k, "") for k in ("state", "kind", "address", "message", "id")}


async def _test(row: dict, config, env, secrets) -> tuple[bool, str]:
    try:
        provider = build_provider(row, config=config, env=env, secrets=secrets)
    except SecretStoreUnavailable:
        return False, ("The keychain is locked or unavailable, so the sign-in can't be read. "
                       "Unlock it and check again.")
    if provider is None:
        return False, "Sellbird has no working connection for it on this computer."
    try:
        return await asyncio.wait_for(provider.test_connection(),
                                      timeout=inbox_health.SIGN_IN_TIMEOUT)
    except TimeoutError:
        return False, inbox_health.NO_ANSWER
    except Exception as e:  # a network failure must not 500 the page
        return False, f"Couldn't check the connection ({type(e).__name__})."


def _warmup_shape(email):
    from sellbird.config import WarmupConfig
    shape = getattr(email, "warmup", None)
    return shape if isinstance(shape, WarmupConfig) else WarmupConfig()


async def _test_main(config, env) -> tuple[bool, str]:
    from sellbird.integrations.mail_provider import get_mail_provider
    try:
        provider = get_mail_provider(config, env)
        return await asyncio.wait_for(provider.test_connection(),
                                      timeout=inbox_health.SIGN_IN_TIMEOUT)
    except TimeoutError:
        return False, inbox_health.NO_ANSWER
    except Exception as e:  # a network failure must not 500 the page
        return False, f"Couldn't check the connection ({type(e).__name__})."


async def _check(store: MailboxStore, mailbox_id: str, config, env, secrets) -> dict | None:
    """Run the full check on one inbox (`main` for the main mailbox), keep
    the result and return it; None when there's no such inbox."""
    from sellbird.integrations.mail_provider import NATIVE_PROVIDERS

    email = config.channels.email
    shape = _warmup_shape(email)
    if mailbox_id == MAIN_PATH:
        kind = (email.provider or "").strip().lower()
        if kind not in NATIVE_PROVIDERS:
            return None
        result = await inbox_health.run_check(
            lambda: _test_main(config, env),
            address=getattr(config.persona, "email", "") or "",
            connection="main", provider_name=kind, warmup_started=shape.start_date,
            min_days=shape.min_days, dkim_selector=shape.dkim_selector)
    else:
        row = await _row(store, mailbox_id)
        if row is None:
            return None
        result = await inbox_health.run_check(
            lambda: _test(row, config, env, secrets), address=row["address"],
            connection=row.get("connection", ""), provider_name=row.get("provider", ""),
            warmup_started=str(row.get("warmup_started_at") or ""),
            min_days=shape.min_days, dkim_selector=shape.dkim_selector)
    await inbox_health.save_result(store, mailbox_id, result)
    return result


def _sign_in(kind: str, address: str, env, main: bool = False) -> dict | None:
    """The browser sign-in, in a worker thread. An extra inbox's token stays in
    memory for the keychain; the main mailbox's goes to its usual token file,
    exactly as `sellbird gmail auth` / `sellbird microsoft auth` store it."""
    captured: dict = {}
    save = None if main else captured.update
    expected = address or None
    if kind == "gmail":
        from sellbird.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret,
                           save=save, expected_address=expected)
    else:
        from sellbird.integrations.microsoft import run_auth_flow
        ok = run_auth_flow(env.m365_client_id, env.m365_tenant_id,
                           save=save, expected_address=expected)
    if main:
        return {"main": True} if ok else None
    return captured if ok and captured else None


async def _run_main_connect(kind: str, address: str) -> None:
    config, env = _config_env()
    try:
        done = await asyncio.to_thread(_sign_in, kind, address, env, True)
        if done is None:
            _connect_job.update(state="failed", message=(
                "The sign-in didn't finish, or you signed in as a different address. "
                "Nothing changed."))
            return
        store = _store()
        await store.init_db()
        await store.log_action(action_type="inbox_connected", agent="you",
                               details={"inbox": address or "main mailbox", "provider": kind})
        await _check(store, MAIN_PATH, config, env, _secrets())
        _connect_job.update(state="connected", message="", id=MAIN_PATH)
    except Exception:
        logger.exception("Connecting the main mailbox failed")
        _connect_job.update(state="failed", message="Connecting failed. See the dashboard log.")


async def _run_oauth_connect(kind: str, address: str, name: str, warmup: str) -> None:
    config, env = _config_env()
    try:
        token = await asyncio.to_thread(_sign_in, kind, address, env)
        if token is None:
            _connect_job.update(state="failed", message=(
                "The sign-in didn't finish, or you signed in as a different address. "
                "Nothing was connected."))
            return
        store, secrets = _store(), _secrets()
        await store.init_db()
        mailbox_id = await connect_oauth_mailbox(
            store, secrets, config, provider="google" if kind == "gmail" else "microsoft",
            address=address, display_name=name, warmup_started_at=warmup, token=token)
        await store.log_action(action_type="inbox_connected", agent="you",
                               details={"inbox": address, "provider": kind})
        result = await _check(store, mailbox_id, config, env, secrets) or {}
        _connect_job.update(state="connected", message="", id=mailbox_id,
                            verdict=result.get("verdict", ""))
    except (ConnectRefused, SecretStoreUnavailable) as e:
        _connect_job.update(state="failed", message=str(e))
    except Exception:
        logger.exception("Connecting an inbox failed")
        _connect_job.update(state="failed", message="Connecting failed. See the dashboard log.")


def _connect_fields(body: dict) -> tuple[str, str, str]:
    address = body.get("address")
    name = body.get("name") or ""
    warmup = body.get("warmup_started") or ""
    if not isinstance(address, str) or "@" not in address or len(address) > 254:
        raise ConnectRefused("Enter the inbox's full email address.")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in "\r\n<>\""):
        raise ConnectRefused("A name is up to 100 characters, on one line, without < > or \".")
    if not isinstance(warmup, str):
        raise ConnectRefused("Give the warmup start as YYYY-MM-DD.")
    return address.strip().lower(), name.strip(), warmup.strip()


@router.post("/api/inboxes/main/connect")
async def connect_main_mailbox():
    """Sign the main mailbox in from the dashboard: the same browser sign-in
    as `sellbird gmail auth` / `sellbird microsoft auth`."""
    global _connect_task
    config, env = _config_env()
    provider = (config.channels.email.provider or "").strip().lower()
    if provider not in ("gmail", "microsoft"):
        return _refuse("The main mailbox signs in with a browser only for Gmail or "
                       "Microsoft 365. Set channels.email.provider in sellbird.yaml.")
    problem = _connect_problem(provider, env)
    if problem:
        return _refuse(problem)
    if _connect_task is not None and not _connect_task.done():
        return _refuse("A sign-in is already open. Finish it in your browser, or wait "
                       "five minutes for it to time out.", 409)
    address = (getattr(config.persona, "email", "") or "").strip().lower()
    _connect_job.clear()
    _connect_job.update(state="waiting", kind=provider, address=address, message="")
    _connect_task = asyncio.create_task(_run_main_connect(provider, address))
    return {"success": True, "started": True, "kind": provider}


@router.post("/api/inboxes/connect/{kind}")
async def connect_inbox(kind: str, request: Request):
    global _connect_task
    if kind not in ("gmail", "microsoft", "smtp"):
        return _refuse("Connect gmail, microsoft or smtp.", 404)
    body = await _body(request)
    config, env = _config_env()
    try:
        check_provider_allows_inboxes(config)
        address, name, warmup = _connect_fields(body)
    except ConnectRefused as e:
        return _refuse(str(e))

    if kind == "smtp":
        return await _connect_smtp(body, address, name, warmup, config, env)

    problem = _connect_problem(kind, env)
    if problem:
        return _refuse(problem)
    if _connect_task is not None and not _connect_task.done():
        return _refuse("A sign-in is already open. Finish it in your browser, or wait "
                       "five minutes for it to time out.", 409)
    _connect_job.clear()
    _connect_job.update(state="waiting", kind=kind, address=address, message="")
    _connect_task = asyncio.create_task(_run_oauth_connect(kind, address, name, warmup))
    return {"success": True, "started": True}


@router.get("/api/inboxes/connect/status")
async def connect_status():
    return _connect_job_view()


async def _connect_smtp(body, address, name, warmup, config, env):
    from sellbird.integrations.smtp_mail import SmtpSettings

    password = body.get("password")
    host = body.get("smtp_host")
    imap_host = body.get("imap_host") or ""
    username = body.get("username") or ""
    ports = (body.get("smtp_port", 587), body.get("imap_port", 993))
    if not isinstance(password, str) or not password or len(password) > 1024:
        return _refuse("Enter the inbox's SMTP password (or app password).")
    if not isinstance(host, str) or not host.strip() or not isinstance(imap_host, str) \
            or not isinstance(username, str):
        return _refuse("Enter the SMTP server's host name.")
    if any(isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535 for p in ports):
        return _refuse("A port is a whole number from 1 to 65535.")
    settings = SmtpSettings(host=host.strip(), username=username.strip() or address,
                            password=password, port=ports[0],
                            imap_host=imap_host.strip(), imap_port=ports[1])
    store, secrets = _store(), _secrets()
    await store.init_db()
    # Sign in before anything is saved, so a mistyped password is fixed
    # here, not found later in a failed send. "Save anyway" skips this.
    if body.get("save_anyway") is not True:
        ok, detail = await _try_smtp(settings, address, name, config, env)
        if not ok:
            return JSONResponse({"success": False, "sign_in_failed": True,
                                 "message": f"Sellbird couldn't sign in with these settings, so "
                                            f"nothing was saved. {detail}",
                                 "fix": inbox_health.FORM_SIGN_IN_FIX},
                                status_code=400)
    try:
        mailbox_id = await connect_smtp_mailbox(store, secrets, config, address=address,
                                                display_name=name, warmup_started_at=warmup,
                                                settings=settings)
    except (ConnectRefused, SecretStoreUnavailable) as e:
        return _refuse(str(e))
    await store.log_action(action_type="inbox_connected", agent="you",
                           details={"inbox": address, "provider": "smtp"})
    result = await _check(store, mailbox_id, config, env, secrets) or {}
    signin = next((c for c in result.get("checks", []) if c["key"] == "signin"), {})
    return {"success": True, "address": address, "id": mailbox_id, "health": result,
            "test_ok": signin.get("status") == inbox_health.PASS,
            "test_detail": signin.get("detail", "")}


async def _try_smtp(settings, address: str, name: str, config, env) -> tuple[bool, str]:
    """Sign in with SMTP settings that aren't saved yet (no email is sent)."""
    from sellbird.integrations.mail_provider import MailIdentity, identity_from_config
    from sellbird.integrations.smtp_mail import SmtpImapProvider

    identity = MailIdentity(address, name or identity_from_config(config).display_name)
    provider = SmtpImapProvider(config, env, identity=identity, smtp=settings)
    try:
        return await asyncio.wait_for(provider.test_connection(),
                                      timeout=inbox_health.SIGN_IN_TIMEOUT)
    except TimeoutError:
        return False, inbox_health.NO_ANSWER
    except Exception as e:  # a network failure must not 500 the page
        return False, f"Couldn't reach the server ({type(e).__name__})."


@router.post("/api/inboxes/{mailbox_id}/test")
async def test_inbox(mailbox_id: str):
    """Check connection: sign in, read the domain's DNS and the warmup, and
    keep the result. Sends nothing."""
    store = _store()
    await store.init_db()
    config, env = _config_env()
    result = await _check(store, mailbox_id, config, env, _secrets())
    if result is None:
        return _refuse("No connected inbox with that id.", 404)
    signin = next((c for c in result["checks"] if c["key"] == "signin"), {})
    return {"success": True, "ok": signin.get("status") == inbox_health.PASS,
            "detail": signin.get("detail", ""), "health": result}


# ── Editing an inbox ──

def _int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


@router.get("/api/inboxes/{mailbox_id}/settings")
async def inbox_settings(mailbox_id: str):
    """What the edit form shows. The password never leaves the keychain:
    only whether one is stored."""
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id. The main mailbox's settings are in "
                       "sellbird.yaml.", 404)
    config, _ = _config_env()
    out = {
        "id": row["id"], "address": row["address"], "connection": row.get("connection", ""),
        "provider": row.get("provider", ""), "name": row.get("display_name") or "",
        "warmup_started_at": str(row.get("warmup_started_at") or "")[:10],
        "cap": row.get("cap_override"),
        "default_cap": getattr(config.channels.email, "per_inbox_daily_cap", 30),
        "max_cap": HARD_MAX_INBOX_CAP, "smtp": None, "has_password": False,
    }
    if row.get("connection") == "smtp":
        from sellbird.mailboxes import _smtp_settings
        public = _smtp_settings(row, "").public()
        out["smtp"] = {k: public.get(k) for k in ("host", "port", "username", "imap_host",
                                                   "imap_port")}
        try:
            out["has_password"] = bool(_secrets().get(smtp_ref(row["id"])))
        except SecretStoreUnavailable:
            out["has_password"] = False
    return out


@router.post("/api/inboxes/{mailbox_id}/settings")
async def save_inbox_settings(mailbox_id: str, request: Request):
    """Save an inbox's settings in place, then check it again. New SMTP
    settings or a new password sign in first; when that fails nothing is
    saved, unless the form says "save anyway"."""
    from sellbird.integrations.smtp_mail import SmtpSettings
    from sellbird.mailboxes import _smtp_settings

    store, secrets = _store(), _secrets()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    body = await _body(request)
    config, env = _config_env()

    name = body.get("name", row.get("display_name") or "")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in "\r\n<>\""):
        return _refuse("A name is up to 100 characters, on one line, without < > or \".")
    try:
        warmup = check_warmup_start(body.get("warmup_started", "")
                                    if isinstance(body.get("warmup_started", ""), str) else "x")
    except ConnectRefused as e:
        return _refuse(str(e))
    cap = body.get("cap")
    if cap is not None and _int(cap, 1, HARD_MAX_INBOX_CAP) is None:
        return _refuse(f"A cap is a whole number from 1 to {HARD_MAX_INBOX_CAP}, "
                       "or empty for the default.")

    new_settings = None
    new_password = ""
    if row.get("connection") == "smtp":
        try:
            stored_password = secrets.get(smtp_ref(row["id"])) or ""
        except SecretStoreUnavailable as e:
            return _refuse(str(e) or "The keychain is locked. Unlock it and try again.")
        current = _smtp_settings(row, stored_password)
        new_password = body.get("password") or ""
        if not isinstance(new_password, str) or len(new_password) > 1024:
            return _refuse("A password is up to 1024 characters.")
        host = body.get("smtp_host", current.host)
        imap_host = body.get("imap_host", current.imap_host) or ""
        username = body.get("username", current.username) or ""
        ports = (body.get("smtp_port", current.port), body.get("imap_port", current.imap_port))
        if not isinstance(host, str) or not host.strip() or not isinstance(imap_host, str) \
                or not isinstance(username, str):
            return _refuse("Enter the SMTP server's host name.")
        if any(_int(p, 1, 65535) is None for p in ports):
            return _refuse("A port is a whole number from 1 to 65535.")
        candidate = SmtpSettings(host=host.strip(), username=username.strip() or row["address"],
                                 password=new_password or current.password, port=ports[0],
                                 imap_host=imap_host.strip(), imap_port=ports[1])
        if candidate.public() != current.public() or new_password:
            if not candidate.password:
                return _refuse("Enter the password (or app password) for this inbox.")
            if body.get("save_anyway") is not True:
                ok, detail = await _try_smtp(candidate, row["address"], name, config, env)
                if not ok:
                    return JSONResponse({"success": False, "sign_in_failed": True,
                                         "message": "Sellbird couldn't sign in with these "
                                                    f"settings, so nothing was saved. {detail}",
                                         "fix": inbox_health.FORM_SIGN_IN_FIX},
                                        status_code=400)
            new_settings = candidate

    if new_settings is not None:
        if new_password:
            try:
                secrets.set(smtp_ref(row["id"]), new_password)
            except SecretStoreUnavailable as e:
                return _refuse(str(e) or "The keychain is locked. Unlock it and try again.")
        await store.set_connection_settings(row["id"], new_settings.public())
    await store.set_display_name(row["id"], name)
    await store.set_warmup_start(row["id"], warmup)
    await store.set_cap(row["id"], cap)
    await store.log_action(action_type="inbox_edited", agent="you", details={
        "inbox": row["address"],
        "changed": [k for k, v in (("server", new_settings is not None and not new_password),
                                   ("password", bool(new_password))) if v] or ["details"]})
    result = await _check(store, row["id"], config, env, secrets) or {}
    return {"success": True, "health": result}


async def _run_oauth_reconnect(mailbox_id: str, kind: str, address: str) -> None:
    config, env = _config_env()
    try:
        token = await asyncio.to_thread(_sign_in, kind, address, env)
        if token is None:
            _connect_job.update(state="failed", message=(
                "The sign-in didn't finish, or you signed in as a different address. "
                "The old sign-in is unchanged."))
            return
        store, secrets = _store(), _secrets()
        await store.init_db()
        secrets.set(oauth_ref(mailbox_id), keychain_token(
            token, "Google" if kind == "gmail" else "Microsoft"))
        await store.log_action(action_type="inbox_signed_in_again", agent="you",
                               details={"inbox": address, "provider": kind})
        result = await _check(store, mailbox_id, config, env, secrets) or {}
        _connect_job.update(state="connected", message="", id=mailbox_id,
                            verdict=result.get("verdict", ""))
    except (TokenTooLarge, SecretStoreUnavailable) as e:
        _connect_job.update(state="failed", message=str(e))
    except Exception:
        logger.exception("Signing an inbox in again failed")
        _connect_job.update(state="failed", message="Signing in failed. See the dashboard log.")


@router.post("/api/inboxes/{mailbox_id}/reconnect")
async def reconnect_inbox(mailbox_id: str):
    """Sign a Gmail or Microsoft inbox in again, as the same address. The
    new sign-in replaces the old one in the keychain; the inbox keeps its id."""
    global _connect_task
    store = _store()
    await store.init_db()
    row = await _row(store, mailbox_id)
    if row is None:
        return _refuse("No connected inbox with that id.", 404)
    if row.get("connection") != "oauth" or row.get("provider") not in ("google", "microsoft"):
        return _refuse("Only a Gmail or Microsoft inbox signs in again. Change an SMTP "
                       "inbox's password in its settings.")
    kind = "gmail" if row["provider"] == "google" else "microsoft"
    _, env = _config_env()
    problem = _connect_problem(kind, env)
    if problem:
        return _refuse(problem)
    if _connect_task is not None and not _connect_task.done():
        return _refuse("A sign-in is already open. Finish it in your browser, or wait "
                       "five minutes for it to time out.", 409)
    _connect_job.clear()
    _connect_job.update(state="waiting", kind=kind, address=row["address"], message="",
                        id=row["id"])
    _connect_task = asyncio.create_task(_run_oauth_reconnect(row["id"], kind, row["address"]))
    return {"success": True, "started": True, "kind": kind}

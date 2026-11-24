"""Claude subscription quota gauge.

Reads the real 5-hour-window and weekly utilization the same way Claude
Code's own /usage command does: GET api.anthropic.com/api/oauth/usage with
the locally-stored OAuth token. The endpoint is undocumented, so every
failure degrades to None and callers fall back to Sellbird's own call
counting. Responses are cached in-memory to stay polite: a 429 backs off
(honouring Retry-After), and a recent good reading is reused through a
short outage instead of dropping straight to the call counter.
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from sellbird.usage import claude_config_dir

logger = logging.getLogger("sellbird.quota")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CACHE_TTL_OK = 300.0     # seconds to reuse a good response
CACHE_TTL_FAIL = 60.0    # seconds to back off after a failure
RATE_LIMIT_BACKOFF = 900.0   # first wait after a 429 with no Retry-After
MAX_BACKOFF = 3600.0         # longest wait between reads while rate limited
STALE_OK = 1800.0            # reuse a good reading this long through an outage
REQUEST_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

# macOS keychain item Claude Code stores credentials under.
KEYCHAIN_SERVICE = "Claude Code-credentials"


def _token_from_json_blob(blob: str) -> Optional[str]:
    try:
        data = json.loads(blob)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict):
        token = oauth.get("accessToken")
        if isinstance(token, str) and token:
            return token
    return None


def get_oauth_token() -> Optional[str]:
    """Find the Claude Code OAuth access token on this machine.

    Tries the credentials file (Linux / CLAUDE_CONFIG_DIR overrides),
    then the macOS keychain. Returns None when not logged in.
    """
    creds_file = claude_config_dir() / ".credentials.json"
    try:
        if creds_file.is_file():
            token = _token_from_json_blob(creds_file.read_text(encoding="utf-8"))
            if token:
                return token
    except OSError as e:
        logger.debug(f"Could not read {creds_file}: {e}")

    # Default config dir fallback, in case CLAUDE_CONFIG_DIR points elsewhere
    default_creds = Path.home() / ".claude" / ".credentials.json"
    if default_creds != creds_file:
        try:
            if default_creds.is_file():
                token = _token_from_json_blob(default_creds.read_text(encoding="utf-8"))
                if token:
                    return token
        except OSError:
            pass

    if sys.platform == "darwin":
        # Claude Code stores credentials per config dir: the default item is
        # "Claude Code-credentials", but a CLAUDE_CONFIG_DIR install uses a
        # hash-suffixed service name (and the default item may hold an empty
        # token). Enumerate every matching service and take the first item
        # with a real token.
        for service in _keychain_services():
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", service, "-w"],
                    capture_output=True, text=True, timeout=5,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.debug(f"Keychain lookup failed for {service}: {e}")
                continue
            if result.returncode == 0:
                token = _token_from_json_blob(result.stdout.strip())
                if token:
                    return token

    return None


def _keychain_services() -> list[str]:
    """All keychain service names Claude Code may store credentials under."""
    services = [KEYCHAIN_SERVICE]
    try:
        dump = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, timeout=10,
        )
        if dump.returncode == 0:
            for line in dump.stdout.splitlines():
                # Lines look like:  "svce"<blob>="Claude Code-credentials-53f815ad"
                if '"svce"<blob>="' not in line:
                    continue
                name = line.split('"svce"<blob>="', 1)[1].rstrip('"')
                if name.startswith(KEYCHAIN_SERVICE) and name not in services:
                    services.append(name)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"Keychain enumeration failed: {e}")
    return services


def _parse_window(value) -> Optional[dict]:
    """Normalize one utilization window to {'utilization': float, 'resets_at': str}."""
    if not isinstance(value, dict):
        return None
    utilization = value.get("utilization")
    if not isinstance(utilization, (int, float)):
        return None
    resets_at = value.get("resets_at") or value.get("resetsAt") or ""
    return {
        "utilization": float(utilization),
        "resets_at": str(resets_at) if resets_at else "",
    }


def _retry_after(resp) -> Optional[float]:
    """Seconds from a Retry-After header, if it holds a number."""
    try:
        value = float(resp.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


class QuotaClient:
    """Cached reader of Claude subscription utilization."""

    def __init__(self):
        self._cache: Optional[dict] = None
        self._cache_at = 0.0
        self._cache_ttl = 0.0
        self._last_good: Optional[dict] = None
        self._last_good_at = 0.0
        self._backoff = 0.0
        self._lock = asyncio.Lock()
        self.problem = ""  # why the last read failed ("" when it worked)

    async def get_utilization(self) -> Optional[dict]:
        """Return {'five_hour': {...}, 'seven_day': {...}} or None.

        Each window dict has 'utilization' (percent, 0-100) and
        'resets_at' (ISO timestamp or ''). Missing windows are omitted;
        an empty result is reported as None. While the endpoint fails, a
        good reading up to STALE_OK old is returned instead of None.
        """
        async with self._lock:
            now = time.monotonic()
            if not (self._cache_at and now - self._cache_at < self._cache_ttl):
                result, problem, wait = await self._read()
                self._cache_at = now
                self.problem = problem
                if result:
                    self._last_good, self._last_good_at = result, now
                    self._backoff = 0.0
                    self._cache_ttl = CACHE_TTL_OK
                elif wait is not None:
                    self._cache_ttl = wait
                else:
                    self._cache_ttl = CACHE_TTL_FAIL
                self._cache = result
            if self._cache:
                return self._cache
            if self._last_good and now - self._last_good_at < STALE_OK:
                return self._last_good
            return None

    def status(self) -> dict:
        """Where the last answer came from: live, saved (an earlier good
        reading reused through an outage) or unreadable, and why."""
        now = time.monotonic()
        if self._cache:
            return {"source": "live", "problem": "", "age_minutes": 0}
        age = int((now - self._last_good_at) // 60) if self._last_good else None
        if self._last_good and now - self._last_good_at < STALE_OK:
            return {"source": "saved", "problem": self.problem, "age_minutes": age}
        return {"source": "unreadable", "problem": self.problem or "not read yet",
                "age_minutes": age}

    async def _read(self) -> tuple[Optional[dict], str, Optional[float]]:
        """One read: (windows or None, problem, seconds to wait if rate limited)."""
        token = await asyncio.get_event_loop().run_in_executor(None, get_oauth_token)
        if not token:
            logger.debug("Quota: no Claude OAuth token found; falling back.")
            return None, "no Claude login found (run 'claude' and /login)", None

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(
                    USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": OAUTH_BETA_HEADER,
                    },
                )
        except httpx.HTTPError as e:
            logger.debug(f"Quota fetch failed: {e}")
            return None, f"network error ({type(e).__name__})", None

        if resp.status_code == 429:
            wait = _retry_after(resp)
            if wait is None:
                wait = min(max(self._backoff * 2, RATE_LIMIT_BACKOFF), MAX_BACKOFF)
            self._backoff = min(wait, MAX_BACKOFF)
            logger.debug(f"Quota endpoint rate limited; next read in {self._backoff:.0f}s.")
            return None, "Anthropic's usage endpoint is rate limiting reads (HTTP 429)", \
                self._backoff
        if resp.status_code != 200:
            logger.debug(f"Quota endpoint returned {resp.status_code}.")
            return None, f"usage endpoint answered HTTP {resp.status_code}", None

        try:
            payload = resp.json()
        except ValueError:
            return None, "usage endpoint sent an unreadable answer", None
        if not isinstance(payload, dict):
            return None, "usage endpoint sent an unreadable answer", None

        windows = {}
        for key in ("five_hour", "seven_day"):
            parsed = _parse_window(payload.get(key))
            if parsed:
                windows[key] = parsed
        if not windows:
            return None, "usage endpoint sent no usage windows", None
        return windows, "", None

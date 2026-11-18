"""Email-Check.app verifier: response mapping and where it sits in the chain."""

import httpx
import pytest

import sellbird.integrations.email_finder as ef


def _serve(monkeypatch, status_code=200, body=None, text=None):
    real_client = httpx.AsyncClient
    seen = []

    class _Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(request)
            if text is not None:
                return httpx.Response(status_code, text=text, request=request)
            return httpx.Response(status_code, json=body, request=request)

    def client(*a, **kw):
        kw.pop("transport", None)
        return real_client(transport=_Transport(), **kw)

    monkeypatch.setattr(ef.httpx, "AsyncClient", client)
    return seen


@pytest.mark.parametrize("body, status, catch_all", [
    ({"validFormat": True, "validMx": True, "validSmtp": True, "isDisposable": False}, "valid", False),
    ({"validFormat": True, "validMx": True, "validSmtp": False, "isDisposable": False}, "invalid", False),
    ({"validFormat": True, "validMx": True, "validSmtp": None, "isDisposable": False}, "unknown", False),
    ({"validFormat": False, "validMx": None, "validSmtp": None, "isDisposable": False}, "invalid", False),
    ({"validFormat": True, "validMx": False, "validSmtp": None, "isDisposable": False}, "invalid", False),
    ({"validFormat": True, "validMx": True, "validSmtp": True, "isDisposable": True}, "disposable", False),
    ({"validFormat": True, "validMx": True, "validSmtp": True, "isDisposable": False,
      "smtpDebug": {"isCatchAll": True}}, "valid", True),
])
@pytest.mark.asyncio
async def test_email_check_response_mapping(monkeypatch, body, status, catch_all):
    _serve(monkeypatch, body=body)
    assert await ef.verify_email_check("pat@example.com", "k") == {"status": status, "catch_all": catch_all}


@pytest.mark.asyncio
async def test_email_check_asks_for_the_smtp_check(monkeypatch):
    seen = _serve(monkeypatch, body={"validFormat": True, "validMx": True, "validSmtp": True})
    await ef.verify_email_check("pat@example.com", "k")
    request = seen[0]
    assert request.url.host == "api.email-check.app"
    assert request.url.params["email"] == "pat@example.com"
    assert request.url.params["verifySmtp"] == "true"
    assert request.url.params["verifyMx"] == "true"


@pytest.mark.parametrize("code", [401, 402, 429, 500])
@pytest.mark.asyncio
async def test_email_check_http_errors_are_unavailable(monkeypatch, code):
    _serve(monkeypatch, status_code=code, body={"error": "X", "message": "no"})
    assert await ef.verify_email_check("pat@example.com", "k") is None


@pytest.mark.asyncio
async def test_email_check_error_body_or_bad_json_is_unavailable(monkeypatch):
    _serve(monkeypatch, body={"error": "PAYMENT_REQUIRED", "validFormat": True})
    assert await ef.verify_email_check("pat@example.com", "k") is None
    _serve(monkeypatch, text="<html>not json</html>")
    assert await ef.verify_email_check("pat@example.com", "k") is None


class _Env:
    reoon_api_key = ""
    zerobounce_api_key = ""
    email_check_api_key = "k"


@pytest.mark.parametrize("verdict, expected", [
    ({"status": "valid", "catch_all": False}, ("verified", False)),
    ({"status": "valid", "catch_all": True}, ("risky", True)),
    ({"status": "invalid", "catch_all": False}, ("invalid", False)),
])
@pytest.mark.asyncio
async def test_verify_candidate_uses_email_check_on_a_big_provider(monkeypatch, verdict, expected):
    async def fake(email, key):
        return verdict

    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "verify_email_check", fake)
    assert await ef._verify_candidate("pat@example.com", "google", _Env()) == expected


@pytest.mark.asyncio
async def test_verify_candidate_uses_email_check_on_a_small_host_before_smtp(monkeypatch):
    async def fake(email, key):
        return {"status": "valid", "catch_all": False}

    async def smtp_must_not_run(email):
        raise AssertionError("a conclusive Email-Check verdict ends the chain")

    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "verify_email_check", fake)
    monkeypatch.setattr(ef, "verify_email_smtp", smtp_must_not_run)
    assert await ef._verify_candidate("pat@example.com", "other", _Env()) == ("verified", False)


@pytest.mark.asyncio
async def test_verify_candidate_falls_through_when_email_check_is_inconclusive(monkeypatch):
    async def inconclusive(email, key):
        return {"status": "unknown", "catch_all": False}

    async def smtp_rejects(email):
        return False

    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    monkeypatch.setattr(ef, "verify_email_check", inconclusive)
    monkeypatch.setattr(ef, "verify_email_smtp", smtp_rejects)
    assert await ef._verify_candidate("pat@example.com", "other", _Env()) == ("invalid", False)

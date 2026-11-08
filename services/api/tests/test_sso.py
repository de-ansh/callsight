"""Single sign-on: who may sign in, sessions, CSRF, revocation. The OIDC provider is faked here;
a real browser sign-in against Dex is checked separately."""

import os
import time
import uuid

import pytest

pytest.importorskip("fastapi")
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from callsight import auth, sso, store  # noqa: E402
from callsight.api import app  # noqa: E402

DB_URL = os.environ.get("CALLSIGHT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="needs CALLSIGHT_TEST_DATABASE_URL")
TRANSCRIPT = "[00:00] Alex: Hi\n[00:05] Priya: The price is too expensive.\n"


class FakeProvider:
    """Stands in for Authlib's client: what a provider's validated ID token would contain."""

    def __init__(self):
        self.claims = {}
        self.error = None

    async def authorize_redirect(self, request, redirect_uri):
        assert redirect_uri.endswith("/auth/callback")
        return RedirectResponse("https://idp.example/authorize", status_code=302)

    async def authorize_access_token(self, request):
        if self.error:
            raise self.error
        return {"userinfo": self.claims}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    monkeypatch.setenv("CALLSIGHT_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("CALLSIGHT_OIDC_CLIENT_ID", "callsight")
    fake = FakeProvider()
    monkeypatch.setattr(sso, "_client", lambda settings: fake)
    return fake


@pytest.fixture
def domain():
    """A fresh org with an allowed domain."""
    name, dom = f"sso-{uuid.uuid4().hex[:8]}", f"{uuid.uuid4().hex[:8]}.example.com"
    with store.connect(DB_URL) as conn:
        auth.allow_domain(conn, name, dom)
    return dom


def sign_in(provider, email, verified=True):
    browser = TestClient(app)
    provider.claims = {"email": email, "email_verified": verified}
    res = browser.get("/auth/callback", follow_redirects=False)
    return browser, res


def test_sso_is_off_without_configuration(monkeypatch):
    monkeypatch.delenv("CALLSIGHT_OIDC_ISSUER", raising=False)
    browser = TestClient(app)
    assert browser.get("/auth/config").json() == {"sso": False}
    assert browser.get("/auth/login", follow_redirects=False).status_code == 404


def test_login_redirects_to_the_provider(provider):
    res = TestClient(app).get("/auth/login", follow_redirects=False)
    assert res.status_code == 302 and res.headers["location"].startswith("https://idp.example/")


def test_verified_email_at_allowed_domain_signs_in(provider, domain):
    browser, res = sign_in(provider, f"Alice@{domain.upper()}")
    assert res.status_code == 303 and res.headers["location"] == "/"
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    me = browser.get("/auth/me").json()
    assert me["actor"] == f"user:alice@{domain}"
    created = browser.post("/v1/analyze", json={"transcript": TRANSCRIPT}, headers={"x-callsight-csrf": "1"})
    assert created.status_code == 200
    assert any(r["action"] == "user.sign_in" for r in browser.get("/v1/audit").json())
    assert any(r["actor"] == f"user:alice@{domain}" and r["action"] == "call.create" for r in browser.get("/v1/audit").json())


@pytest.mark.parametrize("email,verified,message", [
    ("bob@not-allowed.example", True, "not allowed"),
    (None, True, "verified email"),
])
def test_refused_sign_ins_get_no_session(provider, domain, email, verified, message):
    browser, res = sign_in(provider, email or f"carol@{domain}", verified if email else False)
    assert res.status_code == 303 and res.headers["location"].startswith("/#/signin?error=")
    assert message.replace(" ", "%20") in res.headers["location"]
    assert browser.get("/auth/me").status_code == 401


def test_provider_errors_are_shown_not_raised(provider):
    from authlib.integrations.starlette_client import OAuthError

    provider.error = OAuthError(error="access_denied")
    res = TestClient(app).get("/auth/callback", follow_redirects=False)
    assert res.status_code == 303 and "error=" in res.headers["location"]


def test_session_writes_need_the_csrf_header(provider, domain):
    browser, _ = sign_in(provider, f"dan@{domain}")
    assert browser.post("/v1/analyze", json={"transcript": TRANSCRIPT}).status_code == 403
    assert browser.put("/v1/settings", json={"competitors": []}).status_code == 403
    assert browser.get("/v1/calls").status_code == 200  # reads are fine


def test_removing_the_domain_ends_sessions_and_logout_works(provider, domain):
    browser, _ = sign_in(provider, f"erin@{domain}")
    assert browser.get("/v1/calls").status_code == 200
    with store.connect(DB_URL) as conn:
        assert auth.disallow_domain(conn, domain) == 1
    assert browser.get("/v1/calls").status_code == 401

    with store.connect(DB_URL) as conn:
        auth.allow_domain(conn, f"sso-{uuid.uuid4().hex[:8]}", domain)  # domain now points at another org
    assert browser.get("/v1/calls").status_code == 401  # old session was for the old org

    browser, _ = sign_in(provider, f"erin@{domain}")
    assert browser.post("/auth/logout").status_code == 204
    assert browser.get("/v1/calls").status_code == 401


def test_sessions_expire(provider, domain, monkeypatch):
    browser, _ = sign_in(provider, f"finn@{domain}")
    real = time.time
    monkeypatch.setattr(sso.time, "time", lambda: real() + sso.SESSION_HOURS * 3600 + 5)
    assert browser.get("/v1/calls").status_code == 401


def test_signed_in_users_only_see_their_org(provider, domain, client):
    mine = client.post("/v1/analyze", json={"call_id": "key-org-call", "transcript": TRANSCRIPT}).json()["id"]
    browser, _ = sign_in(provider, f"gwen@{domain}")
    assert browser.get(f"/v1/calls/{mine}").status_code == 404
    assert all(c["id"] != mine for c in browser.get("/v1/calls").json())


def test_admin_rejects_invalid_domains(monkeypatch, capsys):
    from callsight import admin

    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    assert admin.main(["allow-domain", "--org", "x", "--domain", "not a domain"]) == 2
    assert "not a valid domain" in capsys.readouterr().err

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


def test_access_overview_lists_keys_domains_and_sign_ins_without_secrets(provider, domain):
    browser, _ = sign_in(provider, f"hana@{domain}")
    with store.connect(DB_URL) as conn:
        org = conn.execute("SELECT org_id FROM org_domains WHERE domain = %s", (domain,)).fetchone()["org_id"]
        name = conn.execute("SELECT name FROM orgs WHERE id = %s", (org,)).fetchone()["name"]
        key = auth.create_key(conn, name, "ci-bot")
        auth.create_key(conn, name, "old-laptop")
        auth.revoke_key(conn, name, "old-laptop")
    res = browser.get("/v1/access")
    a = res.json()
    assert {k["name"]: k["revoked_at"] is None for k in a["keys"]} == {"ci-bot": True, "old-laptop": False}
    assert a["domains"][0]["domain"] == domain
    assert a["sign_ins"][0]["email"] == f"hana@{domain}" and a["sign_ins"][0]["sign_ins"] == 1
    assert "key_hash" not in res.text and key not in res.text and auth.hash_key(key) not in res.text

    other = TestClient(app)
    other_key = None
    with store.connect(DB_URL) as conn:
        other_key = auth.create_key(conn, f"other-{uuid.uuid4().hex[:6]}", "x")
    theirs = other.get("/v1/access", headers={"authorization": f"Bearer {other_key}"}).json()
    assert theirs["domains"] == [] and theirs["sign_ins"] == [] and [k["name"] for k in theirs["keys"]] == ["x"]


# --- admin roles ------------------------------------------------------------------------------

CSRF = {"x-callsight-csrf": "1"}


def _org_of(domain):
    with store.connect(DB_URL) as conn:
        org = conn.execute("SELECT org_id FROM org_domains WHERE domain = %s", (domain,)).fetchone()["org_id"]
        return org, conn.execute("SELECT name FROM orgs WHERE id = %s", (org,)).fetchone()["name"]


def _as(key):
    return TestClient(app, headers={"authorization": f"Bearer {key}"})


def test_members_cannot_manage_access(provider, domain):
    _, name = _org_of(domain)
    with store.connect(DB_URL) as conn:
        member = _as(auth.create_key(conn, name, "member"))
    assert member.get("/auth/me").json()["admin"] is False
    assert member.post("/v1/keys", json={"name": "x"}).status_code == 403
    assert member.delete(f"/v1/keys/{uuid.uuid4()}").status_code == 403
    assert member.delete(f"/v1/domains/{domain}").status_code == 403
    assert member.post("/v1/admins", json={"email": f"a@{domain}"}).status_code == 403
    assert member.get("/v1/access").status_code == 200  # still readable
    browser, _ = sign_in(provider, f"ivy@{domain}")
    assert browser.post("/v1/keys", json={"name": "x"}, headers=CSRF).status_code == 403


def test_admin_key_creates_and_revokes_keys(provider, domain):
    org, name = _org_of(domain)
    with store.connect(DB_URL) as conn:
        boss = _as(auth.create_key(conn, name, "boss", admin=True))
        other_key = auth.create_key(conn, f"other-{uuid.uuid4().hex[:6]}", "theirs")
        theirs = conn.execute("SELECT id FROM api_keys WHERE key_hash = %s", (auth.hash_key(other_key),)).fetchone()["id"]
    assert boss.get("/auth/me").json()["admin"] is True
    made = boss.post("/v1/keys", json={"name": " ci-bot ", "admin": False}).json()
    assert made["name"] == "ci-bot" and made["key"].startswith("cs_")
    bot = _as(made["key"])
    assert bot.get("/auth/me").json() == {"actor": "key:ci-bot", "org_id": str(org), "admin": False}

    access = boss.get("/v1/access")
    assert made["key"] not in access.text and auth.hash_key(made["key"]) not in access.text
    assert {k["name"]: k["admin"] for k in access.json()["keys"]} == {"boss": True, "ci-bot": False}

    assert boss.delete(f"/v1/keys/{theirs}").status_code == 404  # another org's key
    assert _as(other_key).get("/auth/me").status_code == 200
    assert boss.delete(f"/v1/keys/{made['id']}").status_code == 204
    assert bot.get("/auth/me").status_code == 401
    assert boss.delete(f"/v1/keys/{made['id']}").status_code == 404  # already revoked
    actions = [(r["actor"], r["action"], r["target"]) for r in boss.get("/v1/audit").json()]
    assert ("key:boss", "key.create", "ci-bot") in actions and ("key:boss", "key.revoke", "ci-bot") in actions


def test_admin_removes_only_their_own_domains(provider, domain):
    _, name = _org_of(domain)
    other_domain = f"{uuid.uuid4().hex[:8]}.example.org"
    with store.connect(DB_URL) as conn:
        boss = _as(auth.create_key(conn, name, "boss", admin=True))
        auth.allow_domain(conn, f"other-{uuid.uuid4().hex[:6]}", other_domain)
    browser, _ = sign_in(provider, f"jo@{domain}")
    assert browser.get("/auth/me").status_code == 200
    assert boss.delete(f"/v1/domains/{other_domain}").status_code == 404
    assert boss.delete("/v1/domains/not a domain").status_code == 404
    with store.connect(DB_URL) as conn:
        assert auth.org_for_email(conn, f"x@{other_domain}") is not None
    assert boss.delete(f"/v1/domains/{domain.upper()}").status_code == 204
    assert browser.get("/auth/me").status_code == 401  # their session ended at once
    assert boss.post(f"/v1/domains/{domain}").status_code == 405  # adding stays on the command line


def test_signed_in_admins(provider, domain):
    _, name = _org_of(domain)
    with store.connect(DB_URL) as conn:
        boss = _as(auth.create_key(conn, name, "boss", admin=True))
    assert boss.post("/v1/admins", json={"email": "not-an-email"}).status_code == 422
    assert boss.post("/v1/admins", json={"email": f" Kim@{domain.upper()} "}).json() == {"email": f"kim@{domain}"}
    kim, _ = sign_in(provider, f"kim@{domain}")
    lee, _ = sign_in(provider, f"lee@{domain}")
    assert kim.get("/auth/me").json()["admin"] is True and lee.get("/auth/me").json()["admin"] is False
    assert kim.post("/v1/admins", json={"email": f"lee@{domain}"}).status_code == 403  # CSRF header still required
    assert kim.post("/v1/admins", json={"email": f"lee@{domain}"}, headers=CSRF).status_code == 201
    assert lee.get("/auth/me").json()["admin"] is True
    assert [a["email"] for a in boss.get("/v1/access").json()["admins"]] == [f"kim@{domain}", f"lee@{domain}"]
    assert kim.delete(f"/v1/admins/kim@{domain}", headers=CSRF).status_code == 400  # not yourself
    assert kim.delete(f"/v1/admins/lee@{domain}", headers=CSRF).status_code == 204
    assert lee.get("/auth/me").json()["admin"] is False  # immediately, same session
    assert lee.post("/v1/keys", json={"name": "x"}, headers=CSRF).status_code == 403
    assert kim.delete(f"/v1/admins/lee@{domain}", headers=CSRF).status_code == 404


def test_admin_cli_roles(domain, monkeypatch, capsys):
    from callsight import admin

    _, name = _org_of(domain)
    monkeypatch.setenv("CALLSIGHT_DATABASE_URL", DB_URL)
    assert admin.main(["create-key", "--org", name, "--name", "ops", "--admin"]) == 0
    assert _as(capsys.readouterr().out.strip()).get("/auth/me").json()["admin"] is True
    assert admin.main(["grant-admin", "--org", name, "--email", f"Max@{domain}"]) == 0
    assert admin.main(["grant-admin", "--org", "no-such-org-xyz", "--email", f"max@{domain}"]) == 1
    assert admin.main(["grant-admin", "--org", name, "--email", "nope"]) == 2
    org, _ = _org_of(domain)
    with store.connect(DB_URL) as conn:
        assert auth.is_email_admin(conn, org, f"max@{domain}")
    assert admin.main(["revoke-admin", "--org", name, "--email", f"max@{domain}"]) == 0
    assert admin.main(["revoke-admin", "--org", name, "--email", f"max@{domain}"]) == 1


def test_existing_keys_stay_members(client):
    assert client.get("/auth/me").json()["admin"] is False

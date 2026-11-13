"""Admin commands. python -m callsight.admin create-key --org acme --name laptop

`demo` creates a 'demo' organization with a key and six example calls, for a first look."""

from __future__ import annotations

import argparse
import sys

from importlib.resources import files

from . import store
from .auth import allow_domain, create_key, disallow_domain, revoke_key, set_email_admin
from .config import Settings
from .migrate import migrate
from .service import analyze_text_with_transcript

DEMO_COMPETITORS = ("AcmeCall", "Gong", "Chorus")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="callsight.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create-key", "revoke-key"):
        p = sub.add_parser(name)
        p.add_argument("--org", default="default", help="organization name (created if missing)")
        p.add_argument("--name", required=True, help="a label for the key, e.g. the person or system using it")
        if name == "create-key":
            p.add_argument("--admin", action="store_true", help="the key may manage keys, domains and admins in the UI")
    for name, verb in (("grant-admin", "make"), ("revoke-admin", "stop")):
        g = sub.add_parser(name, help=f"{verb} EMAIL (who signs in with SSO) an admin of ORG")
        g.add_argument("--org", default="default")
        g.add_argument("--email", required=True)
    d = sub.add_parser("allow-domain", help="let people with a verified email at DOMAIN sign in to ORG")
    d.add_argument("--org", default="default")
    d.add_argument("--domain", required=True)
    dd = sub.add_parser("disallow-domain", help="stop sign-in for DOMAIN (existing sessions end immediately)")
    dd.add_argument("--domain", required=True)
    w = sub.add_parser("webhook-secret", help="print an org's webhook secret and URL path")
    w.add_argument("--org", default="default")
    sub.add_parser("demo", help="create a 'demo' org with a key and six example calls")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.database_url:
        print("set CALLSIGHT_DATABASE_URL", file=sys.stderr)
        return 2
    migrate(settings.database_url)
    with store.connect(settings.database_url) as conn:
        if args.command == "create-key":
            print(create_key(conn, args.org, args.name, admin=args.admin))
            print("Store this key now; it cannot be shown again.", file=sys.stderr)
        elif args.command in ("grant-admin", "revoke-admin"):
            grant = args.command == "grant-admin"
            row = conn.execute("SELECT id FROM orgs WHERE name = %s", (args.org,)).fetchone()
            if not row:
                print(f"no org named {args.org!r}", file=sys.stderr)
                return 1
            try:
                changed = set_email_admin(conn, row["id"], args.email, grant, "admin-cli")
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"{args.email.strip().lower()} is {'now' if grant else 'no longer'} an admin of {args.org!r}"
                  + ("" if changed else " (unchanged)") + ".")
            return 0 if changed or grant else 1
        elif args.command == "revoke-key":
            n = revoke_key(conn, args.org, args.name)
            print(f"revoked {n} key(s)")
            return 0 if n else 1
        elif args.command == "allow-domain":
            try:
                allow_domain(conn, args.org, args.domain)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"People with a verified @{args.domain.strip().lower().lstrip('@')} email can now sign in to {args.org!r}.")
        elif args.command == "disallow-domain":
            n = disallow_domain(conn, args.domain)
            print(f"removed {n} domain(s)")
            return 0 if n else 1
        elif args.command == "demo":
            return _demo(conn, settings)
        else:
            row = conn.execute("SELECT id, webhook_secret FROM orgs WHERE name = %s", (args.org,)).fetchone()
            if not row:
                print(f"no org named {args.org!r}", file=sys.stderr)
                return 1
            print(f"secret: {row['webhook_secret']}\npath:   /v1/orgs/{row['id']}/connectors/generic/webhook")
    return 0


def _demo(conn, settings: Settings) -> int:
    key = create_key(conn, "demo", "demo", admin=True)  # so a first look includes managing access
    org_id = conn.execute("SELECT id FROM orgs WHERE name = 'demo'").fetchone()["id"]
    if not store.org_competitors(conn, org_id):  # so later uploads in the demo org flag them too
        store.set_org_competitors(conn, org_id, list(DEMO_COMPETITORS))
    have = {r["external_id"] for r in conn.execute("SELECT external_id FROM calls WHERE org_id = %s", (org_id,))}
    loaded = 0
    for f in sorted(files("callsight.demo").iterdir(), key=lambda f: f.name):
        name = f.name.removesuffix(".txt")
        if not f.name.endswith(".txt") or name in have:  # running demo twice does not duplicate calls
            continue
        transcript, report = analyze_text_with_transcript(
            f.read_text(encoding="utf-8"), call_id=name, settings=settings, extra_competitors=DEMO_COMPETITORS
        )
        call_id = store.save_call(conn, org_id, transcript, report, source="demo")
        store.audit(conn, org_id, "admin-cli", "call.create", str(call_id), {"source": "demo"})
        loaded += 1
    print(f"Loaded {loaded} example call(s) into the 'demo' organization.")
    print(f"API key: {key}")
    print("Open http://localhost:8000 and paste the key. (Shown once; run demo again for a new key.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

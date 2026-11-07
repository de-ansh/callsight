"""Admin commands. python -m callsight.admin create-key --org acme --name laptop"""

from __future__ import annotations

import argparse
import sys

from . import store
from .auth import create_key, revoke_key
from .config import Settings
from .migrate import migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="callsight.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create-key", "revoke-key"):
        p = sub.add_parser(name)
        p.add_argument("--org", default="default", help="organization name (created if missing)")
        p.add_argument("--name", required=True, help="a label for the key, e.g. the person or system using it")
    w = sub.add_parser("webhook-secret", help="print an org's webhook secret and URL path")
    w.add_argument("--org", default="default")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.database_url:
        print("set CALLSIGHT_DATABASE_URL", file=sys.stderr)
        return 2
    migrate(settings.database_url)
    with store.connect(settings.database_url) as conn:
        if args.command == "create-key":
            print(create_key(conn, args.org, args.name))
            print("Store this key now; it cannot be shown again.", file=sys.stderr)
        elif args.command == "revoke-key":
            n = revoke_key(conn, args.org, args.name)
            print(f"revoked {n} key(s)")
            return 0 if n else 1
        else:
            row = conn.execute("SELECT id, webhook_secret FROM orgs WHERE name = %s", (args.org,)).fetchone()
            if not row:
                print(f"no org named {args.org!r}", file=sys.stderr)
                return 1
            print(f"secret: {row['webhook_secret']}\npath:   /v1/orgs/{row['id']}/connectors/generic/webhook")
    return 0


if __name__ == "__main__":
    sys.exit(main())

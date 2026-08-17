"""API launcher: unlock the token store, then serve.

    python3 -m netapp_migration.interfaces.api.serve --host 0.0.0.0 --port 8000

The global token is asked interactively (never echoed, never in argv, never
in the environment, never written to disk). It is used to decrypt the token
store in memory; the API is unusable until that succeeds.

Because the decrypted store lives only in memory, **any restart of the
service requires a super admin to supply the global token again** — that is
deliberate: an unattended restart must not silently re-open the API.
"""

import argparse
import getpass
import os
import sys

from ...models import AuthError
from ...security.tokens import TokenStore, default_store_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netapp-migration-api",
        description="Unlock the token store and start the migration API.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1 — bind to "
                             "0.0.0.0 only behind a trusted network)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token-store", default=None,
                        help=f"encrypted token store "
                             f"(default: {default_store_path()})")
    parser.add_argument("--token-stdin", action="store_true",
                        help="read the global token from stdin instead of "
                             "prompting (for a supervised restart; the token "
                             "must not appear in a shell history or a file)")
    parser.add_argument("--log-level", default="info")
    return parser


def read_global_token(from_stdin: bool) -> str:
    if from_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            print("ERROR: no token received on stdin.", file=sys.stderr)
            raise SystemExit(2)
        return token
    return getpass.getpass("Global token (super admin): ")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    from . import app as app_module            # imported late: reads env vars

    store = TokenStore(args.token_store)
    if not store.exists:
        print(f"ERROR: no token store at {store.path}", file=sys.stderr)
        print("Initialise it first:", file=sys.stderr)
        print("  python3 netapp_cascade_migration.py --action tokens-init",
              file=sys.stderr)
        return 2

    try:
        store.unlock(read_global_token(args.token_stdin))
    except AuthError as exc:
        print(f"ERROR: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"       {exc.hint}", file=sys.stderr)
        return 3
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.", file=sys.stderr)
        return 1

    # Hand the unlocked store to the application.
    app_module._tokens = store

    scopes = store.list_scopes()
    print(f"Token store unlocked: {len(scopes)} delegated token(s).")
    print(f"Serving on http://{args.host}:{args.port}  (docs at /docs)")
    if args.host == "0.0.0.0":
        print("WARNING: bound to every interface — make sure the port is "
              "reachable only from trusted hosts.")

    import uvicorn
    uvicorn.run(app_module.app, host=args.host, port=args.port,
                log_level=args.log_level, workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

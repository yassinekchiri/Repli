"""API launcher: unlock the token store, then serve.

Two start modes, because a service and a terminal have different needs:

  * **Foreground** (default) — the global token is asked interactively before
    the port is bound. Right for a super admin running the API in a terminal
    or a tmux session::

        python3 -m netapp_migration.interfaces.api.serve --host 0.0.0.0

  * **Locked start** (``--start-locked``) — the port is bound immediately and
    every endpoint answers 503 until the global token arrives on a local unix
    socket. Right for systemd: a service has no terminal an administrator
    connected over SSH can type into, so prompting there would block forever
    with nothing listening::

        python3 -m ...serve --start-locked --unlock-socket /path/unlock.sock
        # then, from any admin session:
        netapp_cascade_migration.py --action api-unlock

In both cases the global token is never echoed, never in argv, never in the
environment, never written to disk. It is used to decrypt the token store in
memory only.

Because the decrypted store lives only in memory, **any restart requires a
super admin to supply the global token again** — deliberately: an unattended
restart must not silently re-open the API.
"""

import argparse
import getpass
import os
import select
import socket
import sys

from ...models import AuthError
from ...security.tokens import TokenStore, default_store_path
from .unlock import UnlockListener, default_socket_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netapp-migration-api",
        description="Unlock the token store and start the migration API.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1 — only "
                             "reachable from the API server itself; use "
                             "0.0.0.0 to accept remote clients)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token-store", default=None,
                        help=f"encrypted token store "
                             f"(default: {default_store_path()})")
    parser.add_argument("--start-locked", action="store_true",
                        help="bind the port immediately and stay locked "
                             "(503 everywhere) until the global token is "
                             "supplied on the unlock socket. For systemd.")
    parser.add_argument("--unlock-socket", default=None,
                        help="unix socket accepting the global token when "
                             "--start-locked is used (default: unlock.sock "
                             "beside the token store)")
    parser.add_argument("--token-stdin", action="store_true",
                        help="read the global token from stdin instead of "
                             "prompting (for a supervised restart; the token "
                             "must not appear in a shell history or a file)")
    parser.add_argument("--token-timeout", type=int, default=300,
                        help="seconds to wait for a token on stdin before "
                             "giving up (0 = wait forever). Guards against a "
                             "service parked invisibly on a read.")
    parser.add_argument("--log-level", default="info")
    return parser


def _sd_notify(state: str) -> None:
    """Tell systemd we are ready, when running under Type=notify.

    A no-op outside systemd. Without it the unit would be reported active
    before the port is bound, which is exactly the lie that makes a stuck
    service look healthy.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):                 # abstract namespace
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
    except OSError:
        pass


def read_global_token(from_stdin: bool, timeout: int = 0) -> str:
    if not from_stdin:
        return getpass.getpass("Global token (super admin): ")

    # Visible even when stdin is a pipe or a console nobody is watching: a
    # silent block is the failure mode this whole path exists to avoid.
    print("Waiting for the global token on stdin...", file=sys.stderr,
          flush=True)
    if timeout > 0:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            print(f"ERROR: no token received on stdin after {timeout}s.",
                  file=sys.stderr)
            print("       A service has no terminal to prompt on — start it "
                  "with --start-locked and unlock it with", file=sys.stderr)
            print("       'netapp_cascade_migration.py --action api-unlock'.",
                  file=sys.stderr)
            raise SystemExit(2)
    token = sys.stdin.readline().strip()
    if not token:
        print("ERROR: no token received on stdin.", file=sys.stderr)
        raise SystemExit(2)
    return token


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

    listener = None
    if args.start_locked:
        socket_path = args.unlock_socket or default_socket_path(store.path)
        listener = UnlockListener(store, socket_path)
        try:
            listener.bind()
        except OSError as exc:
            print(f"ERROR: cannot create the unlock socket {socket_path}: "
                  f"{exc}", file=sys.stderr)
            return 2
        listener.start()
    else:
        try:
            store.unlock(read_global_token(args.token_stdin,
                                           args.token_timeout))
        except AuthError as exc:
            print(f"ERROR: {exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"       {exc.hint}", file=sys.stderr)
            return 3
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.", file=sys.stderr)
            return 1

    # Hand the store to the application. Locked or not, it is the same
    # object: unlocking it later through the socket unlocks the API too.
    app_module._tokens = store

    if store.unlocked:
        print(f"Token store unlocked: {len(store.list_scopes())} "
              f"delegated token(s).")
    else:
        print("API starting LOCKED — every endpoint answers 503 until the "
              "global token is supplied.")
        print(f"Unlock it with:\n"
              f"  netapp_cascade_migration.py --action api-unlock "
              f"--unlock-socket {listener.path}")

    print(f"Serving on http://{args.host}:{args.port}  (docs at /docs)")
    if args.host == "127.0.0.1":
        print("NOTE: bound to the loopback interface — unreachable from "
              "other machines. Use --host 0.0.0.0 (or an SSH tunnel) to "
              "open /docs from a workstation.")
    elif args.host == "0.0.0.0":
        print("WARNING: bound to every interface — make sure the port is "
              "reachable only from trusted hosts.")

    import uvicorn
    config = uvicorn.Config(app_module.app, host=args.host, port=args.port,
                            log_level=args.log_level, workers=1)
    server = uvicorn.Server(config)

    # Report readiness only once the port is actually bound.
    original_startup = server.startup

    async def startup(sockets=None):
        await original_startup(sockets=sockets)
        _sd_notify("READY=1")

    server.startup = startup

    try:
        server.run()
    finally:
        _sd_notify("STOPPING=1")
        if listener is not None:
            listener.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

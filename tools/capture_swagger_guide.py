#!/usr/bin/env python3
"""Capture the screenshots used by docs/api-guide.md.

Drives a real browser against a real API instance, so every image in the
guide shows what the user will actually see — no mockups, no edited
screenshots. The API runs with the **dry-run transport**: no ONTAP cluster is
ever contacted, and the job files land in a temporary directory that is
deleted afterwards.

    python3 -m pip install playwright        # browsers are pre-installed
    python3 tools/capture_swagger_guide.py

Re-run it whenever the API surface or the Swagger version changes.
"""

import os
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "docs", "images")
PORT = 8321
BASE = f"http://127.0.0.1:{PORT}"
GLOBAL_TOKEN = "SuperAdmin-Demo-Token"

# Fictional but realistic names; nothing here maps to a real estate.
CREATE_BODY = {
    "source_cluster": "clu-legacy-01",
    "pivot_cluster": "clu-pivot-01",
    "dest_cluster": "clu-prod-01",
    "dr_cluster": "clu-dr-01",
    "volume": "vol_shared_data",
    "source_vserver": "svm_legacy",
    "pivot_vserver": "svm_pivot",
    "dest_vserver": "svm_prod",
    "dr_vserver": "svm_dr",
    "create_mode": "pivot-only",
    "dry_run": True,
}


# =============================================================================
# The API under test
# =============================================================================

def wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"the API never bound port {port}")


def mint_demo_token(store, qtree, actions, label, clear):
    """Register a chosen, obviously fake delegated token.

    upsert() only ever mints random tokens — right in production, wrong in a
    screenshot. The store's own hashing helpers are used, so the record is
    exactly what authenticate() expects to find.
    """
    from netapp_migration.security.tokens import _hash_token, _now, _token_id

    digest = _hash_token(clear, store._salt)
    record = {"id": _token_id(digest), "hash": digest, "qtrees": [qtree],
              "actions": store.validate_actions(actions), "label": label,
              "created_at": _now(), "updated_at": _now()}
    store._data["tokens"][record["id"]] = record
    store._write()
    return record["id"]


def start_api(workdir: str):
    """Start the API locked, in a thread, and return (store, thread)."""
    os.environ["NETAPP_MIGRATION_JOB_DIR"] = os.path.join(workdir, "jobs")
    os.makedirs(os.environ["NETAPP_MIGRATION_JOB_DIR"], exist_ok=True)

    from netapp_migration.security.tokens import TokenStore

    store = TokenStore(os.path.join(workdir, "tokens.enc"))
    store.initialise(GLOBAL_TOKEN)
    # Fixed, obviously fake tokens: they end up visible in the screenshots,
    # where a random-looking string would read as a leaked secret.
    scoped = "DEMO-TOKEN-finance-only"
    mint_demo_token(store, "q_finance", ["test", "clone", "acl", "status"],
                    "Finance", scoped)
    mint_demo_token(store, "q_hr", ["test", "status"], "HR",
                    "DEMO-TOKEN-hr-only")
    store.lock()

    from netapp_migration.interfaces.api import serve

    thread = threading.Thread(
        target=serve.main,
        args=([f"--host", "127.0.0.1", "--port", str(PORT),
               "--token-store", store.path, "--start-locked",
               "--unlock-socket", os.path.join(workdir, "unlock.sock"),
               "--log-level", "warning"],),
        daemon=True)
    thread.start()
    wait_for_port(PORT)
    return store, scoped, os.path.join(workdir, "unlock.sock")


# =============================================================================
# Swagger UI driving
# =============================================================================

class Swagger:
    """Thin wrapper over the bits of Swagger UI this guide walks through."""

    def __init__(self, page):
        self.page = page
        self.index = 0

    # -- plumbing ----------------------------------------------------------
    def shot(self, name: str, target=None, full: bool = False) -> str:
        self.index += 1
        path = os.path.join(OUT_DIR, f"{self.index:02d}-{name}.png")
        if target is None:
            self.page.screenshot(path=path, full_page=full)
        else:
            target.screenshot(path=path)
        print(f"  {os.path.relpath(path, ROOT)}")
        return path

    def open(self) -> None:
        self.page.goto(f"{BASE}/docs", wait_until="networkidle")
        self.page.wait_for_selector(".opblock", timeout=30_000)

    def operation(self, method: str, path: str):
        """The <div class="opblock"> of one endpoint."""
        block = self.page.locator(
            f'.opblock:has(.opblock-summary-path[data-path="{path}"])'
            f'.opblock-{method.lower()}')
        block.first.scroll_into_view_if_needed()
        return block.first

    def expand(self, method: str, path: str):
        block = self.operation(method, path)
        if "is-open" not in (block.get_attribute("class") or ""):
            block.locator(".opblock-summary").click()
            self.page.wait_for_timeout(300)
        return block

    def try_it(self, block):
        # Once an operation is in try-out mode the same class also carries
        # "Cancel" and "Reset"; only the bare button opens the form.
        button = block.locator(
            "button.try-out__btn:not(.cancel):not(.reset)")
        if button.count():
            button.first.click()
            self.page.wait_for_timeout(200)

    def fill_body(self, block, body: str):
        area = block.locator("textarea.body-param__text")
        if area.count():
            area.first.fill(body)

    def fill_path_param(self, block, name: str, value: str):
        field = block.locator(f'input[placeholder="{name}"]')
        if not field.count():
            field = block.locator("tr td.parameters-col_name:has-text('%s') "
                                  "~ td input" % name)
        if field.count():
            field.first.fill(value)

    def execute(self, block):
        block.locator("button.execute").click()
        # The live response table only appears once the call has returned.
        block.locator(".live-responses-table").wait_for(timeout=30_000)
        self.page.wait_for_timeout(400)
        return block

    def collapse_all(self):
        for block in self.page.locator(".opblock.is-open").all():
            block.locator(".opblock-summary").click()
        self.page.wait_for_timeout(200)

    # -- one full step -----------------------------------------------------
    def step(self, name: str, method: str, path: str, body: str = None,
             params: dict = None):
        block = self.expand(method, path)
        self.try_it(block)
        if params:
            for key, value in params.items():
                self.fill_path_param(block, key, value)
        if body is not None:
            self.fill_body(block, body)
        self.execute(block)
        self.shot(name, block)
        self.collapse_all()

    # -- the Authorize dialog ----------------------------------------------
    def authorize(self, token: str, shot_name: str = None):
        self.page.locator("button.authorize").first.click()
        self.page.wait_for_selector(".auth-container", timeout=10_000)
        field = self.page.locator(".auth-container input[type='text']")
        if field.count():
            field.first.fill(token)
        if shot_name:
            self.shot(shot_name, self.page.locator(".dialog-ux .modal-ux"))
        self.page.locator(".auth-btn-wrapper button.authorize").click()
        self.page.wait_for_timeout(300)
        close = self.page.locator(".btn-done, button:has-text('Close')")
        if close.count():
            close.first.click()
        self.page.wait_for_timeout(200)

    def logout(self):
        self.page.locator("button.authorize").first.click()
        self.page.wait_for_selector(".auth-container", timeout=10_000)
        out = self.page.locator("button.btn-done ~ button, button:has-text('Logout')")
        if out.count():
            out.first.click()
        self.page.wait_for_timeout(200)
        close = self.page.locator(".btn-done, button:has-text('Close')")
        if close.count():
            close.first.click()
        self.page.wait_for_timeout(200)


# =============================================================================
# The walkthrough
# =============================================================================

def capture(page, store, scoped_token, unlock_socket):
    import json

    ui = Swagger(page)
    ui.open()

    # -- 1. the API is locked when it starts -------------------------------
    ui.step("locked-503", "get", "/api/v1/migrations")

    from netapp_migration.interfaces.api.unlock import request_unlock
    print("  unlocking the API...")
    request_unlock(unlock_socket, GLOBAL_TOKEN)

    # -- 2. the landing page -----------------------------------------------
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".opblock", timeout=30_000)
    ui.shot("overview", full=True)

    # -- 3. authentication --------------------------------------------------
    ui.step("no-token-401", "get", "/api/v1/auth/whoami")
    ui.authorize(GLOBAL_TOKEN, "authorize-dialog")
    ui.step("whoami-super-admin", "get", "/api/v1/auth/whoami")
    ui.step("health", "get", "/api/v1/health")

    # -- 4. delegating tokens ----------------------------------------------
    ui.step("scopes-import", "post", "/api/v1/auth/scopes/import",
            body=json.dumps({
                "csv": "qtree,token,actions,label\n"
                       "q_ops,NEW_TOKEN,\"test,clone\",Operations\n"},
                indent=2))
    ui.step("scopes-list", "get", "/api/v1/auth/scopes")

    # -- 5. before doing anything: is it feasible? -------------------------
    ui.step("preflight-create", "post", "/api/v1/preflight/create",
            body=json.dumps(CREATE_BODY, indent=2))

    # -- 6. create the cascade ---------------------------------------------
    ui.step("create", "post", "/api/v1/migrations",
            body=json.dumps(CREATE_BODY, indent=2))

    job_id = _newest_job_id()
    print(f"  job created: {job_id}")

    ui.step("list-migrations", "get", "/api/v1/migrations")
    ui.step("status", "get", "/api/v1/migrations/{job_id}/status",
            params={"job_id": job_id})

    # -- 7. per-qtree actions ----------------------------------------------
    ui.step("preflight-test", "post",
            "/api/v1/migrations/{job_id}/preflight/{action}",
            params={"job_id": job_id, "action": "test"},
            body=json.dumps({"qtrees": "q_finance",
                             "volume_map": {"q_finance": "vol_fin_prod"}},
                            indent=2))
    ui.step("test", "post", "/api/v1/migrations/{job_id}/test",
            params={"job_id": job_id},
            body=json.dumps({"qtrees": "q_finance", "validity_days": 7,
                             "volume_map": {"q_finance": "vol_fin_prod"}},
                            indent=2))
    ui.step("clone", "post", "/api/v1/migrations/{job_id}/clone",
            params={"job_id": job_id},
            body=json.dumps({"qtrees": "q_finance", "fresh": False,
                             "volume_map": {"q_finance": "vol_fin_prod"}},
                            indent=2))
    ui.step("acl", "post", "/api/v1/migrations/{job_id}/acl",
            params={"job_id": job_id},
            body=json.dumps({"ad_groups": ["CORP\\\\grp_finance_rw"],
                             "acl_path": "/vol_fin_prod/projects",
                             "acl_rights": "full-control"}, indent=2))

    # -- 8. a scoped token hitting its limits ------------------------------
    ui.logout()
    ui.authorize(scoped_token, "authorize-scoped")
    ui.step("scoped-forbidden-qtree", "post",
            "/api/v1/migrations/{job_id}/test",
            params={"job_id": job_id},
            body=json.dumps({"qtrees": "q_hr",
                             "volume_map": {"q_hr": "vol_hr_prod"}}, indent=2))
    ui.step("scoped-forbidden-action", "post", "/api/v1/migrations",
            body=json.dumps(CREATE_BODY, indent=2))


def _newest_job_id() -> str:
    """The job the create step just wrote."""
    from netapp_migration.core.jobs import JobStore
    from netapp_migration.config import job_dir

    directory = job_dir()
    for _ in range(50):
        names = [n for n in os.listdir(directory)
                 if n.startswith("netapp_migration_") and n.endswith(".json")]
        if names:
            newest = max(names, key=lambda n: os.path.getmtime(
                os.path.join(directory, n)))
            return newest[len("netapp_migration_"):-len(".json")]
        time.sleep(0.2)
    raise RuntimeError("no job file was created")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is missing: python3 -m pip install playwright",
              file=sys.stderr)
        return 2

    workdir = tempfile.mkdtemp(prefix="swagger-guide-")
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"Working directory: {workdir}")
    print(f"Screenshots:       {os.path.relpath(OUT_DIR, ROOT)}/")

    store, scoped_token, unlock_socket = start_api(workdir)
    print(f"API listening on {BASE} (locked)")

    executable = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
    with sync_playwright() as pw:
        launch = {"args": ["--no-sandbox", "--force-color-profile=srgb"]}
        if os.path.exists(executable):
            launch["executable_path"] = executable
        browser = pw.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1360, "height": 900},
                                device_scale_factor=1)
        try:
            capture(page, store, scoped_token, unlock_socket)
        finally:
            browser.close()

    shutil.rmtree(workdir, ignore_errors=True)
    optimise()
    print("Done.")
    return 0


def optimise() -> None:
    """Palette-compress the captures: these are flat UI colours, so 256
    entries lose nothing visible and roughly a third of the bytes survive.

    Skipped silently when Pillow is absent — the screenshots are simply
    heavier then.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow absent: screenshots left uncompressed)")
        return

    before = after = 0
    for name in sorted(os.listdir(OUT_DIR)):
        if not name.endswith(".png"):
            continue
        path = os.path.join(OUT_DIR, name)
        before += os.path.getsize(path)
        image = Image.open(path).convert("RGB")
        image.quantize(colors=256, method=Image.MEDIANCUT).save(
            path, optimize=True)
        after += os.path.getsize(path)
    print(f"  compressed {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())

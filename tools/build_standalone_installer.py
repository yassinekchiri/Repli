#!/usr/bin/env python3
"""Build install-standalone.sh: one file carrying the whole application.

The target servers cannot reach GitHub, so the code cannot be cloned there;
only a single file can be carried in. This packs the source tree into a
gzipped tar, base64-encodes it and appends it to tools/installer_template.sh
behind a marker line. The resulting script unpacks itself, checks the
payload's SHA-256 and installs from the machine's own pip index.

    python3 tools/build_standalone_installer.py

Re-run it after ANY change to the source, otherwise the installer ships a
stale copy. The archive is built deterministically (sorted entries, fixed
timestamps and ownership), so an unchanged tree produces a byte-identical
file and git shows no diff.
"""

import base64
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "tools", "installer_template.sh")
OUTPUT = os.path.join(ROOT, "install-standalone.sh")
MARKER = "__NETAPP_MIGRATION_PAYLOAD__"

# What the installed system needs. wheels/ is deliberately excluded: it is
# 42 MB of binaries, and this installer fetches from a package index instead.
PAYLOAD = [
    "netapp_migration",
    "tests",
    "docs",
    "netapp_cascade_migration.py",
    "requirements.txt",
    "requirements-dev.txt",
    "pytest.ini",
    "README.md",
    "README.fr.md",
]

EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv"}
# The Swagger walkthrough screenshots: ~1 MB of PNG that belongs in the
# repository, not in a file people carry onto a server by hand. The guide
# text itself is kept.
EXCLUDED_RELPATHS = {os.path.join("docs", "images")}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".swp")


def collect(root: str) -> list:
    """Every file to embed, as (absolute path, archive name), sorted."""
    found = []
    for entry in PAYLOAD:
        absolute = os.path.join(root, entry)
        if not os.path.exists(absolute):
            sys.exit(f"missing from the tree: {entry}")
        if os.path.isfile(absolute):
            found.append((absolute, entry))
            continue
        for dirpath, dirnames, filenames in os.walk(absolute):
            relative = os.path.relpath(dirpath, root)
            if any(relative == skip or relative.startswith(skip + os.sep)
                   for skip in EXCLUDED_RELPATHS):
                dirnames[:] = []
                continue
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for filename in sorted(filenames):
                if filename.endswith(EXCLUDED_SUFFIXES):
                    continue
                path = os.path.join(dirpath, filename)
                found.append((path, os.path.relpath(path, root)))
    return sorted(found, key=lambda pair: pair[1])


def build_archive(files: list) -> bytes:
    """Deterministic tar.gz: no mtimes, no uid/gid, no gzip timestamp."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=9,
                      format=tarfile.PAX_FORMAT) as tar:
        for path, name in files:
            info = tar.gettarinfo(path, arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            # Executable bit only where it is meaningful.
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            with open(path, "rb") as fh:
                tar.addfile(info, fh)
    return _strip_gzip_timestamp(raw.getvalue())


def _strip_gzip_timestamp(blob: bytes) -> bytes:
    """Zero the MTIME field of the gzip header (bytes 4..8)."""
    return blob[:4] + b"\x00\x00\x00\x00" + blob[8:]


def revision() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", ROOT, "describe", "--always", "--dirty", "--tags"],
            capture_output=True, text=True, check=True)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    files = collect(ROOT)
    archive = build_archive(files)
    digest = hashlib.sha256(archive).hexdigest()

    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        template = fh.read()

    if MARKER not in template:
        sys.exit(f"the template does not declare the marker {MARKER}")

    built = datetime.fromtimestamp(
        int(os.environ.get("SOURCE_DATE_EPOCH", time.time())), timezone.utc
    ).strftime("%Y-%m-%d")

    header = (template
              .replace("@PAYLOAD_SHA256@", digest)
              .replace("@PAYLOAD_SHA256_SHORT@", digest[:16])
              .replace("@BUILD_DATE@", built)
              .replace("@REVISION@", revision())
              .replace("@FILE_COUNT@", str(len(files)))
              .replace("@PAYLOAD_BYTES@", f"{len(archive):,}"))

    encoded = base64.b64encode(archive).decode("ascii")
    lines = [encoded[i:i + 76] for i in range(0, len(encoded), 76)]

    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header)
        fh.write("\n" + MARKER + "\n")
        fh.write("\n".join(lines))
        fh.write("\n")

    os.chmod(OUTPUT, 0o755)

    size = os.path.getsize(OUTPUT)
    print(f"{os.path.relpath(OUTPUT, ROOT)}: {len(files)} files, "
          f"payload {len(archive):,} B, script {size:,} B")
    print(f"sha256({MARKER}) = {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

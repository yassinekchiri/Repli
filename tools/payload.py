"""Shared machinery for the self-extracting scripts.

Two generated files embed a copy of this repository:

  * install-standalone.sh — source + deps, installs onto a server
  * repo-selfextract.sh   — the whole repository, extracts and stops

Both pack their payload the same way: a deterministic gzipped tar, base64
encoded, appended to a shell template behind a marker line. "Deterministic"
matters — sorted entries, zeroed timestamps and ownership, zeroed gzip
header time — so that rebuilding an unchanged tree produces a byte-identical
file instead of a multi-megabyte diff on every run.
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

EXCLUDED_DIRS = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv",
                 ".mypy_cache", ".idea", ".vscode"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".swp", ".swo")
LINE_WIDTH = 76


def collect(root: str, entries, excluded_relpaths=frozenset()) -> list:
    """Every file to embed, as (absolute path, archive name), sorted.

    `entries` are repository-relative names; a directory is walked, a file is
    taken as is. `excluded_relpaths` prunes subtrees (also relative to root).
    """
    found = []
    for entry in entries:
        absolute = os.path.join(root, entry)
        if not os.path.exists(absolute):
            sys.exit(f"missing from the tree: {entry}")
        if os.path.isfile(absolute):
            found.append((absolute, entry))
            continue
        for dirpath, dirnames, filenames in os.walk(absolute):
            relative = os.path.relpath(dirpath, root)
            if any(relative == skip or relative.startswith(skip + os.sep)
                   for skip in excluded_relpaths):
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


def revision(root: str = ROOT) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", root, "describe", "--always", "--dirty", "--tags"],
            capture_output=True, text=True, check=True)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build_date() -> str:
    return datetime.fromtimestamp(
        int(os.environ.get("SOURCE_DATE_EPOCH", time.time())), timezone.utc
    ).strftime("%Y-%m-%d")


def emit(template_path: str, output_path: str, marker: str,
         files: list, archive: bytes, extra=None) -> str:
    """Write <template with placeholders filled><marker><base64 payload>."""
    digest = hashlib.sha256(archive).hexdigest()

    with open(template_path, "r", encoding="utf-8") as fh:
        template = fh.read()
    if marker not in template:
        sys.exit(f"the template does not declare the marker {marker}")

    values = {
        "@PAYLOAD_SHA256@": digest,
        "@PAYLOAD_SHA256_SHORT@": digest[:16],
        "@BUILD_DATE@": build_date(),
        "@REVISION@": revision(),
        "@FILE_COUNT@": str(len(files)),
        "@PAYLOAD_BYTES@": f"{len(archive):,}",
    }
    values.update(extra or {})

    header = template
    for placeholder, value in values.items():
        header = header.replace(placeholder, value)

    encoded = base64.b64encode(archive).decode("ascii")
    lines = [encoded[i:i + LINE_WIDTH]
             for i in range(0, len(encoded), LINE_WIDTH)]

    with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header)
        fh.write("\n" + marker + "\n")
        fh.write("\n".join(lines))
        fh.write("\n")
    os.chmod(output_path, 0o755)

    print(f"{os.path.relpath(output_path, ROOT)}: {len(files)} files, "
          f"payload {len(archive):,} B, script "
          f"{os.path.getsize(output_path):,} B")
    print(f"sha256(payload) = {digest}")
    return digest

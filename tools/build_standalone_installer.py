#!/usr/bin/env python3
"""Build install-standalone.sh: one file carrying the whole application.

The target servers cannot reach GitHub, so the code cannot be cloned there;
only a single file can be carried in. This packs the source tree into a
gzipped tar, base64-encodes it and appends it to tools/installer_template.sh
behind a marker line. The resulting script unpacks itself, checks the
payload's SHA-256 and installs from the machine's own pip index.

    python3 tools/build_standalone_installer.py

Re-run it after ANY change to the source, otherwise the installer ships a
stale copy. The archive is built deterministically, so an unchanged tree
produces a byte-identical file and git shows no diff.

See tools/build_selfextract.py for the sibling script that carries the whole
repository and only extracts it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import payload

ROOT = payload.ROOT
TEMPLATE = os.path.join(ROOT, "tools", "installer_template.sh")
OUTPUT = os.path.join(ROOT, "install-standalone.sh")
MARKER = "__NETAPP_MIGRATION_PAYLOAD__"

# What the installed system needs. wheels/ is deliberately excluded: it is
# 42 MB of binaries, and this installer fetches from a package index instead.
ENTRIES = [
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

# The Swagger walkthrough screenshots: ~1 MB of PNG that belongs in the
# repository, not in a file people carry onto a server by hand. The guide
# text itself is kept.
EXCLUDED = {os.path.join("docs", "images")}


def main() -> int:
    files = payload.collect(ROOT, ENTRIES, EXCLUDED)
    archive = payload.build_archive(files)
    payload.emit(TEMPLATE, OUTPUT, MARKER, files, archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())

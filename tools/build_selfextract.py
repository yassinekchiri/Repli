#!/usr/bin/env python3
"""Build repo-selfextract.sh: the whole repository in one runnable file.

Same trick as install-standalone.sh (see tools/payload.py), different
purpose: this one installs nothing. It carries every tracked file except the
wheels/ directory, unpacks itself into a directory of your choosing, and
stops.

    python3 tools/build_selfextract.py

`git ls-files` decides *which* paths ship, so build artefacts, caches and
anything untracked stay out. The *content* is read from the working tree, as
install-standalone.sh does: an edit you have not committed yet is included.
Re-run after any change you want in the archive.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import payload

ROOT = payload.ROOT
TEMPLATE = os.path.join(ROOT, "tools", "selfextract_template.sh")
OUTPUT = os.path.join(ROOT, "repo-selfextract.sh")
MARKER = "__NETAPP_MIGRATION_REPO__"
DEFAULT_DIR = "netapp-migration"

# The one thing left out, and the reason this file is 3 MB instead of 45.
EXCLUDED_PREFIXES = ("wheels/",)
# Never ship the extractor inside itself: each rebuild would embed the
# previous build, and the file would double in size every time.
EXCLUDED_NAMES = {os.path.basename(OUTPUT)}


def tracked_files() -> list:
    """Repository-relative paths of everything tracked, minus the exclusions."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                             capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f"cannot list the repository's files: {exc}")

    names = [n for n in out.stdout.decode().split("\0") if n]
    kept = [n for n in names
            if not n.startswith(EXCLUDED_PREFIXES)
            and n not in EXCLUDED_NAMES]
    if not kept:
        sys.exit("git ls-files returned nothing to pack")
    return sorted(kept)


def main() -> int:
    names = tracked_files()

    missing = [n for n in names if not os.path.exists(os.path.join(ROOT, n))]
    if missing:
        sys.exit("tracked but absent from the working tree: "
                 + ", ".join(missing[:5]))

    files = sorted(((os.path.join(ROOT, n), n) for n in names),
                   key=lambda pair: pair[1])
    archive = payload.build_archive(files)
    payload.emit(TEMPLATE, OUTPUT, MARKER, files, archive,
                 extra={"@DEFAULT_DIR@": DEFAULT_DIR})

    dirty = [line for line in subprocess.run(
        ["git", "-C", ROOT, "status", "--porcelain"],
        capture_output=True, text=True).stdout.splitlines() if line.strip()]
    if dirty:
        print(f"NOTE: {len(dirty)} uncommitted change(s) in the working tree. "
              "Modified tracked files ARE in this archive as they stand; "
              "untracked files are not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

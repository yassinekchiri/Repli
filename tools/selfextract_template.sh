#!/usr/bin/env bash
#
# NetApp Cascade Migration — self-extracting copy of the repository.
#
# This ONE file carries the whole repository inside it, everything except the
# wheels/ directory (42 MB of binaries) and the git history. Nothing is
# installed and nothing on the machine is touched outside the directory you
# extract into: it unpacks, and stops.
#
# Use it to move the repository onto a machine that cannot reach the code
# repository. To *install* the tool instead, use install-standalone.sh, which
# also sets up a venv, a service and the token store.
#
#   bash repo-selfextract.sh                  # extract into ./@DEFAULT_DIR@
#   bash repo-selfextract.sh --into /opt/src  # extract somewhere else
#   bash repo-selfextract.sh --list           # show the contents, extract nothing
#   bash repo-selfextract.sh --check          # verify integrity only
#
# See --help for every option.
#
#   Built     : @BUILD_DATE@
#   Revision  : @REVISION@
#   Payload   : @FILE_COUNT@ files, @PAYLOAD_BYTES@ bytes, sha256 @PAYLOAD_SHA256_SHORT@

set -Eeuo pipefail

# Empty when piped (`curl | bash`) — caught with a clear message below.
SELF="${BASH_SOURCE[0]:-}"
PAYLOAD_SHA256="@PAYLOAD_SHA256@"
PAYLOAD_MARKER="__NETAPP_MIGRATION_REPO__"
BUILD_REVISION="@REVISION@"
BUILD_DATE="@BUILD_DATE@"
FILE_COUNT="@FILE_COUNT@"

TARGET="./@DEFAULT_DIR@"
MODE="extract"
FORCE=0

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step() { printf '\n%s==> %s%s\n' "${BOLD}${BLUE}" "$*" "${RESET}"; }
ok()   { printf '    %s[ OK ]%s %s\n' "${GREEN}"  "${RESET}" "$*"; }
warn() { printf '    %s[WARN]%s %s\n' "${YELLOW}" "${RESET}" "$*"; }
info() { printf '           %s\n' "$*"; }
die()  { printf '\n%s[FAIL]%s %s\n' "${RED}" "${RESET}" "$*" >&2; exit 1; }

usage() {
    # The header block: from line 2 up to the first blank line.
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --into DIR     extract here (default: ${TARGET})
  --list         list the payload's contents and exit
  --check        verify the payload's checksum and exit
  --sha256       print the payload's SHA-256 and exit
  --force        extract even if the target directory is not empty
                 (existing files with the same names are overwritten)
  -h, --help     this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --into)     TARGET="${2:?--into needs a directory}"; shift 2 ;;
        --list)     MODE="list"; shift ;;
        --check)    MODE="check"; shift ;;
        --sha256)   MODE="sha256"; shift ;;
        --force)    FORCE=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          die "unknown option '$1' (try --help)" ;;
    esac
done

# ----------------------------------------------------------------------------
# Locating the payload
# ----------------------------------------------------------------------------
# The payload lives inside this very file, so the script must exist on disk.
# `curl ... | bash` gives $0 = "bash" and there would be nothing to unpack.
[[ -f "${SELF}" ]] || die "this archive carries its payload inside itself and cannot be piped.
       Save it to a file first:
         curl -o repo-selfextract.sh <url> && bash repo-selfextract.sh"

PAYLOAD_LINE=$(awk -v marker="${PAYLOAD_MARKER}" \
    '$0 == marker { print NR + 1; exit }' "${SELF}")
[[ -n "${PAYLOAD_LINE}" ]] \
    || die "payload marker not found — the file is truncated or corrupted"

payload_bytes() { tail -n "+${PAYLOAD_LINE}" "${SELF}" | base64 -d; }

# base64/sha256sum come from coreutils and tar is everywhere, so no
# interpreter is needed. python3 is only a fallback for the odd minimal image.
HAVE_COREUTILS=1
for binary in base64 tar; do
    command -v "${binary}" >/dev/null 2>&1 || HAVE_COREUTILS=0
done
command -v sha256sum >/dev/null 2>&1 || HAVE_COREUTILS=0

PYTHON=""
for candidate in python3 python; do
    command -v "${candidate}" >/dev/null 2>&1 && { PYTHON="${candidate}"; break; }
done

if (( ! HAVE_COREUTILS )) && [[ -z "${PYTHON}" ]]; then
    die "neither coreutils (base64/sha256sum/tar) nor python3 is available —
       nothing here can unpack the payload"
fi

# ----------------------------------------------------------------------------
# Python fallback: decode, verify and extract in one pass
# ----------------------------------------------------------------------------
python_payload() {
    local action="$1" destination="${2:-}"
    "${PYTHON}" - "${SELF}" "${PAYLOAD_MARKER}" "${PAYLOAD_SHA256}" \
                 "${action}" "${destination}" <<'PYEXTRACT'
import base64, hashlib, io, os, sys, tarfile

script, marker, expected, action, destination = sys.argv[1:6]

with open(script, "rb") as fh:
    blob = fh.read()
at = blob.find(("\n" + marker + "\n").encode())
if at < 0:
    sys.exit("payload marker not found — the file is truncated or corrupted")

payload = base64.b64decode(blob[at + len(marker) + 2:], validate=False)
digest = hashlib.sha256(payload).hexdigest()

if action == "sha256":
    print(digest)
    raise SystemExit(0)

if digest != expected:
    sys.exit("payload checksum mismatch\n"
             f"  expected {expected}\n"
             f"  got      {digest}\n"
             "the file was altered in transit (mail, copy/paste, CRLF...)")

if action == "check":
    raise SystemExit(0)

with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
    members = tar.getmembers()
    for member in members:
        name = os.path.normpath(member.name)
        if name.startswith(("/", "..")) or member.issym() or member.islnk():
            sys.exit(f"refusing unsafe archive member: {member.name}")
    if action == "list":
        for member in members:
            print(f"{member.size:>9}  {member.name}")
    else:
        tar.extractall(destination)
        print(len(members))
PYEXTRACT
}

verify() {
    if (( HAVE_COREUTILS )); then
        local actual
        actual=$(payload_bytes | sha256sum | cut -d' ' -f1)
        [[ "${actual}" == "${PAYLOAD_SHA256}" ]] || die \
"payload checksum mismatch
       expected ${PAYLOAD_SHA256}
       got      ${actual}
       the file was altered in transit (mail, copy/paste, CRLF...)"
    else
        python_payload check >/dev/null
    fi
}

# ----------------------------------------------------------------------------
# Modes that never write anything
# ----------------------------------------------------------------------------
case "${MODE}" in
    sha256)
        if (( HAVE_COREUTILS )); then
            payload_bytes | sha256sum | cut -d' ' -f1
        else
            python_payload sha256
        fi
        exit 0 ;;
    check)
        verify
        step "Integrity verified"
        info "Revision : ${BUILD_REVISION}"
        info "Built    : ${BUILD_DATE}"
        info "Contents : ${FILE_COUNT} files"
        info "sha256   : ${PAYLOAD_SHA256}"
        exit 0 ;;
    list)
        verify
        if (( HAVE_COREUTILS )); then
            payload_bytes | tar -tzvf -
        else
            python_payload list
        fi
        exit 0 ;;
esac

# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
step "Self-extracting repository (${BUILD_REVISION}, ${FILE_COUNT} files)"

verify
ok "payload checksum verified"

if [[ -e "${TARGET}" ]]; then
    [[ -d "${TARGET}" ]] || die "${TARGET} exists and is not a directory"
    if [[ -n "$(ls -A "${TARGET}" 2>/dev/null)" ]] && (( ! FORCE )); then
        die "${TARGET} is not empty — pass --force to extract into it anyway,
       or --into <other-dir>"
    fi
    (( FORCE )) && warn "extracting into a non-empty directory (--force)"
else
    mkdir -p "${TARGET}"
fi

if (( HAVE_COREUTILS )); then
    # Refuse absolute paths and traversal rather than trusting the archive.
    if payload_bytes | tar -tzf - | grep -qE '^/|(^|/)\.\./'; then
        die "the archive contains unsafe paths — refusing to extract"
    fi
    payload_bytes | tar -xzf - -C "${TARGET}"
    EXTRACTED=$(payload_bytes | tar -tzf - | grep -vc '/$' || true)
else
    EXTRACTED=$(python_payload extract "${TARGET}")
fi
ok "${EXTRACTED} files extracted into ${TARGET}"

TARGET_ABS="$(cd "${TARGET}" && pwd)"

cat <<EOF

${BOLD}${GREEN}Done.${RESET}

  Directory : ${TARGET_ABS}
  Revision  : ${BUILD_REVISION} (built ${BUILD_DATE})

${BOLD}What is NOT in here${RESET}

  * wheels/  — 42 MB of offline Python packages. Needed only by install.sh;
               install-standalone.sh fetches from a package index instead.
  * .git/    — no history: this is a snapshot, not a clone. Commits made
               here cannot be pushed back.

${BOLD}Next steps${RESET}

  Read the documentation:
      ${TARGET_ABS}/README.md
      ${TARGET_ABS}/docs/api-guide.md          (illustrated Swagger walkthrough)

  Run the test suite (needs pytest):
      cd ${TARGET_ABS} && python3 -m pytest

  Install the tool from this copy:
      cd ${TARGET_ABS} && sudo bash install-standalone.sh
EOF

exit 0

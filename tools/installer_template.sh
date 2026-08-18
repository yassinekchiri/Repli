#!/usr/bin/env bash
#
# NetApp Cascade Migration — standalone installer (single file, online deps).
#
# This ONE file carries the whole application inside it. Nothing else needs to
# be downloaded: no git clone, no repository access. Only the Python
# dependencies are fetched, from the pip index the machine is already
# configured for (PyPI, an internal mirror, Artifactory...).
#
# Use install.sh instead when the machine has NO package index at all: that
# one installs from the bundled wheels/ directory and needs the full checkout.
#
#   sudo bash install-standalone.sh                       # full install
#   sudo bash install-standalone.sh --index-url https://artifactory/api/pypi/pypi/simple
#   bash install-standalone.sh --prefix "$HOME/netapp-migration" --no-service
#   bash install-standalone.sh --check                    # verify only
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
PAYLOAD_MARKER="__NETAPP_MIGRATION_PAYLOAD__"
BUILD_REVISION="@REVISION@"

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
PREFIX="/opt/netapp-migration"
SERVICE_USER="netappmig"
SERVICE_NAME="netapp-migration-api"
API_HOST="127.0.0.1"
API_PORT="8000"
MIN_PY_MINOR=9

INDEX_URL=""
EXTRA_INDEX_URL=""
TRUSTED_HOSTS=()
PIP_TIMEOUT=120
PIP_RETRIES=10

INSTALL_SERVICE=1
INIT_TOKENS=1
RUN_TESTS=1
CHECK_ONLY=0
ASSUME_YES=0

# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; BLUE=$'\033[34m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

step()  { printf '\n%s==> %s%s\n' "${BOLD}${BLUE}" "$*" "${RESET}"; }
ok()    { printf '    %s[ OK ]%s %s\n'   "${GREEN}"  "${RESET}" "$*"; }
warn()  { printf '    %s[WARN]%s %s\n'   "${YELLOW}" "${RESET}" "$*"; }
info()  { printf '           %s\n' "$*"; }
die()   { printf '\n%s[FAIL]%s %s\n' "${RED}" "${RESET}" "$*" >&2; exit 1; }

WORKDIR=""
# Must always succeed: a failing EXIT trap re-triggers the ERR trap and
# would print a bogus "aborted" after a perfectly good run.
cleanup() {
    [[ -n "${WORKDIR}" && -d "${WORKDIR}" ]] && rm -rf "${WORKDIR}"
    return 0
}
trap cleanup EXIT

on_error() {
    printf '\n%s[FAIL]%s installation aborted (line %s).\n' \
        "${RED}" "${RESET}" "$1" >&2
    printf '       Fix the cause and re-run — the script is safe to re-run.\n' >&2
}
trap 'on_error $LINENO' ERR

usage() {
    # The header block: from line 2 up to the first blank line.
    sed -n '2,/^$/p' "${SELF}" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --prefix PATH         install directory        (default: ${PREFIX})
  --user NAME           service account          (default: ${SERVICE_USER})
  --host ADDR           API bind address         (default: ${API_HOST})
  --port PORT           API port                 (default: ${API_PORT})
  --service-name NAME   systemd unit name        (default: ${SERVICE_NAME})
  --index-url URL       pip index (internal mirror / Artifactory)
  --extra-index-url URL additional pip index
  --trusted-host HOST   pip trusted host (repeatable, for a self-signed mirror)
  --pip-timeout SEC     per-request pip timeout  (default: ${PIP_TIMEOUT})
  --no-service          do not install the systemd unit
  --no-tokens           do not initialise the token store now
  --no-tests            skip the test suite
  --check               verify prerequisites only, change nothing
  --extract-only DIR    unpack the embedded source into DIR and stop
  -y, --yes             do not ask for confirmation
  -h, --help            this help
EOF
}

# ----------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------
EXTRACT_ONLY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)          PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
        --user)            SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --host)            API_HOST="${2:?--host needs an address}"; shift 2 ;;
        --port)            API_PORT="${2:?--port needs a number}"; shift 2 ;;
        --service-name)    SERVICE_NAME="${2:?--service-name needs a name}"; shift 2 ;;
        --index-url)       INDEX_URL="${2:?--index-url needs a URL}"; shift 2 ;;
        --extra-index-url) EXTRA_INDEX_URL="${2:?--extra-index-url needs a URL}"; shift 2 ;;
        --trusted-host)    TRUSTED_HOSTS+=("${2:?--trusted-host needs a host}"); shift 2 ;;
        --pip-timeout)     PIP_TIMEOUT="${2:?--pip-timeout needs seconds}"; shift 2 ;;
        --no-service)      INSTALL_SERVICE=0; shift ;;
        --no-tokens)       INIT_TOKENS=0; shift ;;
        --no-tests)        RUN_TESTS=0; shift ;;
        --check)           CHECK_ONLY=1; shift ;;
        --extract-only)    EXTRACT_ONLY="${2:?--extract-only needs a directory}"; shift 2 ;;
        -y|--yes)          ASSUME_YES=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "unknown option '$1' (try --help)" ;;
    esac
done

VENV="${PREFIX}/.venv"
PYBIN="${VENV}/bin/python"
JOB_DIR="${PREFIX}/jobs"
LOG_DIR="${PREFIX}/logs"
ETC_DIR="${PREFIX}/etc"
TOKEN_STORE="${ETC_DIR}/netapp_tokens.enc"
CREDS_FILE="${ETC_DIR}/creds.json"

# ----------------------------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------------------------
step "Checking prerequisites"

# The payload lives inside this very file, so the script must exist on disk.
# `curl ... | bash` gives $0 = "bash" and there would be nothing to unpack.
if [[ ! -f "${SELF}" ]]; then
    die "this installer carries its payload inside itself and cannot be piped.
       Save it to a file first:
         curl -o install-standalone.sh <url> && bash install-standalone.sh"
fi
ok "self-contained installer: ${SELF}"

# Python interpreter: prefer the newest usable one on the machine.
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    major=$("${candidate}" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)
    minor=$("${candidate}" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)
    if (( major == 3 && minor >= MIN_PY_MINOR )); then
        PYTHON="${candidate}"
        break
    fi
done
[[ -n "${PYTHON}" ]] || die "no Python 3.${MIN_PY_MINOR}+ found — install python3 first (this installer cannot download it)"
PY_VERSION=$("${PYTHON}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
ok "Python ${PY_VERSION} (${PYTHON})"

# The venv module is a separate package on Debian/Ubuntu.
"${PYTHON}" -c 'import venv' >/dev/null 2>&1 || die "the 'venv' module is missing — install it first:
       Debian/Ubuntu : apt-get install python3-venv
       RHEL/Rocky    : dnf install python3-libs"
ok "venv module available"

command -v systemctl >/dev/null 2>&1 || {
    if (( INSTALL_SERVICE )); then
        warn "systemd not found — the service will not be installed"
        INSTALL_SERVICE=0
    fi
}

# ----------------------------------------------------------------------------
# 2. Unpacking the embedded payload
# ----------------------------------------------------------------------------
# Done with Python rather than base64/tar so that the same code path works on
# every distribution, and so the integrity check is not optional.
extract_payload() {
    local dest="$1"
    "${PYTHON}" - "${SELF}" "${PAYLOAD_MARKER}" "${PAYLOAD_SHA256}" "${dest}" <<'PYEXTRACT'
import base64, hashlib, io, os, sys, tarfile

script, marker, expected, dest = sys.argv[1:5]

with open(script, "rb") as fh:
    blob = fh.read()

needle = ("\n" + marker + "\n").encode()
at = blob.find(needle)
if at < 0:
    sys.exit("payload marker not found — the file is truncated or corrupted")

payload = base64.b64decode(blob[at + len(needle):], validate=False)
digest = hashlib.sha256(payload).hexdigest()
if digest != expected:
    sys.exit("payload checksum mismatch\n"
             f"  expected {expected}\n"
             f"  got      {digest}\n"
             "the file was altered in transit (mail, copy/paste, CRLF...)")

count = 0
with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
    for member in tar.getmembers():
        name = os.path.normpath(member.name)
        if name.startswith(("/", "..")) or member.issym() or member.islnk():
            sys.exit(f"refusing unsafe archive member: {member.name}")
        count += 1
    tar.extractall(dest)

print(count)
PYEXTRACT
}

step "Unpacking the embedded application"
WORKDIR="$(mktemp -d)"
FILE_COUNT="$(extract_payload "${WORKDIR}")" \
    || die "could not unpack the embedded payload"
ok "${FILE_COUNT} files unpacked and checksum verified"
[[ -f "${WORKDIR}/netapp_cascade_migration.py" ]] \
    || die "the payload does not contain the application"

if [[ -n "${EXTRACT_ONLY}" ]]; then
    mkdir -p "${EXTRACT_ONLY}"
    cp -r "${WORKDIR}/." "${EXTRACT_ONLY}/"
    ok "source extracted to ${EXTRACT_ONLY} — nothing else was changed"
    exit 0
fi

if (( CHECK_ONLY )); then
    step "Check mode: prerequisites satisfied, nothing was changed"
    info "Python ${PY_VERSION}, payload verified, ${FILE_COUNT} files ready."
    info "Dependencies would be fetched from: ${INDEX_URL:-the configured pip index}"
    exit 0
fi

# Writing outside $HOME needs root.
NEED_ROOT=0
parent="$(dirname "${PREFIX}")"
[[ -w "${parent}" ]] || NEED_ROOT=1
[[ -d "${PREFIX}" && ! -w "${PREFIX}" ]] && NEED_ROOT=1
if (( NEED_ROOT )) && [[ "$(id -u)" -ne 0 ]]; then
    die "root privileges are required to write ${PREFIX} — re-run with sudo, or pass --prefix \$HOME/netapp-migration"
fi
(( INSTALL_SERVICE )) && [[ "$(id -u)" -ne 0 ]] && {
    warn "not root: the systemd unit will not be installed"
    INSTALL_SERVICE=0
}

# ----------------------------------------------------------------------------
# 3. Confirmation
# ----------------------------------------------------------------------------
step "Installation plan"
info "Revision      : ${BUILD_REVISION}"
info "Install dir   : ${PREFIX}"
info "Python        : ${PYTHON} (${PY_VERSION})"
info "Dependencies  : ${INDEX_URL:-configured pip index} (network required)"
info "Job directory : ${JOB_DIR}"
info "Token store   : ${TOKEN_STORE}"
if (( INSTALL_SERVICE )); then
    info "Service       : ${SERVICE_NAME} (${SERVICE_USER}), ${API_HOST}:${API_PORT}"
else
    info "Service       : not installed"
fi

if (( ! ASSUME_YES )); then
    read -r -p "    Proceed? [y/N] " answer
    [[ "${answer,,}" == "y" ]] || { echo "    Aborted."; exit 0; }
fi

# ----------------------------------------------------------------------------
# 4. Service account
# ----------------------------------------------------------------------------
if (( INSTALL_SERVICE )); then
    step "Service account"
    if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
        ok "user '${SERVICE_USER}' already exists"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin \
                "${SERVICE_USER}" 2>/dev/null \
            || useradd --system --no-create-home --shell /sbin/nologin \
                "${SERVICE_USER}"
        ok "system user '${SERVICE_USER}' created (no login, no home)"
    fi
fi

# ----------------------------------------------------------------------------
# 5. Directories and code
# ----------------------------------------------------------------------------
step "Installing the code into ${PREFIX}"
mkdir -p "${PREFIX}" "${JOB_DIR}" "${LOG_DIR}" "${ETC_DIR}"

for item in netapp_migration netapp_cascade_migration.py requirements.txt \
            requirements-dev.txt docs tests pytest.ini \
            README.md README.fr.md; do
    [[ -e "${WORKDIR}/${item}" ]] || continue
    rm -rf "${PREFIX:?}/${item}"
    cp -r "${WORKDIR}/${item}" "${PREFIX}/"
done
ok "code installed"

chmod 700 "${ETC_DIR}"
chmod 750 "${JOB_DIR}" "${LOG_DIR}"
ok "directories ready (etc 700, jobs/logs 750)"

# ----------------------------------------------------------------------------
# 6. Virtual environment and dependencies (online)
# ----------------------------------------------------------------------------
step "Creating the virtual environment"
if [[ -x "${PYBIN}" ]]; then
    ok "virtual environment already present — reusing it"
else
    "${PYTHON}" -m venv "${VENV}"
    ok "virtual environment created: ${VENV}"
fi

PIP_ARGS=(--disable-pip-version-check
          --timeout "${PIP_TIMEOUT}" --retries "${PIP_RETRIES}")
[[ -n "${INDEX_URL}" ]]       && PIP_ARGS+=(--index-url "${INDEX_URL}")
[[ -n "${EXTRA_INDEX_URL}" ]] && PIP_ARGS+=(--extra-index-url "${EXTRA_INDEX_URL}")
for host in "${TRUSTED_HOSTS[@]+"${TRUSTED_HOSTS[@]}"}"; do
    PIP_ARGS+=(--trusted-host "${host}")
done

step "Installing the dependencies (network)"
info "an internal mirror behind a slow link needs the retries below;"
info "timeout ${PIP_TIMEOUT}s, ${PIP_RETRIES} retries per package"

# pip itself first: an old pip rejects recent manylinux tags. Not fatal —
# some mirrors do not carry pip.
if "${PYBIN}" -m pip install --quiet "${PIP_ARGS[@]}" \
        --upgrade pip setuptools wheel 2>/dev/null; then
    ok "pip $("${PYBIN}" -m pip --version | awk '{print $2}') ready"
else
    warn "could not upgrade pip — continuing with $("${PYBIN}" -m pip --version | awk '{print $2}')"
fi

"${PYBIN}" -m pip install "${PIP_ARGS[@]}" -r "${PREFIX}/requirements.txt" \
    || die "dependency installation failed.
       Behind a proxy or an internal mirror, point pip at it:
         --index-url https://<mirror>/api/pypi/pypi/simple --trusted-host <mirror>
       With no index at all, use install.sh with the bundled wheels/ instead."
ok "runtime dependencies installed"

if (( RUN_TESTS )) && [[ -f "${PREFIX}/requirements-dev.txt" ]]; then
    if "${PYBIN}" -m pip install --quiet "${PIP_ARGS[@]}" \
            -r "${PREFIX}/requirements-dev.txt"; then
        ok "test dependencies installed"
    else
        warn "test dependencies unavailable — tests will be skipped"
        RUN_TESTS=0
    fi
fi

# ----------------------------------------------------------------------------
# 7. Verification
# ----------------------------------------------------------------------------
step "Verifying the installation"
# From ${PREFIX}: the package is installed as a directory, not on sys.path.
( cd "${PREFIX}" && "${PYBIN}" - <<'PYCHECK'
from cryptography.fernet import Fernet
import fastapi, uvicorn, requests
from netapp_migration.core.engine import MigrationEngine
from netapp_migration.security.tokens import TokenStore
from netapp_migration.interfaces.api.app import app
print(f"    imports OK — fastapi {fastapi.__version__}, "
      f"{len(app.routes)} API routes")
PYCHECK
) || die "the installed package does not import correctly"
ok "package imports correctly"

"${PYBIN}" "${PREFIX}/netapp_cascade_migration.py" --help >/dev/null \
    || die "the CLI does not start"
ok "CLI responds"

if (( RUN_TESTS )); then
    # pytest.ini already carries -q; adding another would hide the summary.
    if ( cd "${PREFIX}" && "${PYBIN}" -m pytest >/tmp/netapp_install_tests.log 2>&1 ); then
        summary=$(grep -oE '[0-9]+ passed[^ ]*' /tmp/netapp_install_tests.log | tail -1)
        ok "test suite passed (${summary:-all tests})"
    else
        warn "the test suite reported failures — see /tmp/netapp_install_tests.log"
    fi
fi

# ----------------------------------------------------------------------------
# 8. Credentials template
# ----------------------------------------------------------------------------
step "ONTAP credentials"
if [[ -f "${CREDS_FILE}" ]]; then
    ok "${CREDS_FILE} already exists — left untouched"
else
    umask 077
    cat > "${CREDS_FILE}" <<'EOF'
{
  "defaults": {
    "username": "mutrepli",
    "password": "CHANGE_ME",
    "verify_ssl": false,
    "port": 443
  },
  "clusters": {
    "CLUSTER_SOURCE": {},
    "CLUSTER_PIVOT":  {},
    "CLUSTER_PROD":   {},
    "CLUSTER_DR":     {}
  }
}
EOF
    chmod 600 "${CREDS_FILE}"
    ok "template written: ${CREDS_FILE} (mode 600)"
    warn "edit it and replace CHANGE_ME with the mutrepli password"
fi

# ----------------------------------------------------------------------------
# 9. systemd unit
# ----------------------------------------------------------------------------
if (( INSTALL_SERVICE )); then
    step "systemd service"
    UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
    cat > "${UNIT}" <<EOF
[Unit]
Description=NetApp Cascade Migration API
Documentation=file://${PREFIX}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PREFIX}
Environment=NETAPP_MIGRATION_CONFIG=${CREDS_FILE}
Environment=NETAPP_MIGRATION_JOB_DIR=${JOB_DIR}
Environment=NETAPP_TOKEN_STORE=${TOKEN_STORE}

# The global token is typed by a super admin on every start: the unit reads
# it from stdin, so the service is NEVER started automatically.
ExecStart=${PYBIN} -m netapp_migration.interfaces.api.serve \\
    --host ${API_HOST} --port ${API_PORT} --token-stdin
StandardInput=tty
TTYPath=/dev/console

# No automatic restart: an unattended restart could not supply the token,
# and must not silently re-open a locked API.
Restart=no

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${JOB_DIR} ${LOG_DIR}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
EOF
    chmod 644 "${UNIT}"
    systemctl daemon-reload
    ok "unit installed: ${UNIT}"
    info "deliberately NOT enabled at boot: the API needs the global token"
fi

# ----------------------------------------------------------------------------
# 10. Ownership
# ----------------------------------------------------------------------------
if (( INSTALL_SERVICE )) && id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    step "Permissions"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${JOB_DIR}" "${LOG_DIR}" "${ETC_DIR}"
    ok "jobs, logs and etc owned by ${SERVICE_USER}"
fi

# ----------------------------------------------------------------------------
# 11. Token store
# ----------------------------------------------------------------------------
if (( INIT_TOKENS )); then
    step "Token store (super admin)"
    if [[ -f "${TOKEN_STORE}" ]]; then
        ok "${TOKEN_STORE} already exists — left untouched"
    elif [[ ! -t 0 ]]; then
        warn "not an interactive terminal — token store not initialised"
        info "run it manually:"
        info "  ${PYBIN} ${PREFIX}/netapp_cascade_migration.py \\"
        info "      --action tokens-init --token-store ${TOKEN_STORE}"
    else
        info "You are about to choose the GLOBAL token."
        info "It is never written anywhere and cannot be recovered:"
        info "it encrypts the store AND identifies the super admin."
        if "${PYBIN}" "${PREFIX}/netapp_cascade_migration.py" \
                --action tokens-init --token-store "${TOKEN_STORE}"; then
            [[ -f "${TOKEN_STORE}" ]] && chmod 600 "${TOKEN_STORE}"
            if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
                chown "${SERVICE_USER}:${SERVICE_USER}" "${TOKEN_STORE}" 2>/dev/null || true
            fi
            ok "token store created: ${TOKEN_STORE}"
        else
            warn "token store not created — run --action tokens-init later"
        fi
    fi
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
CLI="${PYBIN} ${PREFIX}/netapp_cascade_migration.py"
cat <<EOF

${BOLD}${GREEN}Installation complete.${RESET}

  Install directory : ${PREFIX}
  Revision          : ${BUILD_REVISION}
  Python            : ${PY_VERSION}
  Credentials       : ${CREDS_FILE}
  Job directory     : ${JOB_DIR}
  Token store       : ${TOKEN_STORE}

${BOLD}Next steps${RESET}

  1. Fill in the ONTAP credentials:
       \$EDITOR ${CREDS_FILE}

  2. Create the mutrepli account on the four clusters
     (commands in README.md, section 2.5).

  3. Delegate per-qtree tokens:
       cat > scopes.csv <<'CSV'
       qtree,token,actions,label
       q_fin,NEW_TOKEN,"test,clone,acl",Finance
       CSV
       ${CLI} \\
           --action tokens-import --token-store ${TOKEN_STORE} \\
           --scope-csv scopes.csv --scope-out issued.csv

  4. Start the API (asks for the global token):
EOF
if (( INSTALL_SERVICE )); then
cat <<EOF
       systemctl start ${SERVICE_NAME}      # then type the token on the console
     or, in the foreground:
       ${PYBIN} -m netapp_migration.interfaces.api.serve \\
           --host ${API_HOST} --port ${API_PORT}
EOF
else
cat <<EOF
       ${PYBIN} -m netapp_migration.interfaces.api.serve \\
           --host ${API_HOST} --port ${API_PORT}
EOF
fi
cat <<EOF

  5. Check it answers:
       curl -s http://${API_HOST}:${API_PORT}/api/v1/health
       Swagger UI: http://${API_HOST}:${API_PORT}/docs

Full documentation: ${PREFIX}/README.md and ${PREFIX}/docs/architecture.md
EOF

exit 0

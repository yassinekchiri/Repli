#!/usr/bin/env bash
#
# NetApp Cascade Migration — offline installer for a fresh VM.
#
# Installs everything from the bundled wheels/ directory: no PyPI, no mirror,
# no Internet. Safe to re-run: existing pieces are detected and kept.
#
#   sudo ./install.sh                         # full install, defaults
#   sudo ./install.sh --prefix /opt/netapp-migration --user migration
#   ./install.sh --no-service                 # no systemd unit
#   ./install.sh --check                      # verify only, change nothing
#
# See --help for every option.

set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/opt/netapp-migration"
SERVICE_USER="netappmig"
SERVICE_NAME="netapp-migration-api"
API_HOST="127.0.0.1"
API_PORT="8000"
MIN_PY_MINOR=9

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

on_error() {
    local line=$1
    printf '\n%s[FAIL]%s installation aborted (line %s).\n' \
        "${RED}" "${RESET}" "${line}" >&2
    printf '       Nothing else was changed. Fix the cause and re-run.\n' >&2
}
trap 'on_error $LINENO' ERR

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --prefix PATH       install directory        (default: ${PREFIX})
  --user NAME         service account          (default: ${SERVICE_USER})
  --host ADDR         API bind address         (default: ${API_HOST})
  --port PORT         API port                 (default: ${API_PORT})
  --service-name NAME systemd unit name        (default: ${SERVICE_NAME})
  --no-service        do not install the systemd unit
  --no-tokens         do not initialise the token store now
  --no-tests          skip the offline test suite
  --check             verify prerequisites only, change nothing
  -y, --yes           do not ask for confirmation
  -h, --help          this help
EOF
}

# ----------------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix)       PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
        --user)         SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
        --host)         API_HOST="${2:?--host needs an address}"; shift 2 ;;
        --port)         API_PORT="${2:?--port needs a number}"; shift 2 ;;
        --service-name) SERVICE_NAME="${2:?--service-name needs a name}"; shift 2 ;;
        --no-service)   INSTALL_SERVICE=0; shift ;;
        --no-tokens)    INIT_TOKENS=0; shift ;;
        --no-tests)     RUN_TESTS=0; shift ;;
        --check)        CHECK_ONLY=1; shift ;;
        -y|--yes)       ASSUME_YES=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option '$1' (try --help)" ;;
    esac
done

VENV="${PREFIX}/.venv"
PYBIN="${VENV}/bin/python"
JOB_DIR="${PREFIX}/jobs"
LOG_DIR="${PREFIX}/logs"
ETC_DIR="${PREFIX}/etc"
TOKEN_STORE="${ETC_DIR}/netapp_tokens.enc"
CREDS_FILE="${ETC_DIR}/creds.json"
UNLOCK_SOCKET="${ETC_DIR}/unlock.sock"

# ----------------------------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------------------------
step "Checking prerequisites"

[[ -f "${SOURCE_DIR}/netapp_cascade_migration.py" ]] \
    || die "run this script from the repository root (netapp_cascade_migration.py not found)"
[[ -d "${SOURCE_DIR}/wheels" ]] \
    || die "the wheels/ directory is missing — this installer is offline-only"

WHEEL_COUNT=$(find "${SOURCE_DIR}/wheels" -name '*.whl' | wc -l)
(( WHEEL_COUNT > 0 )) || die "wheels/ contains no .whl file"
ok "${WHEEL_COUNT} wheels available for the offline install"

# Python interpreter: prefer the newest usable one on the machine.
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    command -v "${candidate}" >/dev/null 2>&1 || continue
    minor=$("${candidate}" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)
    major=$("${candidate}" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)
    if (( major == 3 && minor >= MIN_PY_MINOR )); then
        PYTHON="${candidate}"
        break
    fi
done
[[ -n "${PYTHON}" ]] || die "no Python 3.${MIN_PY_MINOR}+ found — install python3 first (this installer cannot download it)"
PY_VERSION=$("${PYTHON}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
ok "Python ${PY_VERSION} (${PYTHON})"

# The venv module is a separate package on Debian/Ubuntu.
if ! "${PYTHON}" -c 'import venv' >/dev/null 2>&1; then
    die "the 'venv' module is missing — install it first:
       Debian/Ubuntu : apt-get install python3-venv
       RHEL/Rocky    : dnf install python3-libs"
fi
ok "venv module available"

# Matching compiled wheels must exist for this interpreter.
PY_TAG="cp$("${PYTHON}" -c 'import sys; print("%d%d" % sys.version_info[:2])')"
if ! find "${SOURCE_DIR}/wheels" -name "pydantic_core-*${PY_TAG}*" | grep -q .; then
    warn "no compiled wheel tagged ${PY_TAG} in wheels/"
    info "Python ${PY_VERSION} is probably not covered by this bundle."
    info "Regenerate it from a machine with repository access:"
    info "  pip download -r requirements.txt -d wheels/ --only-binary :all: \\"
    info "      --python-version ${PY_VERSION%.*} --platform manylinux2014_x86_64"
    die "aborting rather than producing a half-installed system"
fi
ok "compiled wheels present for ${PY_TAG}"

command -v systemctl >/dev/null 2>&1 || {
    if (( INSTALL_SERVICE )); then
        warn "systemd not found — the service will not be installed"
        INSTALL_SERVICE=0
    fi
}

if (( CHECK_ONLY )); then
    step "Check mode: prerequisites satisfied, nothing was changed"
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
# 2. Confirmation
# ----------------------------------------------------------------------------
step "Installation plan"
info "Source        : ${SOURCE_DIR}"
info "Install dir   : ${PREFIX}"
info "Python        : ${PYTHON} (${PY_VERSION})"
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
# 3. Service account
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
# 4. Directories and code
# ----------------------------------------------------------------------------
step "Installing the code into ${PREFIX}"
mkdir -p "${PREFIX}" "${JOB_DIR}" "${LOG_DIR}" "${ETC_DIR}"

if [[ "${SOURCE_DIR}" != "${PREFIX}" ]]; then
    for item in netapp_migration netapp_cascade_migration.py requirements.txt \
                requirements-dev.txt wheels docs tests pytest.ini \
                README.md README.fr.md; do
        [[ -e "${SOURCE_DIR}/${item}" ]] || continue
        rm -rf "${PREFIX:?}/${item}"
        cp -r "${SOURCE_DIR}/${item}" "${PREFIX}/"
    done
    ok "code copied"
else
    ok "installing in place"
fi

chmod 700 "${ETC_DIR}"
chmod 750 "${JOB_DIR}" "${LOG_DIR}"
ok "directories ready (etc 700, jobs/logs 750)"

# ----------------------------------------------------------------------------
# 5. Virtual environment, offline
# ----------------------------------------------------------------------------
step "Creating the virtual environment (offline)"
if [[ -x "${PYBIN}" ]]; then
    ok "virtual environment already present — reusing it"
else
    "${PYTHON}" -m venv "${VENV}"
    ok "virtual environment created: ${VENV}"
fi

PIP_ARGS=(--no-index --find-links "${PREFIX}/wheels" --disable-pip-version-check)

# pip itself first: an old pip rejects recent manylinux tags.
"${PYBIN}" -m pip install --quiet "${PIP_ARGS[@]}" --upgrade pip setuptools wheel \
    || die "could not upgrade pip from wheels/ — the bundle may not cover Python ${PY_VERSION}"
ok "pip $("${PYBIN}" -m pip --version | awk '{print $2}') ready"

"${PYBIN}" -m pip install --quiet "${PIP_ARGS[@]}" -r "${PREFIX}/requirements.txt" \
    || die "dependency installation failed — see the message above"
ok "runtime dependencies installed (no network used)"

if (( RUN_TESTS )) && [[ -f "${PREFIX}/requirements-dev.txt" ]]; then
    if "${PYBIN}" -m pip install --quiet "${PIP_ARGS[@]}" \
            -r "${PREFIX}/requirements-dev.txt" 2>/dev/null; then
        ok "test dependencies installed"
    else
        warn "test dependencies unavailable in wheels/ — tests will be skipped"
        RUN_TESTS=0
    fi
fi

# ----------------------------------------------------------------------------
# 6. Verification
# ----------------------------------------------------------------------------
step "Verifying the installation"
# From ${PREFIX}: the package is installed as a directory, not on sys.path,
# so the check must not depend on the directory the installer was called from.
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
# 7. Credentials template
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
# 8. systemd unit
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
# notify: the unit is reported active only once the port is really bound,
# never while the process is still starting up.
Type=notify
NotifyAccess=main
TimeoutStartSec=60
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${PREFIX}
Environment=NETAPP_MIGRATION_CONFIG=${CREDS_FILE}
Environment=NETAPP_MIGRATION_JOB_DIR=${JOB_DIR}
Environment=NETAPP_TOKEN_STORE=${TOKEN_STORE}
Environment=NETAPP_UNLOCK_SOCKET=${UNLOCK_SOCKET}

# A service has no terminal: an administrator connected over SSH could never
# answer a prompt here. So it starts LOCKED — the port is bound, every
# endpoint answers 503 — and a super admin supplies the global token from
# their own session with '--action api-unlock'. The token still reaches the
# process only in memory, and a restart comes back locked.
ExecStart=${PYBIN} -m netapp_migration.interfaces.api.serve \\
    --host ${API_HOST} --port ${API_PORT} \\
    --start-locked --unlock-socket ${UNLOCK_SOCKET}
StandardInput=null

# No automatic restart: a restart must be a deliberate act, followed by a
# deliberate unlock.
Restart=no

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${JOB_DIR} ${LOG_DIR} ${ETC_DIR}
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
# 9. Ownership
# ----------------------------------------------------------------------------
if (( INSTALL_SERVICE )) && id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    step "Permissions"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${JOB_DIR}" "${LOG_DIR}" "${ETC_DIR}"
    ok "jobs, logs and etc owned by ${SERVICE_USER}"
fi

# ----------------------------------------------------------------------------
# 10. Token store
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

  4. Start the API. It comes up LOCKED — the port is bound but every
     endpoint answers 503 — because a service has no terminal to prompt on.
     A super admin then unlocks it from their own session:
EOF
if (( INSTALL_SERVICE )); then
cat <<EOF
       systemctl start ${SERVICE_NAME}
       ${CLI} --action api-unlock --unlock-socket ${UNLOCK_SOCKET}

     or run it in the foreground instead, where it prompts directly
     (use tmux/screen so it survives the SSH session):
       ${PYBIN} -m netapp_migration.interfaces.api.serve \\
           --host ${API_HOST} --port ${API_PORT}
EOF
else
cat <<EOF
       ${PYBIN} -m netapp_migration.interfaces.api.serve \\
           --host ${API_HOST} --port ${API_PORT}
     It prompts for the global token, then serves. Use tmux/screen so it
     survives the SSH session.
EOF
fi
cat <<EOF

  5. Check it answers:
       curl -s http://${API_HOST}:${API_PORT}/api/v1/health
       -> {"auth":{"unlocked":true}} once step 4 is complete
       Swagger UI: http://${API_HOST}:${API_PORT}/docs
EOF
if [[ "${API_HOST}" == "127.0.0.1" ]]; then
cat <<EOF

${BOLD}Note${RESET} — the API is bound to 127.0.0.1: /docs is reachable from this
server only. To open it from a workstation, either tunnel:
       ssh -L ${API_PORT}:127.0.0.1:${API_PORT} <user>@$(hostname -f 2>/dev/null || hostname)
or reinstall with --host 0.0.0.0 once the port is firewalled properly.
EOF
fi
cat <<EOF

Full documentation: ${PREFIX}/README.md and ${PREFIX}/docs/architecture.md
EOF

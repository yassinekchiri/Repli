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

# Hardened hosts often run with umask 077, which would create the install
# directory unreadable to anyone but root — the service account could then not
# even spawn the interpreter (systemd reports "status=203/EXEC, Permission
# denied"). Everything secret gets an explicit umask 077 / chmod later on; the
# rest must be traversable.
umask 022

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
PYTHON=""          # --python: force a base interpreter

INDEX_URL=""
EXTRA_INDEX_URL=""
TRUSTED_HOSTS=()
PIP_TIMEOUT=120
PIP_CERT=""        # --cert: CA bundle for a private/corporate index
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
  --cert PATH           CA bundle for an index behind a private CA
                        (e.g. /etc/pki/tls/certs/ca-bundle.crt)
  --python PATH         base interpreter for the venv (default: newest
                        system Python 3.9+ the service account can reach)
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
        --cert)            PIP_CERT="${2:?--cert needs a path}"; shift 2 ;;
        --python)          PYTHON="${2:?--python needs a path}"; shift 2 ;;
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
UNLOCK_SOCKET="${ETC_DIR}/unlock.sock"

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

# A virtualenv is a set of symlinks to the interpreter it was built from, so
# the SERVICE ACCOUNT has to be able to reach that interpreter — not just
# root. An interpreter under /root (mode 0550) produces a venv that only root
# can run, and systemd reports the useless "status=203/EXEC".
reachable_by_others() {
    local target mode dir
    target=$(readlink -f "$1" 2>/dev/null) || return 1
    mode=$(stat -Lc '%a' "${target}" 2>/dev/null) || return 1
    (( (8#${mode} & 5) == 5 )) || return 1      # the binary: o+r and o+x
    dir=$(dirname "${target}")
    while :; do
        mode=$(stat -Lc '%a' "${dir}" 2>/dev/null) || return 1
        (( 8#${mode} & 1 )) || return 1         # every directory: o+x
        [[ "${dir}" == "/" ]] && break
        dir=$(dirname "${dir}")
    done
    return 0
}

python_version_ok() {
    local major minor
    major=$("$1" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)
    minor=$("$1" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)
    (( major == 3 && minor >= MIN_PY_MINOR ))
}

if [[ -n "${PYTHON}" ]]; then
    # --python was given: honour it, but do not stay silent about a choice
    # that cannot work for the service.
    command -v "${PYTHON}" >/dev/null 2>&1 \
        || die "no such interpreter: ${PYTHON}"
    python_version_ok "${PYTHON}" \
        || die "${PYTHON} is not a Python 3.${MIN_PY_MINOR}+"
    if (( INSTALL_SERVICE )) && ! reachable_by_others "${PYTHON}"; then
        warn "$(readlink -f "${PYTHON}") is not reachable by a non-root account"
        info "the service would fail with status=203/EXEC"
    fi
else
    # Absolute system locations first: root's PATH may put a private
    # interpreter (an agent's bundled Python under /root, for instance)
    # ahead of the system one.
    PRIVATE_FALLBACK=""
    for candidate in /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3.10 \
                     /usr/bin/python3.9 /usr/bin/python3 \
                     /usr/local/bin/python3 \
                     python3.12 python3.11 python3.10 python3.9 python3; do
        command -v "${candidate}" >/dev/null 2>&1 || continue
        python_version_ok "${candidate}" || continue
        if reachable_by_others "${candidate}"; then
            PYTHON="${candidate}"
            break
        fi
        [[ -n "${PRIVATE_FALLBACK}" ]] || PRIVATE_FALLBACK="${candidate}"
    done

    if [[ -z "${PYTHON}" && -n "${PRIVATE_FALLBACK}" ]]; then
        warn "the only Python 3.${MIN_PY_MINOR}+ found is $(readlink -f "${PRIVATE_FALLBACK}")"
        info "it sits in a directory no other account can traverse, so a"
        info "service built on it cannot start (status=203/EXEC)."
        if (( INSTALL_SERVICE )); then
            die "install a system Python (dnf install python3 / apt-get install python3),
       or pass --python /usr/bin/python3.X, or --no-service to run it as root"
        fi
        warn "continuing: no service is being installed"
        PYTHON="${PRIVATE_FALLBACK}"
    fi
fi
[[ -n "${PYTHON}" ]] || die "no Python 3.${MIN_PY_MINOR}+ found — install python3 first (this installer cannot download it)"
PY_VERSION=$("${PYTHON}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')
ok "Python ${PY_VERSION} (${PYTHON})"

# The venv module is a separate package on Debian/Ubuntu.
"${PYTHON}" -c 'import venv' >/dev/null 2>&1 || die "the 'venv' module is missing — install it first:
       Debian/Ubuntu : apt-get install python3-venv
       RHEL/Rocky    : dnf install python3-libs"
ok "venv module available"

# `systemctl` being on PATH is not enough: inside a container or a chroot it
# is present but systemd is not PID 1, and every call fails. /run/systemd/system
# exists only when systemd really is running the machine.
if (( INSTALL_SERVICE )) \
        && { ! command -v systemctl >/dev/null 2>&1 || [[ ! -d /run/systemd/system ]]; }; then
    warn "systemd is not running this machine — the service will not be installed"
    info "the API can still be started in the foreground (see the summary)"
    INSTALL_SERVICE=0
fi

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

# Noted BEFORE the code is replaced. A running service keeps executing the
# code it loaded at start-up, so an upgrade that only refreshes the files on
# disk changes nothing the user can see — the API goes on answering with the
# previous version until it is restarted. Said loudly in the summary rather
# than restarted here: a restart drops the unlocked state and interrupts
# whatever action is in flight, so it stays a deliberate act.
SERVICE_WAS_RUNNING=0
if (( INSTALL_SERVICE )) \
        && systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    SERVICE_WAS_RUNNING=1
fi

mkdir -p "${PREFIX}" "${JOB_DIR}" "${LOG_DIR}" "${ETC_DIR}"

for item in netapp_migration netapp_cascade_migration.py requirements.txt \
            requirements-dev.txt docs tests pytest.ini \
            README.md README.fr.md; do
    [[ -e "${WORKDIR}/${item}" ]] || continue
    rm -rf "${PREFIX:?}/${item}"
    cp -r "${WORKDIR}/${item}" "${PREFIX}/"
done
ok "code installed"

# Explicit, not umask-dependent: the service account has to traverse every
# component of ${PREFIX} to reach the interpreter and the code. A re-run over
# a previously too-strict install repairs it.
chmod 755 "${PREFIX}"
chmod 700 "${ETC_DIR}"
chmod 750 "${JOB_DIR}" "${LOG_DIR}"
ok "directories ready (prefix 755, etc 700, jobs/logs 750)"

# ----------------------------------------------------------------------------
# 6. Virtual environment and dependencies (online)
# ----------------------------------------------------------------------------
step "Creating the virtual environment"
# A venv is only as usable as the interpreter it points at. One built earlier
# from a private interpreter must be rebuilt, or re-running this installer
# would "succeed" and leave the same unstartable service behind. Not gated on
# --no-service: a venv only root can run is never what anyone wants, and the
# service may well be added later. Only rebuild when the replacement is
# actually better, otherwise an explicit --python would loop forever.
if [[ -x "${PYBIN}" ]] && ! reachable_by_others "${PYBIN}" \
        && reachable_by_others "${PYTHON}"; then
    warn "the existing venv points at $(readlink -f "${PYBIN}" 2>/dev/null)"
    info "no other account can reach it — rebuilding on ${PYTHON}"
    rm -rf "${VENV}"
fi

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
[[ -n "${PIP_CERT}" ]]        && PIP_ARGS+=(--cert "${PIP_CERT}")
for host in "${TRUSTED_HOSTS[@]+"${TRUSTED_HOSTS[@]}"}"; do
    PIP_ARGS+=(--trusted-host "${host}")
done

# A venv's pip trusts its own bundled CA store (pip/_vendor/certifi), NOT the
# system trust store — so an internal mirror fronted by a corporate CA fails
# with SSLCertVerificationError even though the system pip works fine. Find
# the system bundle so we can point pip at it.
system_ca_bundle() {
    local candidate
    for candidate in /etc/pki/tls/certs/ca-bundle.crt \
                     /etc/ssl/certs/ca-certificates.crt \
                     /etc/ssl/cert.pem /etc/ssl/ca-bundle.pem; do
        [[ -r "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
    done
    return 1
}

# Runs pip, and retries once against the system CA bundle if — and only if —
# the failure was a certificate one. Silent when nothing goes wrong.
CERT_RETRIED=0
pip_run() {
    local log status bundle
    log=$(mktemp)
    if "${PYBIN}" -m pip install "${PIP_ARGS[@]}" "$@" >"${log}" 2>&1; then
        rm -f "${log}"
        return 0
    fi
    status=1
    if (( ! CERT_RETRIED )) && [[ -z "${PIP_CERT}" ]] \
            && grep -qE 'CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError|SSLError' "${log}" \
            && bundle=$(system_ca_bundle); then
        CERT_RETRIED=1
        warn "TLS verification failed against the package index"
        info "a venv's pip trusts its own bundled CA store, not the system one;"
        info "retrying with ${bundle}"
        PIP_ARGS+=(--cert "${bundle}")
        PIP_CERT="${bundle}"
        if "${PYBIN}" -m pip install "${PIP_ARGS[@]}" "$@" >"${log}" 2>&1; then
            ok "the system CA bundle works — recording it for this install"
            rm -f "${log}"
            return 0
        fi
    fi
    cat "${log}" >&2
    rm -f "${log}"
    return "${status}"
}

step "Installing the dependencies (network)"
info "an internal mirror behind a slow link needs the retries below;"
info "timeout ${PIP_TIMEOUT}s, ${PIP_RETRIES} retries per package"

# pip itself first: an old pip rejects recent manylinux tags. Not fatal —
# some mirrors do not carry pip.
if pip_run --quiet --upgrade pip setuptools wheel; then
    ok "pip $("${PYBIN}" -m pip --version | awk '{print $2}') ready"
else
    warn "could not upgrade pip — continuing with $("${PYBIN}" -m pip --version | awk '{print $2}')"
fi

pip_run -r "${PREFIX}/requirements.txt" \
    || die "dependency installation failed.
       Behind a proxy or an internal mirror, point pip at it:
         --index-url https://<mirror>/api/pypi/pypi/simple --trusted-host <mirror>
       For a private CA:  --cert /etc/pki/tls/certs/ca-bundle.crt
       With no index at all, use install.sh with the bundled wheels/ instead."
ok "runtime dependencies installed"

if (( RUN_TESTS )) && [[ -f "${PREFIX}/requirements-dev.txt" ]]; then
    if pip_run --quiet -r "${PREFIX}/requirements-dev.txt"; then
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

# Everything above ran as root. The service will not: check that the service
# account can actually spawn the interpreter, or systemd will only say
# "status=203/EXEC, Permission denied" at the first start.
if (( INSTALL_SERVICE )) && id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    # The service account has a nologin shell, so plain `su -` is out:
    # runuser is the right tool, with `su -s /bin/sh` as the fallback where
    # util-linux's runuser is missing.
    if command -v runuser >/dev/null 2>&1; then
        service_can_exec() {
            runuser -u "${SERVICE_USER}" -- /bin/sh -c "$1" >/dev/null 2>&1
        }
    else
        service_can_exec() {
            su -s /bin/sh "${SERVICE_USER}" -c "$1" >/dev/null 2>&1
        }
    fi

    # Two distinct failures, two distinct messages: spawning the interpreter
    # is what systemd reports as 203/EXEC; reading the code is a later, much
    # clearer error. The code is a directory under ${PREFIX}, not an installed
    # package, so the import only resolves from there — exactly as the unit's
    # WorkingDirectory arranges.
    if service_can_exec "'${PYBIN}' -c 'pass'" \
            && service_can_exec "cd '${PREFIX}' && '${PYBIN}' -c 'import netapp_migration'"; then
        ok "${SERVICE_USER} can start the API"
    else
        if service_can_exec "'${PYBIN}' -c 'pass'"; then
            warn "${SERVICE_USER} can run the interpreter but cannot import the code"
            info "check the read permissions on ${PREFIX}/netapp_migration"
        else
            warn "${SERVICE_USER} CANNOT execute ${PYBIN}"
            info "systemd would fail with: status=203/EXEC, Permission denied."
        fi
        info "Diagnosing:"

        # 1. Path permissions, component by component.
        if command -v namei >/dev/null 2>&1; then
            namei -l "${PYBIN}" | sed 's/^/           /'
        else
            ls -ld "${PREFIX}" "${VENV}" "${VENV}/bin" "${PYBIN}" \
                | sed 's/^/           /'
        fi

        # 2. A noexec mount makes every binary there unrunnable.
        mount_point=$(df -P "${PREFIX}" | awk 'NR==2 {print $6}')
        if findmnt -no OPTIONS "${mount_point}" 2>/dev/null | grep -q noexec; then
            warn "${mount_point} is mounted noexec — nothing under it can be executed"
            info "install elsewhere (--prefix) or remount without noexec"
        fi

        # 3. SELinux mislabels files copied into /opt on RHEL-family hosts.
        if command -v getenforce >/dev/null 2>&1 \
                && [[ "$(getenforce)" == "Enforcing" ]]; then
            warn "SELinux is enforcing"
            if command -v restorecon >/dev/null 2>&1; then
                restorecon -R "${PREFIX}" 2>/dev/null || true
                if service_can_exec "cd '${PREFIX}' && '${PYBIN}' -c 'import netapp_migration'"; then
                    ok "fixed by restorecon -R ${PREFIX}"
                else
                    info "check the denial: ausearch -m avc -ts recent"
                fi
            else
                info "run: restorecon -R ${PREFIX}  (policycoreutils)"
            fi
        fi

        service_can_exec "cd '${PREFIX}' && '${PYBIN}' -c 'import netapp_migration'" \
            || die "the service account cannot run the API — fix the cause above and re-run this installer"
    fi
fi

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
    # Never fatal: the unit file is written and correct either way, and an
    # install that succeeded must not be reported as aborted.
    if systemctl daemon-reload 2>/dev/null; then
        ok "unit installed: ${UNIT}"
    else
        warn "unit written but 'systemctl daemon-reload' failed"
        info "run it yourself before starting the service"
    fi
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
EOF

if (( SERVICE_WAS_RUNNING )); then
cat <<EOF

${BOLD}${YELLOW}The service was already running — RESTART IT.${RESET}

  The new code is on disk, but ${SERVICE_NAME} is still executing the
  version it loaded when it started. Nothing you just installed takes
  effect until:

       systemctl restart ${SERVICE_NAME}
       ${PYBIN} ${PREFIX}/netapp_cascade_migration.py \\
           --action api-unlock --unlock-socket ${UNLOCK_SOCKET}

  It is not restarted automatically: that would drop the unlocked state
  and cut whatever action is in flight. The restart comes back LOCKED,
  hence the unlock on the second line.
EOF
fi

cat <<EOF

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

exit 0

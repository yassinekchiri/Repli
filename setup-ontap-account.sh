#!/usr/bin/env bash
#
# NetApp Cascade Migration — ONTAP-side provisioning.
#
# Creates, on ONE cluster, the service account the tool authenticates with,
# and the least-privilege roles it needs. Run it once per cluster of the
# topology (source, pivot, PROD, DR).
#
# It assumes you already have administrative SSH access to the cluster (key
# or agent): it only ever runs `ssh <cluster> <ONTAP command>`. It never asks
# for YOUR password — only for the one to give the new account, hidden, twice.
#
#   ./setup-ontap-account.sh                     # asks for cluster + password
#   ./setup-ontap-account.sh --cluster clu01     # asks for the password only
#   ./setup-ontap-account.sh --check             # report what exists, change nothing
#   ./setup-ontap-account.sh --dry-run           # print the commands, run none
#
# Safe to re-run: existing roles and logins are detected and kept. See --help.

set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------
CLUSTER=""
SSH_USER=""                 # empty: ssh decides (config / current user)
ACCOUNT="mutrepli"          # must match "username" in creds.json
REST_ROLE=""                # default: <account>_rest
CLI_ROLE=""                 # default: <account>_cli
VSERVER=""                  # empty: the cluster's admin vserver
WITH_SSH_LOGIN=1            # also create the CLI/ssh login (fallback transport)
CHECK_ONLY=0
DRY_RUN=0
ASSUME_YES=0

# The endpoints the REST transport actually calls, and nothing else.
# The three readonly grants at the end are not optional: ONTAP resolves
# objects referenced inside a request with the CALLER's permissions, so an
# endpoint the role cannot read is reported as "not found" even when the
# object exists.
REST_GRANTS=(
    "/api/storage/volumes|all"
    "/api/storage/qtrees|all"
    "/api/storage/aggregates|readonly"
    "/api/storage/quota/rules|readonly"
    "/api/snapmirror/relationships|all"
    "/api/protocols/cifs/shares|all"
    "/api/protocols/nfs/export-policies|all"
    "/api/protocols/file-security/permissions|all"
    "/api/svm/svms|readonly"
    "/api/cluster/jobs|readonly"
    "/api/cluster/schedules|readonly"
    "/api/snapmirror/policies|readonly"
    "/api/svm/peers|readonly"
)

# Command directories for the SSH fallback transport.
CLI_GRANTS=(
    "volume|all"
    "snapmirror|all"
    "storage aggregate|readonly"
    "volume quota policy|readonly"
    "vserver cifs share|all"
    "vserver export-policy|all"
    "vserver security file-directory|all"
)

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
ok()   { printf '    %s[ OK ]%s %s\n'   "${GREEN}"  "${RESET}" "$*"; }
warn() { printf '    %s[WARN]%s %s\n'   "${YELLOW}" "${RESET}" "$*"; }
skip() { printf '    %s[SKIP]%s %s\n'   "${BLUE}"   "${RESET}" "$*"; }
info() { printf '           %s\n' "$*"; }
die()  { printf '\n%s[FAIL]%s %s\n' "${RED}" "${RESET}" "$*" >&2; exit 1; }

PASSWORD=""
cleanup() {
    # The password lives in a shell variable and nowhere else; clear it as
    # soon as the script ends, however it ends.
    PASSWORD=""
    return 0
}
trap cleanup EXIT

on_error() {
    printf '\n%s[FAIL]%s aborted (line %s).\n' "${RED}" "${RESET}" "$1" >&2
    printf '       Nothing further was changed. The script is safe to re-run.\n' >&2
}
trap 'on_error $LINENO' ERR

usage() {
    # The header block: from line 2 up to the first blank line.
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --cluster HOST      cluster management address (default: asked)
  --ssh-user USER     SSH as this user (default: your ssh configuration)
  --account NAME      account to create        (default: ${ACCOUNT})
  --rest-role NAME    REST role name           (default: <account>_rest)
  --cli-role NAME     CLI role name            (default: <account>_cli)
  --vserver NAME      scope the roles to this vserver (default: admin vserver)
  --no-ssh-login      REST access only: no CLI role, no ssh login
  --check             report what exists, change nothing
  --dry-run           print every command, run none (no password is read)
  -y, --yes           do not ask for confirmation
  -h, --help          this help

The account password is read from the terminal, twice, never echoed. It is
never passed on a command line, never written to a file, and never appears
in this script's output.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cluster)      CLUSTER="${2:?--cluster needs a host}"; shift 2 ;;
        --ssh-user)     SSH_USER="${2:?--ssh-user needs a name}"; shift 2 ;;
        --account)      ACCOUNT="${2:?--account needs a name}"; shift 2 ;;
        --rest-role)    REST_ROLE="${2:?--rest-role needs a name}"; shift 2 ;;
        --cli-role)     CLI_ROLE="${2:?--cli-role needs a name}"; shift 2 ;;
        --vserver)      VSERVER="${2:?--vserver needs a name}"; shift 2 ;;
        --no-ssh-login) WITH_SSH_LOGIN=0; shift ;;
        --check)        CHECK_ONLY=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        -y|--yes)       ASSUME_YES=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option '$1' (try --help)" ;;
    esac
done

REST_ROLE="${REST_ROLE:-${ACCOUNT}_rest}"
CLI_ROLE="${CLI_ROLE:-${ACCOUNT}_cli}"

# ----------------------------------------------------------------------------
# Talking to the cluster
# ----------------------------------------------------------------------------
ssh_target() {
    [[ -n "${SSH_USER}" ]] && printf '%s@%s' "${SSH_USER}" "${CLUSTER}" \
        || printf '%s' "${CLUSTER}"
}

# Read-only, non-interactive. Never prompts: if the key is not accepted the
# command fails rather than hanging on a password prompt.
cluster_query() {
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=15 "$(ssh_target)" "$1" 2>&1
}

# A change. Echoed under --dry-run, executed otherwise.
cluster_run() {
    local command="$1"
    if (( DRY_RUN )); then
        printf '           %s%s%s\n' "${BOLD}" "${command}" "${RESET}"
        return 0
    fi
    local output
    if ! output=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
                      -o ConnectTimeout=15 "$(ssh_target)" "${command}" 2>&1); then
        printf '%s\n' "${output}" | sed 's/^/           /' >&2
        return 1
    fi
    # ONTAP reports failures in its output, not always in the exit status.
    if grep -qiE '^Error:|command failed|is not a recognized' <<<"${output}"; then
        printf '%s\n' "${output}" | sed 's/^/           /' >&2
        return 1
    fi
    return 0
}

# ONTAP's "not there" answers, in the several shapes it uses.
is_absent() {
    grep -qiE 'no entries matching|does not exist|doesn'"'"'t exist|entry does not exist' <<<"$1"
}

object_exists() {
    local output="$1"
    if is_absent "${output}"; then
        return 1
    fi
    if grep -qiE '^Error:' <<<"${output}"; then
        # An error that is not "absent" means we cannot tell — say so rather
        # than assume the object is missing and try to create it.
        printf '%s\n' "${output}" | sed 's/^/           /' >&2
        die "could not query the cluster (see above)"
    fi
    return 0
}

# The one interactive step: ONTAP asks for the new password on a terminal and
# will not take it on the command line. -tt allocates one; the two lines are
# the answer and its confirmation. The transcript is scrubbed before anything
# is printed, so the password cannot leak into a terminal capture.
cluster_run_with_password() {
    local command="$1" transcript status=0
    if (( DRY_RUN )); then
        printf '           %s%s%s\n' "${BOLD}" "${command}" "${RESET}"
        info "(would then supply the password twice, on the terminal)"
        return 0
    fi
    transcript=$(printf '%s\n%s\n%s\n' "${command}" "${PASSWORD}" "${PASSWORD}" \
        | ssh -tt -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
              "$(ssh_target)" 2>&1) || status=$?
    # Defensive: the prompt disables echo, but never take that on trust.
    transcript="${transcript//${PASSWORD}/********}"
    if (( status != 0 )) \
            || grep -qiE '^Error:|command failed|password.*(too short|not.*complex|invalid)' \
                    <<<"${transcript}"; then
        printf '%s\n' "${transcript}" | sed 's/^/           /' >&2
        return 1
    fi
    return 0
}

# ----------------------------------------------------------------------------
# 1. What are we doing, and where
# ----------------------------------------------------------------------------
step "Target cluster"

if [[ -z "${CLUSTER}" ]]; then
    read -r -p "    Cluster management address: " CLUSTER
    [[ -n "${CLUSTER}" ]] || die "no cluster given"
fi

# --dry-run only prints commands, so it works anywhere — including a
# workstation with no ssh client and no route to the cluster.
if (( ! DRY_RUN )); then
    command -v ssh >/dev/null 2>&1 || die "the ssh client is not installed"
fi

if (( DRY_RUN )); then
    ok "dry run: no connection is made, no password is read"
else
    VERSION=$(cluster_query "version") \
        || die "cannot reach ${CLUSTER} over SSH as $(ssh_target).
       This script needs YOUR administrative access to already work:
         ssh $(ssh_target) version"
    ok "connected: $(head -1 <<<"${VERSION}" | cut -c1-70)"

    # Creating logins and roles is an admin privilege; find out now rather
    # than half-way through.
    PRIV=$(cluster_query "security login role show -role admin -fields role" || true)
    if grep -qiE 'not authorized|insufficient privilege' <<<"${PRIV}"; then
        die "the account you are connecting with cannot administer logins on ${CLUSTER}"
    fi
    ok "administrative access confirmed"
fi

step "Plan"
info "Cluster      : ${CLUSTER}"
info "SSH as       : $(ssh_target)"
info "Account      : ${ACCOUNT}"
info "REST role    : ${REST_ROLE}   (${#REST_GRANTS[@]} endpoint grants)"
if (( WITH_SSH_LOGIN )); then
    info "CLI role     : ${CLI_ROLE}   (${#CLI_GRANTS[@]} command grants)"
else
    info "CLI role     : not created (--no-ssh-login)"
fi
[[ -n "${VSERVER}" ]] && info "Vserver      : ${VSERVER}"

if (( CHECK_ONLY )); then
    step "Check mode: reporting only, nothing will be changed"
fi

if (( ! CHECK_ONLY && ! DRY_RUN && ! ASSUME_YES )); then
    read -r -p "    Proceed? [y/N] " answer
    [[ "${answer,,}" == "y" ]] || { echo "    Aborted."; exit 0; }
fi

# ----------------------------------------------------------------------------
# 2. The password for the new account
# ----------------------------------------------------------------------------
VSERVER_ARG=""
[[ -n "${VSERVER}" ]] && VSERVER_ARG=" -vserver ${VSERVER}"

NEED_PASSWORD=0
if (( ! CHECK_ONLY && ! DRY_RUN )); then
    NEED_PASSWORD=1
fi

if (( NEED_PASSWORD )); then
    step "Password for '${ACCOUNT}'"
    info "It is read here, hidden, and handed straight to the cluster:"
    info "never on a command line, never in a file, never in this output."
    read -r -s -p "    Password for '${ACCOUNT}': " PASSWORD; echo
    read -r -s -p "    Confirm: " CONFIRM; echo
    [[ -n "${PASSWORD}" ]] || die "empty password"
    [[ "${PASSWORD}" == "${CONFIRM}" ]] || die "the two entries differ"
    CONFIRM=""
    # ONTAP's own minimum; catching it here beats a failed create half-way.
    (( ${#PASSWORD} >= 8 )) || die "ONTAP requires at least 8 characters"
    ok "password captured (${#PASSWORD} characters)"
fi

# ----------------------------------------------------------------------------
# 3. REST role
# ----------------------------------------------------------------------------
step "REST role '${REST_ROLE}'"

REST_ROLE_EXISTS=0
if (( ! DRY_RUN )); then
    if object_exists "$(cluster_query \
            "security login rest-role show -role ${REST_ROLE}${VSERVER_ARG}")"; then
        REST_ROLE_EXISTS=1
    fi
fi

if (( REST_ROLE_EXISTS )); then
    ok "already exists — its grants are checked below, not overwritten"
elif (( CHECK_ONLY )); then
    warn "missing: ${REST_ROLE}"
else
    info "creating it, one endpoint at a time"
fi

for grant in "${REST_GRANTS[@]}"; do
    api="${grant%%|*}"
    access="${grant##*|}"
    command="security login rest-role create -role ${REST_ROLE}${VSERVER_ARG} -api ${api} -access ${access}"

    if (( CHECK_ONLY )); then
        existing=$(cluster_query \
            "security login rest-role show -role ${REST_ROLE}${VSERVER_ARG} -api ${api} -fields access")
        if is_absent "${existing}"; then
            warn "missing grant: ${api} (${access})"
        elif grep -qw "${access}" <<<"${existing}"; then
            ok "${api} -> ${access}"
        else
            warn "${api}: expected ${access}, found something else"
            printf '%s\n' "${existing}" | sed 's/^/           /'
        fi
        continue
    fi

    if (( REST_ROLE_EXISTS )) && ! is_absent "$(cluster_query \
            "security login rest-role show -role ${REST_ROLE}${VSERVER_ARG} -api ${api} -fields access")"; then
        skip "${api} (already granted)"
        continue
    fi

    if cluster_run "${command}"; then
        ok "${api} -> ${access}"
    else
        die "could not grant ${api} on ${REST_ROLE}"
    fi
done

# ----------------------------------------------------------------------------
# 4. CLI role (SSH fallback transport)
# ----------------------------------------------------------------------------
if (( WITH_SSH_LOGIN )); then
    step "CLI role '${CLI_ROLE}'"

    CLI_ROLE_EXISTS=0
    if (( ! DRY_RUN )); then
        if object_exists "$(cluster_query \
                "security login role show -role ${CLI_ROLE}${VSERVER_ARG}")"; then
            CLI_ROLE_EXISTS=1
        fi
    fi
    (( CLI_ROLE_EXISTS )) && ok "already exists" || true

    for grant in "${CLI_GRANTS[@]}"; do
        cmddir="${grant%%|*}"
        access="${grant##*|}"
        command="security login role create -role ${CLI_ROLE}${VSERVER_ARG} -cmddirname \"${cmddir}\" -access ${access}"

        if (( CHECK_ONLY )); then
            existing=$(cluster_query \
                "security login role show -role ${CLI_ROLE}${VSERVER_ARG} -cmddirname \"${cmddir}\" -fields access")
            if is_absent "${existing}"; then
                warn "missing grant: ${cmddir} (${access})"
            else
                ok "${cmddir} -> ${access}"
            fi
            continue
        fi

        if (( CLI_ROLE_EXISTS )) && ! is_absent "$(cluster_query \
                "security login role show -role ${CLI_ROLE}${VSERVER_ARG} -cmddirname \"${cmddir}\" -fields access")"; then
            skip "${cmddir} (already granted)"
            continue
        fi

        if cluster_run "${command}"; then
            ok "${cmddir} -> ${access}"
        else
            die "could not grant '${cmddir}' on ${CLI_ROLE}"
        fi
    done
fi

# ----------------------------------------------------------------------------
# 5. The logins
# ----------------------------------------------------------------------------
step "Account '${ACCOUNT}'"

# application -> role. 'http' is what the REST transport authenticates with;
# 'ontapi' covers tooling that still speaks ZAPI; 'ssh' is the CLI fallback.
LOGINS=("http|${REST_ROLE}" "ontapi|${REST_ROLE}")
(( WITH_SSH_LOGIN )) && LOGINS+=("ssh|${CLI_ROLE}")

FIRST_LOGIN=1
for entry in "${LOGINS[@]}"; do
    application="${entry%%|*}"
    role="${entry##*|}"
    existing=""
    (( ! DRY_RUN )) && existing=$(cluster_query \
        "security login show -user-or-group-name ${ACCOUNT} -application ${application}${VSERVER_ARG}")

    if (( CHECK_ONLY )); then
        if is_absent "${existing}"; then
            warn "no ${application} login for '${ACCOUNT}'"
        else
            ok "${application} login exists"
            printf '%s\n' "${existing}" | sed -n '3,6p' | sed 's/^/           /'
        fi
        continue
    fi

    if (( ! DRY_RUN )) && ! is_absent "${existing}"; then
        skip "${application} login already exists (password left untouched)"
        FIRST_LOGIN=0
        continue
    fi

    command="security login create -user-or-group-name ${ACCOUNT} -application ${application} -authentication-method password -role ${role}${VSERVER_ARG}"

    # ONTAP prompts for the password on the FIRST password-method login of an
    # account; later ones reuse it. Feeding the answers to a command that did
    # not ask would leave them interpreted as CLI input, so only the first
    # goes through the interactive path.
    if (( FIRST_LOGIN )); then
        if cluster_run_with_password "${command}"; then
            ok "${application} login created with role '${role}' (password set)"
            FIRST_LOGIN=0
        else
            die "could not create the ${application} login — see the cluster's answer above"
        fi
    else
        if cluster_run "${command}"; then
            ok "${application} login created with role '${role}'"
        else
            die "could not create the ${application} login"
        fi
    fi
done

# ----------------------------------------------------------------------------
# 6. Verification
# ----------------------------------------------------------------------------
if (( ! DRY_RUN )); then
    step "Verification"
    RESULT=$(cluster_query "security login show -user-or-group-name ${ACCOUNT}${VSERVER_ARG}")
    if is_absent "${RESULT}"; then
        die "'${ACCOUNT}' does not exist on ${CLUSTER} after all — see above"
    fi
    printf '%s\n' "${RESULT}" | sed 's/^/           /'
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
if (( CHECK_ONLY )); then
    cat <<EOF

${BOLD}Check complete.${RESET} Nothing was modified.
Re-run without --check to create what is reported missing.
EOF
    exit 0
fi

cat <<EOF

${BOLD}${GREEN}Done on ${CLUSTER}.${RESET}

  Account   : ${ACCOUNT}
  REST role : ${REST_ROLE}
  CLI role  : $( ((WITH_SSH_LOGIN)) && echo "${CLI_ROLE}" || echo "not created" )

${BOLD}Next${RESET}

  1. Repeat on every cluster of the topology (source, pivot, PROD, DR).

  2. Put the account in creds.json on the API server:
       "defaults": { "username": "${ACCOUNT}", "password": "...", "verify_ssl": false }
     A password containing " or \\ must be escaped: "pa\\"ss\\\\word".

  3. Check REST authentication from the API server itself:
       curl -sk -u ${ACCOUNT} \\
           "https://${CLUSTER}/api/storage/volumes?max_records=1"

  4. Then the tool's own pre-flight will tell you if a grant is missing:
       netapp_cascade_migration.py --action check-status --job-id <ID>
EOF

if (( WITH_SSH_LOGIN )); then
cat <<EOF

${BOLD}Note on the SSH transport${RESET} — the tool connects with BatchMode, so
password authentication is not enough for it. Add a key to use it:

  security login create -user-or-group-name ${ACCOUNT} -application ssh \\
      -authentication-method publickey -role ${CLI_ROLE}
  security login publickey create -username ${ACCOUNT} \\
      -publickey "ssh-ed25519 AAAA... migration@server"

The default REST transport needs none of this.
EOF
fi

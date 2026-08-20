# Using the API from Swagger UI — step by step

*Version française : [api-guide.fr.md](api-guide.fr.md)*

This walkthrough follows one migration from an empty API to a promoted
clone, entirely through the browser. Every screenshot below is a real
capture of the real API — none of them is a mockup, and they are regenerated
from the code by `tools/capture_swagger_guide.py`.

> **About the tokens and cluster names in the screenshots.** They come from a
> throwaway demo store created during the capture and destroyed immediately
> after. `SuperAdmin-Demo-Token`, `DEMO-TOKEN-finance-only`, `clu-prod-01`…
> are fictional and valid nowhere. The capture runs with the **dry-run
> transport**: no ONTAP cluster is ever contacted to produce this guide.

**Contents**

1. [Reaching Swagger UI](#1-reaching-swagger-ui)
2. [The API starts locked](#2-the-api-starts-locked)
3. [The landing page](#3-the-landing-page)
4. [Authenticating](#4-authenticating)
5. [Delegating tokens to clients](#5-delegating-tokens-to-clients)
6. [Checking feasibility before acting](#6-checking-feasibility-before-acting)
7. [Creating the cascade](#7-creating-the-cascade)
8. [Following a running migration](#8-following-a-running-migration)
9. [Per-qtree actions: test, clone, acl](#9-per-qtree-actions-test-clone-acl)
10. [What a scoped token can and cannot do](#10-what-a-scoped-token-can-and-cannot-do)
11. [Reading the answers](#11-reading-the-answers)

---

## 1. Reaching Swagger UI

Open `http://<api-server>:8000/docs`.

If nothing answers, the API is bound to `127.0.0.1` (the installed default),
which means *reachable from the API server only*. Either tunnel from your
workstation:

```bash
ssh -L 8000:127.0.0.1:8000 <user>@<api-server>
# then open http://127.0.0.1:8000/docs locally
```

or start the API with `--host 0.0.0.0` once the port is firewalled properly.

The Swagger assets are served by the API itself (`/static/`), so the page
works on a server with no Internet access.

---

## 2. The API starts locked

The token store is decrypted in memory only. After every start — including
`systemctl start` — the API is **locked**: the port is bound and Swagger UI
loads, but every endpoint answers `503`.

![Locked API answering 503](images/01-locked-503.png)

The body says exactly what to do:

```json
{
  "detail": {
    "error": "locked",
    "message": "the API is locked: its token store has not been unlocked since the last restart",
    "hint": "a super admin must supply the global token: python3 netapp_cascade_migration.py --action api-unlock"
  }
}
```

A super admin unlocks it from a shell **on the API server** — this is the one
step that cannot be done from the browser, by design: the global token never
travels over HTTP.

```bash
python3 netapp_cascade_migration.py --action api-unlock \
    --unlock-socket /opt/netapp-migration/etc/unlock.sock
#   Global token (super admin): ********
#   API unlocked (unlocked, 3 delegated token(s)).
```

Reload the page afterwards.

> Running the API in the foreground instead (`python3 -m
> netapp_migration.interfaces.api.serve`) makes it prompt for the token
> before it binds the port; there is nothing to unlock in that case.

---

## 3. The landing page

Once unlocked, `/docs` lists everything the API can do.

![Swagger UI landing page](images/02-overview.png)

Read it as four families:

| Family | Endpoints | Who |
|---|---|---|
| Authentication | `/auth/whoami`, `/auth/scopes*` | super admin |
| Migration life cycle | `/migrations`, `/migrations/{job_id}`, `…/resume`, `…/retry`, `…/refresh` | super admin |
| Per-qtree work | `…/test`, `…/clone`, `…/acl`, `…/cleanup` | client tokens, within their scope |
| Feasibility | `/preflight/create`, `…/preflight/{action}` | anyone with the matching scope |

---

## 4. Authenticating

Every endpoint except `/health` needs a token. Without one you get `401`:

![401 without a token](images/03-no-token-401.png)

Click **Authorize** (top right), paste the token, then **Authorize** again
and **Close**. Swagger UI now attaches `Authorization: Bearer …` to every
call you make from this page.

![The Authorize dialog](images/04-authorize-dialog.png)

Check who you are with `GET /api/v1/auth/whoami`. Click the operation,
**Try it out**, then **Execute**:

![whoami as super admin](images/05-whoami-super-admin.png)

`"super_admin": true` means the global token was accepted: no restriction on
actions or qtrees. A delegated token answers with its own `qtrees` and
`actions` lists instead.

`GET /api/v1/health` needs no token at all — use it to check that the API is
up *and* unlocked:

![health endpoint](images/06-health.png)

---

## 5. Delegating tokens to clients

Only the super admin can do this. `POST /api/v1/auth/scopes/import` takes a
CSV of `qtree,token,actions[,label]`. Write `NEW_TOKEN` in the token column
and the API generates one:

![Importing scopes from CSV](images/07-scopes-import.png)

The answer echoes a CSV containing the **generated tokens in clear** — this
is the only time they are ever readable. Hand each one to its owner and do
not keep the response; the store itself only ever holds salted hashes.

`GET /api/v1/auth/scopes` lists what exists, without ever showing a token:

![Listing delegated scopes](images/08-scopes-list.png)

Scopes can be changed later with `PATCH /auth/scopes/{token_id}` and revoked
with `DELETE /auth/scopes/{token_id}`.

---

## 6. Checking feasibility before acting

Every action verifies its own prerequisites before touching anything, and
each check can also be run on its own. `POST /api/v1/preflight/create`
answers with a report and changes nothing:

![Pre-flight report for create](images/09-preflight-create.png)

Read `ok` first (plus `failed_count` / `warning_count`), then the `checks`
list. Each entry carries a stable `code`, a human `title`, what was actually
observed (`detail`), the `target` it was looking at, and a `hint` when there
is something to do about it. A failing check tells you *why*, not just *no*:

```json
{
  "code": "SVM_MISSING",
  "title": "Pivot SVM exists",
  "passed": false,
  "severity": "error",
  "detail": "vserver 'svm_pivot' not found on clu-pivot-01",
  "hint": "create the SVM or fix the --*-vserver parameter",
  "target": "clu-pivot-01 / svm_pivot"
}
```

`severity` matters: `error` blocks the action, `warning` does not. A common
warning is `*_UNREADABLE` — the object may well exist, but the API account's
ONTAP role cannot read it, so the check could not conclude. Its `hint` names
the missing grant (for example `grant readonly on /api/svm/peers`).

The same endpoint exists per action and per job:
`POST /api/v1/migrations/{job_id}/preflight/{action}` with `action` being
`resume`, `retry`, `test`, `clone`, `acl` or `cleanup`.

---

## 7. Creating the cascade

`POST /api/v1/migrations` starts the migration. The pre-flight runs
**synchronously first**: a refusal comes back as `422` with the same report
as above, and nothing is created. Only when it passes does the work start in
the background and the call answers `202`:

![Creating a migration](images/10-create.png)

Keep the `job_id` from the answer — every later call needs it.

Two fields worth knowing:

* `create_mode`: `pivot-only` sets up Source → Pivot and stops there;
  `full` goes all the way to PROD and DR.
* `dry_run`: `true` simulates everything, contacts no cluster and writes no
  job state. Use it to rehearse a run. *(Every screenshot in this guide was
  produced this way.)*

---

## 8. Following a running migration

`GET /api/v1/migrations` lists the known jobs:

![Listing migrations](images/11-list-migrations.png)

`GET /api/v1/migrations/{job_id}/status` gives the live state — paste the
`job_id` into the field, then **Execute**:

![Job status](images/12-status.png)

The status reports the SnapMirror relationships in explicit terms
(`MIRROR_HEALTHY`, `TRANSFER_ACTIVE`, `TRANSFER_FAILED`, `MIRROR_BROKEN`,
`MIRROR_ABSENT`) rather than raw ONTAP strings, plus the checkpoint the job
has reached. `POST …/refresh` re-reads the clusters and rewrites the job
file; `POST …/resume` continues a job that stopped at a checkpoint, and
`POST …/retry` re-runs the failed phase.

---

## 9. Per-qtree actions: test, clone, acl

These are the operations a client token is allowed to run, within its own
scope.

### test — build the full future environment, reversibly

`POST /api/v1/migrations/{job_id}/test` creates the FlexClones on the future
PROD **and** DR and the SnapMirror relationship between them — everything
except the split and the volume move. The client can validate access and
permissions on a real environment; nothing is committed.

Check it first with `preflight/test`:

![Pre-flight for test](images/13-preflight-test.png)

Then run it:

![Running test](images/14-test.png)

#### Writing `volume_map`

It answers two questions per qtree: **which volume to create**, and **what
the qtree is called inside it**. You choose both; nothing is generated.

The short form gives the volume only, and the qtree keeps its source name:

```json
{
  "qtrees": "q_finance,q_hr",
  "volume_map": {
    "q_finance": "vol_fin_prod",
    "q_hr":      "vol_rh_prod"
  }
}
```

The full form adds the new qtree name. The two styles mix freely — here
`q_finance` is renamed and `q_hr` is not:

```json
{
  "qtrees": "q_finance,q_hr",
  "volume_map": {
    "q_finance": { "volume": "vol_fin_prod", "new_qtree": "finance" },
    "q_hr":      { "volume": "vol_rh_prod" }
  }
}
```

`new_qtree` omitted, empty, or equal to the source name all mean the same
thing: no rename. Two further shapes are accepted if they suit your caller
better — a list, or the CSV as a JSON string:

```json
{"volume_map": [{"qtree": "q_finance", "volume": "vol_fin_prod", "new_qtree": "finance"}]}
{"volume_map": "qtree,volume,new_qtree\nq_finance,vol_fin_prod,finance\n"}
```

What the pre-flight enforces, and the code it answers with:

| Rule | Refusal |
|---|---|
| Every qtree listed in `qtrees` has an entry | `VOLUME_MAP_MISSING` |
| `volume` is present and free on PROD **and** DR | `VOLUME_ALREADY_EXISTS` |
| No two qtrees share a volume name | `VOLUME_MAP_DUPLICATE` |
| `volume` is a legal ONTAP volume name | `VOLUME_NAME_ILLEGAL` |
| `new_qtree` has none of `/ \ : * ? " < > |`, ≤ 64 characters | `QTREE_NAME_ILLEGAL` |
| `new_qtree` is not already a qtree of the source volume | `QTREE_NAME_TAKEN` |
| No two qtrees take the same new name | `QTREE_NAME_DUPLICATE` |

Keys are matched case-insensitively against the qtrees you listed. A `clone`
run after a `test` **inherits** the mapping recorded in the job file — send
it again only when it changes.

The rename is applied on the **PROD clone only** (the DR clone is a mirror
destination, hence read-only) and **before** the clone mirror is created, so
the first resync carries the new name to DR.

#### Pruning: one volume, one client's data

A FlexClone copies the **whole** parent volume, so the volume created for
`q_finance` starts out holding `q_hr`, `q_ops` and every other qtree of the
source — other clients' data inside this client's volume.

`test` and `clone` therefore delete, in each clone, every qtree it did not
come for. This is **on by default**; send `"prune": false` to keep
everything, and the pre-flight will warn (`PRUNE_DISABLED`).

It happens right after the clones are created, before the mirror and before
the volume move, so the DR clone never holds the surplus either and the move
relocates only what is left. **PROD only**, and the **source volume is never
touched**. The pre-flight lists the deletions in advance:

```json
{
  "code": "PRUNE_PLAN",
  "severity": "warning",
  "detail": "keeps 'q_finance', deletes 2: q_hr, q_ops",
  "target": "clu-prod-01 / svm_prod:vol_fin_prod"
}
```

> That entry does not appear in the screenshots above: this walkthrough runs
> in dry-run, where the job never reaches `completed` and the source qtrees
> are simulated, so the mapping checks stop earlier. Run the same call
> against a real job to see it.

`validity_days` (default 7) records when the test environment expires.

### clone — promote the test environment

`POST /api/v1/migrations/{job_id}/clone` before expiry **promotes** what
`test` built: it only performs the volume moves that detach the clones from
their parent. Nothing is rebuilt.

![Running clone](images/15-clone.png)

`"fresh": true` ignores an existing test environment and runs the full flow
on a clean base — the old test clones stay on the clusters and are listed at
the end of the run for manual deletion.

### acl — force AD groups onto a path

`POST /api/v1/migrations/{job_id}/acl` is completely independent of
test/clone and acts on **one explicit path**:

![Applying ACLs](images/16-acl.png)

Backslashes must be escaped in JSON: `"CORP\\grp_finance_rw"`.

---

## 10. What a scoped token can and cannot do

Authorize again with a delegated token to see enforcement from the client's
side:

![Authorizing with a delegated token](images/17-authorize-scoped.png)

A qtree outside the token's scope is refused with `403`, and the answer shows
what the token *does* have — so the client can tell whether they mistyped or
were never granted it:

![403 on an out-of-scope qtree](images/18-scoped-forbidden-qtree.png)

Reserved actions are refused the same way. `create`, `resume`, `retry`,
`refresh` and token administration are super-admin only, whatever the
token's qtree scope:

![403 on a super-admin action](images/19-scoped-forbidden-action.png)

---

## 11. Reading the answers

| Code | Meaning | What to do |
|---|---|---|
| `200` | done, synchronous action | — |
| `202` | accepted, running in the background | poll `GET …/status` |
| `401` | no token, or an unknown one | click **Authorize** |
| `403` | authenticated, but out of scope | the body lists the granted scope |
| `404` | unknown `job_id` | check `GET /api/v1/migrations` |
| `409` | another action is already running on this job | wait, then poll the status |
| `422` | pre-flight refusal, or a malformed body | read `checks`: nothing was changed |
| `503` | API locked, or no token store | unlock it (section 2) |

A `422` from pre-flight always has this shape:

```json
{
  "detail": {
    "error": "preflight_failed",
    "message": "pre-flight for action 'clone': 2 check(s) failed (SVM_MISSING, CLUSTER_PEER_MISSING)",
    "action": "clone",
    "failed_checks": [ { "code": "SVM_MISSING", "passed": false, "severity": "error", "detail": "...", "hint": "..." } ],
    "warnings": [ ... ],
    "checks": [ ... ],
    "hint": "no cluster was modified; fix the failed checks and retry"
  }
}
```

`failed_checks` holds only what blocks you — start there. `checks` carries the
full report, passed checks included.

Nothing is ever half-done because of a `422`: the checks run before the first
write.

---

## Regenerating this guide

The screenshots are produced from the code, against a real running instance,
so they cannot silently drift from the API:

```bash
python3 -m pip install playwright pillow     # browsers are pre-installed
python3 tools/capture_swagger_guide.py
```

It starts an API on port 8321 with a temporary token store and the dry-run
transport, drives Chromium through the whole walkthrough, writes
`docs/images/*.png`, and deletes everything it created. Re-run it whenever
the API surface or the Swagger version changes.

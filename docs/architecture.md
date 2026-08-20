# Architecture & workflow

*Companion to [README.md](../README.md). Diagrams are Mermaid: they render on
GitHub and in most Markdown viewers.*

---

## 1. Overall architecture

```mermaid
flowchart TB
    subgraph users["Users"]
        OP["Storage admin<br/>(CLI)"]
        API_CLIENT["API client<br/>(curl / Swagger UI)"]
        ENDUSER["End client<br/>(CIFS shares)"]
    end

    subgraph server["Orchestration server (Linux, Python 3.9, offline)"]
        direction TB
        CLI["CLI<br/>netapp_cascade_migration.py<br/>tables + Y diagram"]
        REST_API["REST API — FastAPI + uvicorn:8000<br/>local Swagger UI /docs"]

        subgraph core["Core"]
            ENGINE["MigrationEngine<br/>8 actions"]
            PREFLIGHT["PreflightChecker<br/>feasibility checks"]
            JOBS["JobStore<br/>netapp_migration_&lt;ID&gt;.json"]
        end

        subgraph transport["Transport — OntapClient"]
            REST_T["rest.py (default)<br/>HTTPS 443, basic auth"]
            SSH_T["ssh.py (fallback)<br/>ONTAP CLI over SSH"]
            DRY_T["dryrun.py<br/>simulation"]
        end

        CREDS[("creds.json<br/>per-cluster credentials")]
    end

    subgraph ontap["ONTAP estate — 4 clusters, ONTAP 9.16.1"]
        SRC["SOURCE cluster<br/>svm_source"]
        PIV["PIVOT cluster<br/>svm_pivot"]
        PRD["PROD cluster<br/>svm_dest"]
        DR["DR cluster<br/>svm_dr"]
    end

    OP --> CLI
    API_CLIENT -->|"HTTP :8000"| REST_API
    CLI --> ENGINE
    REST_API --> ENGINE
    ENGINE --> PREFLIGHT
    ENGINE --> JOBS
    PREFLIGHT -.->|"read-only probes"| transport
    ENGINE --> transport
    CREDS -.-> REST_T

    REST_T -->|"HTTPS 443<br/>mutrepli / basic auth"| SRC
    REST_T --> PIV
    REST_T --> PRD
    REST_T --> DR
    SSH_T -.->|"SSH 22, fallback"| PIV

    SRC -->|"SnapMirror XDP"| PIV
    PIV -->|"SnapMirror"| PRD
    PIV -->|"SnapMirror"| DR
    PRD -.->|"clone mirror"| DR
    PRD -->|"CIFS"| ENDUSER
```

Both interfaces call the **same engine**; the engine reaches clusters only
through the **OntapClient** contract, so swapping REST for SSH changes no
business logic. State lives in **JSON job files** — no database, no broker;
background work runs in threads of the uvicorn process.

---

## 1bis. Authentication and scopes

```mermaid
flowchart TB
    subgraph install["Install — once"]
        INIT["tokens-init<br/>super admin types the GLOBAL token"]
        STORE[("netapp_tokens.enc<br/>Fernet AES-128-CBC + HMAC<br/>key = PBKDF2(global token)")]
        INIT --> STORE
    end

    subgraph grant["Delegation — CSV driven"]
        CSVIN["qtree,token,actions<br/>q_fin,NEW_TOKEN,&quot;test,clone,acl&quot;"]
        IMPORT["tokens-import<br/>POST /auth/scopes/import"]
        CSVOUT["answer CSV<br/>generated tokens, once"]
        CSVIN --> IMPORT --> CSVOUT
        IMPORT -.->|"hashes only"| STORE
    end

    subgraph run["Runtime"]
        SERVE["serve.py<br/>prompts for the GLOBAL token"]
        MEM["store unlocked<br/>IN MEMORY ONLY"]
        REQ["request<br/>Authorization: Bearer ..."]
        SERVE --> MEM
        STORE --> SERVE
        REQ --> MEM
        MEM --> SUPER["super admin<br/>everything"]
        MEM --> SCOPED["scoped token<br/>its actions, its qtrees"]
    end

    STOP["service restart"] -.->|"memory lost"| LOCKED["API answers 503<br/>until a manual unlock"]
```

**The global token** is typed by the super admin at install time and again at
every start of the service. It is never written anywhere: it is the key
material of the store (PBKDF2-HMAC-SHA256, 600 000 iterations) and doubles as
the super-admin API token. Lose it and the store cannot be recovered.

**Delegated tokens** are stored as salted hashes only. A generated token
appears in clear exactly once, in the answer CSV. The store file also holds
no qtree name in clear — the whole payload is encrypted.

**Scope model**

| Action | Who |
|---|---|
| `create`, `resume`, `retry`, `refresh`, token administration | super admin only — they act on the whole cascade |
| `test`, `clone`, `acl`, `cleanup` | delegable, restricted to the token's qtrees |
| `status`, `preflight`, `read` | delegable, read-only |

A scoped token that names a qtree outside its grant gets `403` with the list
of what it does hold. Scopes change at any time with the super-admin token
(`PATCH /auth/scopes/{id}`) without re-issuing the token.

**Restart policy** — the decrypted store lives only in memory, so a restart
of the service leaves the API locked (`503`) until a super admin supplies the
global token again on the command line. That is deliberate: an unattended
restart must not silently re-open the API.

---

## 2. What the API can do

```mermaid
flowchart LR
    subgraph read["Read — no side effect"]
        H["GET /health"]
        L["GET /migrations"]
        G["GET /migrations/{id}<br/>job + last run + logs"]
        S["GET /migrations/{id}/status<br/>live ONTAP state"]
    end

    subgraph check["Check — feasibility only, never mutates"]
        PC["POST /preflight/create"]
        PA["POST /migrations/{id}/preflight/{action}"]
    end

    subgraph sync["Act — synchronous (200)"]
        RS["POST /migrations/{id}/resume<br/>confirm required"]
        AC["POST /migrations/{id}/acl"]
        CU["POST /migrations/{id}/cleanup"]
        RF["POST /migrations/{id}/refresh"]
    end

    subgraph bg["Act — background (202 + poll)"]
        CR["POST /migrations"]
        RT["POST /migrations/{id}/retry"]
        TS["POST /migrations/{id}/test"]
        CL["POST /migrations/{id}/clone"]
    end

    check -.->|"same checks<br/>run again"| sync
    check -.-> bg
    bg -->|"poll"| G
```

### Endpoint reference

| Method | Endpoint | Purpose | Answers |
|---|---|---|---|
| `GET` | `/api/v1/health` | service liveness + auth state — **public** | `200` |
| `GET` | `/api/v1/auth/whoami` | scope of the presented token | `200` `401` |
| `POST` | `/api/v1/auth/scopes/import` | apply the qtree/token/actions CSV — **super admin** | `200` `422` |
| `GET` | `/api/v1/auth/scopes` | list delegated scopes (never the tokens) — **super admin** | `200` |
| `PATCH` | `/api/v1/auth/scopes/{id}` | change a scope dynamically — **super admin** | `200` `400` |
| `DELETE` | `/api/v1/auth/scopes/{id}` | revoke a token — **super admin** | `204` `400` |
| `GET` | `/api/v1/migrations` | list jobs | `200` |
| `GET` | `/api/v1/migrations/{id}` | job record, last run state, log tail, last pre-flight report | `200` `404` |
| `GET` | `/api/v1/migrations/{id}/status` | live replication state — **read-only**, never writes the job | `200` `404` `502` |
| `POST` | `/api/v1/migrations/{id}/refresh` | same as above **and** persists a finished replication | `200` `404` |
| `POST` | `/api/v1/preflight/create` | can a cascade be created? | `200` |
| `POST` | `/api/v1/migrations/{id}/preflight/{action}` | is this action feasible? (`resume`, `retry`, `test`, `clone`, `acl`, `cleanup`) | `200` `400` `404` |
| `POST` | `/api/v1/migrations` | create the cascade | `202` `422` |
| `POST` | `/api/v1/migrations/{id}/resume` | fan out to PROD + DR | `200` `409` `422` |
| `POST` | `/api/v1/migrations/{id}/retry` | resume a failed create | `202` `422` |
| `POST` | `/api/v1/migrations/{id}/test` | build the test environment | `202` `422` |
| `POST` | `/api/v1/migrations/{id}/clone` | definitive clones (promotion / fresh / full) | `202` `422` |
| `POST` | `/api/v1/migrations/{id}/acl` | force AD-group DACLs on one path | `200` `422` |
| `POST` | `/api/v1/migrations/{id}/cleanup` | cut source access for one, several or all migrated qtrees | `200` `422` |

### Status codes and what they mean

| Code | Meaning | What to do |
|---|---|---|
| `200` | done, `result` carries the outcome | — |
| `202` | accepted, running in background | poll `GET /migrations/{id}` |
| `400` | unknown action name | fix the URL |
| `404` | no such job | check the job id / job directory |
| `409` | another action is running on this job, **or** confirmation required | wait, or re-POST with `{"confirm": true}` |
| `401` | missing, unknown or revoked token | present a valid token |
| `403` | the token's scope does not cover this action or qtree | ask the super admin to widen the scope |
| `503` | the token store is locked (service restarted) or not initialised | a super admin must restart the service with the global token |
| `422` | **pre-flight refused the action — nothing was modified** | fix the listed checks, retry |
| `502` | ONTAP failed during execution | read `detail`, check the log file |
| `500` | unexpected failure | read the log file |

### Shape of a 422 (the important one)

Every refusal itemises what was verified, what was observed and how to fix it:

```json
{
  "detail": {
    "error": "preflight_failed",
    "message": "pre-flight for action 'create': 2 check(s) failed (SCHEDULE_MISSING, SVM_PEER_MISSING)",
    "action": "create",
    "failed_checks": [
      {
        "code": "SCHEDULE_MISSING",
        "title": "DR: transfer schedule visible",
        "passed": false,
        "severity": "error",
        "detail": "schedule 'hourly' not visible to the API user",
        "hint": "job schedule cron create -name hourly -minute 5, or grant readonly on /api/cluster/schedules",
        "target": "CMOPARDC5SFS100 / hourly"
      }
    ],
    "warnings": [],
    "checks": [ "… every check, passed ones included …" ],
    "hint": "no cluster was modified; fix the failed checks and retry the same call"
  }
}
```

---

## 3. Pre-flight coverage per action

Nothing is attempted before these are satisfied. Codes are stable and safe to
consume by automation.

| Action | What gets verified |
|---|---|
| `create` | all parameters present; four **distinct** clusters; the 4 SVMs exist and run; source volume exists (size + security style read); the 3 aggregates exist and have room; the 3 DP volumes do **not** already exist; **cluster peering + SVM peering** on the 3 legs; SnapMirror **policy and schedule visible** on each destination cluster; the 3 relationships do **not** already exist |
| `resume` | job is exactly at `pivot_initialized`; pivot mirrored **and** idle; PROD/DR volumes exist; PROD/DR relationships declared and **not already initialized** (blocks a double initialize) |
| `retry` | job status recognised; for phases still to run, the same peering / policy / schedule checks as `create` |
| `test` | cascade complete and **healthy on all three legs**; **no test environment already in place**; qtrees exist on the source and are unique; derived clone name is a legal ONTAP volume name; peering + policy + schedule for the clone mirror PROD→DR |
| `clone` (promotion) | requested qtrees **exactly match** the test set; each clone exists on PROD and DR; each clone mirror is healthy and idle; validity not expired (warning); a move-target aggregate exists other than the parent's |
| `clone` (full/fresh) | same as `test`, plus the move-target aggregate check; `--fresh` warns that the old test clones are abandoned |
| `acl` | path provided, absolute, no traversal, **not `/`**; path belongs to a **clone volume of this job**; path resolves on the PROD SVM; volume security style is NTFS/mixed; AD group syntax |
| `test` / `clone` naming | **every qtree has an explicit target volume name**; names are distinct; each is a legal ONTAP volume name; each is free on PROD **and** DR |
| `cleanup` | at least one qtree, existing on the source, no duplicate, no path separator; migration `completed`; the qtree is in the job's `volume_map` and the migrated volume is present on PROD **and** DR; not already cleaned up (`_MIG_`); the new name is free; clones promoted (warning); **explicit preview of the exact CIFS shares** that will be deleted |

---

## 4. Migration workflow

### 4.1 Life cycle

```mermaid
stateDiagram-v2
    [*] --> started: create
    started --> space_checked
    space_checked --> volumes_created
    volumes_created --> relationships_created
    relationships_created --> pivot_initialized: pivot initialize fired
    pivot_initialized --> dest_initialized: resume (confirmed)
    dest_initialized --> completed: PROD + DR mirrored AND idle
    started --> started: retry
    space_checked --> volumes_created: retry
    volumes_created --> relationships_created: retry
    completed --> completed: test / acl / clone / cleanup
```

A checkpoint is written **immediately after** each phase, so `retry` resumes
exactly where it stopped and skips completed phases (creates are idempotent).

### 4.2 create — full mode

```mermaid
sequenceDiagram
    participant OP as Operator
    participant E as Engine
    participant PF as Pre-flight
    participant SRC as SOURCE
    participant PIV as PIVOT
    participant PRD as PROD
    participant DR as DR

    OP->>E: create
    E->>PF: for_create()
    PF-->>SRC: volume, SVM
    PF-->>PIV: aggregate, peering, policy, schedule
    PF-->>PRD: idem
    PF-->>DR: idem
    PF-->>E: report (blocks on any failure)

    E->>SRC: read size + security style
    E->>PIV: create DP volume
    E->>PRD: create DP volume
    E->>DR: create DP volume
    Note over E: checkpoint volumes_created
    E->>PIV: snapmirror create (source→pivot)
    E->>PRD: snapmirror create (pivot→PROD)
    E->>DR: snapmirror create (pivot→DR)
    Note over E: checkpoint relationships_created
    E->>PIV: snapmirror initialize
    Note over E: checkpoint pivot_initialized

    alt pivot-only
        E-->>OP: job id, exit immediately
    else full
        E->>PIV: poll until mirrored AND idle
        E->>PRD: snapmirror initialize
        E->>DR: snapmirror initialize
        Note over E: checkpoint dest_initialized
        E->>PRD: poll until ready
        E->>DR: poll until ready
        E-->>OP: completed
    end
```

The strict rule: **PROD and DR are never initialized before the pivot is
mirrored and idle**, then both start back-to-back (Y fan-out).

### 4.3 resume — confirmation gate

```mermaid
sequenceDiagram
    participant C as Caller
    participant API
    participant E as Engine
    participant PIV as PIVOT
    participant PRD as PROD
    participant DR as DR

    C->>API: POST /resume {}
    API->>E: resume(confirm=false)
    E->>PIV: state?
    PIV-->>E: mirrored + idle
    E-->>API: ConfirmationRequired
    API-->>C: 409 — re-POST with {"confirm": true}

    C->>API: POST /resume {"confirm": true}
    API->>E: resume(confirm=true)
    E->>PRD: snapmirror initialize
    E->>DR: snapmirror initialize
    E-->>C: 200, job = dest_initialized
```

### 4.4 test — full environment, no split

```mermaid
sequenceDiagram
    participant E as Engine
    participant SRC as SOURCE
    participant PIV as PIVOT
    participant PRD as PROD
    participant DR as DR

    Note over E: pre-flight: cascade healthy, qtrees valid,<br/>no existing test env, clone-mirror peering
    E->>SRC: snapshot test_migr_<stamp>
    E->>PIV: snapmirror update → wait idle
    par fan-out
        E->>PRD: snapmirror update
        E->>DR: snapmirror update
    end
    E->>PRD: verify snapshot present
    E->>DR: verify snapshot present
    loop per qtree
        E->>PRD: FlexClone v_<qtree>_<uid>
        E->>DR: FlexClone v_<qtree>_<uid>
        E->>DR: snapmirror create + resync (PROD clone → DR clone)
    end
    E->>DR: wait every clone mirror idle
    Note over E: job records clone_uid, test_env=true,<br/>test_expires_at (+N days)
    Note over E: NO volume move → clones stay thin (0 extra space)
```

### 4.5 clone — three modes

```mermaid
flowchart TD
    START["POST /clone {qtrees, fresh}"] --> PF["Pre-flight"]
    PF --> Q{"test_env in job?"}
    Q -->|"no"| FULL["FULL FLOW<br/>snapshot → propagation → clones<br/>→ clone mirror + resync"]
    Q -->|"yes, fresh=false"| PROMO["PROMOTION<br/>nothing rebuilt"]
    Q -->|"yes, fresh=true"| FRESH["FULL FLOW, new uid<br/>old test clones abandoned<br/>(delete commands printed)"]

    PROMO --> CHK["verify clone mirrors idle"]
    FULL --> AGG
    FRESH --> AGG
    CHK --> AGG["select best aggregate<br/>(parent's excluded)"]
    AGG --> MOVE["volume move start on PROD + DR<br/>= detach clone from parent"]
    MOVE --> EXIT["exit immediately<br/>moves run asynchronously"]
```

### 4.6 End-to-end operational sequence

```mermaid
flowchart LR
    A["create<br/>pivot-only"] --> B["check-status<br/>(poll)"]
    B --> C["resume<br/>+ confirm"]
    C --> D["check-status<br/>until completed"]
    D --> E["test<br/>N days"]
    E --> F["client validates<br/>access + permissions"]
    F --> G{"OK?"}
    G -->|"yes"| H["clone<br/>= promotion"]
    G -->|"no"| I["delete test clones<br/>fix, test again"]
    I --> E
    H --> J["cleanup<br/>one, several or all<br/>migrated qtrees"]
    A -.->|"on failure"| R["retry"]
    R --> B
    K["acl — independent,<br/>any destination path"] -.-> F
```

---

## 5. Known limitations

These are deliberately **not** addressed by the pre-flight work and remain open:

* **The API has no authentication** — every endpoint is anonymous. Do not
  expose port 8000 beyond a trusted host; put an authenticated reverse proxy
  in front, or wait for the auth lot.
* **No cluster allowlist** — cluster names come from the request and the
  `defaults` credentials would be sent to any host named. Keep the API closed
  until this is fixed.
* **`transport=ssh` is still accepted by the API**, and SSH commands
  interpolate identifiers without ONTAP-specific escaping.
* **No per-resource locking** — two jobs targeting the same volume can run
  concurrently; only one action per job id is serialised.
* **Volume moves are launched, not tracked** — `clone` exits once the moves
  start, by design; their completion is not verified by the tool.
* **The CLI is driven by the super admin only.** Delegated tokens are meant
  for the REST API; they cannot open the local encrypted store.
* **Clones contain every qtree of the source volume.** A FlexClone copies the
  whole parent, so after the move each target volume holds all the source
  qtrees — a data-segregation and capacity issue that needs a product
  decision (prune the other qtrees after the move, or expose only the right
  one through the CIFS share).

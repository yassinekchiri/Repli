# NetApp Cascade Migration

*English version: [README.md](README.md)*

Outil d'orchestration de migration NetApp ONTAP en topologie **Y** : un volume
source est répliqué à travers un cluster pivot vers **deux destinations
simultanées** (PROD + DR), puis découpé en volumes clones (1 volume = 1 qtree).

```
    [ SOURCE ]  cluster source
         |
         |  SnapMirror (relation 1)
         v
    [ PIVOT  ]  cluster de transit
        /  \
       /    \      relations 2 et 3 (fan-out simultané)
      v      v
  [ PROD  ]    [ DR ]
```

Tous les appels vers les clusters passent par **l'API REST ONTAP**
(authentification basic auth) par défaut. Un transport SSH est conservé en
secours (`--transport ssh`). Le même **moteur** est appelé par deux
interfaces : la **CLI** et une **API REST** (FastAPI).

Version ONTAP minimale : 9.9.1 — validé pour 9.16.1.

---

## 1. Architecture

```
netapp_migration/
├── config.py               # credentials REST + répertoire des jobs
├── models.py               # dataclasses partagées (params, erreurs, objets ONTAP)
├── transport/
│   ├── base.py             # OntapClient : le contrat abstrait
│   ├── rest.py             # implémentation API REST ONTAP (défaut)
│   ├── ssh.py              # implémentation CLI ONTAP via SSH (fallback)
│   └── dryrun.py           # simulation --dry-run (aucun cluster contacté)
├── core/
│   ├── engine.py           # MigrationEngine : les 8 actions métier
│   └── jobs.py             # JobStore : fichiers de job JSON + checkpoints
└── interfaces/
    ├── cli.py              # interface ligne de commande
    └── api/
        ├── app.py          # application FastAPI
        └── schemas.py      # modèles Pydantic requête/réponse

netapp_cascade_migration.py # point d'entrée CLI (compatibilité historique)
requirements.txt
```

Les fichiers de job (`netapp_migration_<ID>.json`) sont **compatibles** avec
ceux générés par l'ancienne version mono-fichier du script.

---

## 2. Installation

### 2.1 Prérequis

* Python **3.9+** (validé en 3.11)
* Accès réseau HTTPS (443) vers les LIF de management des clusters ONTAP
* Un compte ONTAP avec les droits API REST (rôle `admin` ou rôle dédié avec
  accès aux endpoints `storage`, `snapmirror`, `protocols`)

### 2.2 Étapes

```bash
# 1. Récupérer le code
git clone <url-du-repo> && cd Repli

# 2. Créer un environnement virtuel
python3 -m venv .venv
source .venv/bin/activate

# 3. Installer les dépendances
pip install -r requirements.txt

# (optionnel) transport SSH avec le backend paramiko :
# pip install paramiko
```

### 2.3 Configuration des credentials REST

Créer un fichier `creds.json` (à protéger : `chmod 600 creds.json`) :

```json
{
  "defaults": {
    "username": "svc_migration",
    "password": "********",
    "verify_ssl": false,
    "port": 443
  },
  "clusters": {
    "CMOPARPA4SFS100": { "verify_ssl": true },
    "CMOPARDC5SFS100": { "username": "autre_compte", "password": "********" }
  }
}
```

* `defaults` s'applique à tous les clusters ; chaque entrée de `clusters`
  surcharge les valeurs pour un cluster précis.
* `verify_ssl: false` est nécessaire si les certificats des LIF sont
  auto-signés (un avertissement est loggé).

Le fichier est fourni via `--config creds.json` (CLI) ou la variable
d'environnement `NETAPP_MIGRATION_CONFIG` (CLI et API).

Alternative sans fichier — variables d'environnement :

```bash
export NETAPP_API_USER="svc_migration"
export NETAPP_API_PASSWORD="********"
```

### 2.4 Répertoire des jobs

Par défaut les fichiers de job sont écrits dans le répertoire courant.
Pour un emplacement fixe (recommandé pour l'API) :

```bash
export NETAPP_MIGRATION_JOB_DIR=/var/lib/netapp-migration/jobs
mkdir -p "$NETAPP_MIGRATION_JOB_DIR"
```

---

## 3. Utilisation en ligne de commande

```bash
export NETAPP_MIGRATION_CONFIG=/chemin/vers/creds.json
```

### 3.1 create — initialiser la cascade

```bash
python3 netapp_cascade_migration.py --action create \
    --source-cluster CMOPARTIGMUT100 \
    --pivot-cluster  CMOPARTIGBKP110 \
    --dest-cluster   CMOPARPA4SFS100 \
    --dr-cluster     CMOPARDC5SFS100 \
    --volume vol_prod_01 \
    --pivot-aggr aggr1_pivot --dest-aggr aggr1_dest --dr-aggr aggr1_dr
```

Mode deux temps (lancer le pivot, revenir plus tard) :

```bash
python3 netapp_cascade_migration.py --action create ... --create-mode pivot-only
python3 netapp_cascade_migration.py --action check-status --job-id <ID>
python3 netapp_cascade_migration.py --action resume       --job-id <ID>
```

### 3.2 retry — reprendre un create échoué

```bash
python3 netapp_cascade_migration.py --action retry --job-id <ID>
```

Reprend au dernier checkpoint (`space_checked`, `volumes_created`, ...) ;
les créations déjà faites sont ignorées (idempotence).

### 3.3 test — environnement de test complet (aucun espace consommé)

```bash
python3 netapp_cascade_migration.py --action test --job-id <ID> \
    --qtrees all --test-validity-days 7
```

Construit l'environnement cible **complet, sauf le split / volume move** :
FlexClones `v_<qtree>_<uid>` sur la future PROD **et** la future DR, avec
les **relations SnapMirror entre les clones** (resync attendu idle). Les
clones restent attachés à leur volume DP parent : zéro espace consommé.

L'environnement est **limité dans le temps** (`--test-validity-days`,
défaut 7 jours ; la date d'expiration est stockée dans le fichier de job).
Avant cette date :

* le client valide accès et permissions ;
* `--action clone` **promeut** cet environnement en définitif (seuls les
  volume moves sont lancés — rien n'est reconstruit) ;
* sinon, passé la date, les clones doivent être **supprimés** (les
  commandes exactes sont affichées en fin d'action).

Une seule invocation `test` par job : relancer `test` alors qu'un
environnement existe est refusé (le promouvoir ou le supprimer d'abord).

### 3.4 acl — forcer des groupes AD sur un path (DACL côté NetApp)

Action **totalement découplée** de `test` et `clone` : elle n'agit que sur
**un path** fourni par le client, invocable sur n'importe quel path de ses
volumes de destination.

```bash
python3 netapp_cascade_migration.py --action acl --job-id <ID> \
    --acl-path /v_q_fin_8072b8/projects \
    --ad-groups 'CORP\grp_rw,CORP\grp_ro' --acl-rights modify
```

`--acl-path` est obligatoire (chemin absolu sur le vserver de destination).
Le forçage est propagé sur toute l'arborescence (dossier, sous-dossiers,
fichiers) côté PROD ; la DR reçoit les ACLs via la réplication SnapMirror
des clones.

### 3.5 clone — clones définitifs + détachement

```bash
python3 netapp_cascade_migration.py --action clone --job-id <ID> --qtrees q_fin,q_hr
```

Trois modes :

* **Promotion** (défaut si un environnement `test` existe) : vérification
  que les miroirs de clones sont idle, puis `volume move` (détachement des
  parents). Rien n'est reconstruit ; les qtrees demandés doivent
  correspondre à ceux du test.
* **`--fresh`** — repartir sur une base propre **même si un test existe** :
  l'environnement de test est ignoré et le flux complet s'exécute (nouveau
  snapshot, nouveaux clones avec un nouvel UID). Les anciens clones de test
  restent en place ; les commandes pour les supprimer sont affichées en fin
  de run.

  ```bash
  python3 netapp_cascade_migration.py --action clone --job-id <ID> \
      --qtrees q_fin,q_hr --fresh
  ```
* **Flux complet** — pas d'environnement de test : snapshot dédié →
  propagation cascade → FlexClones sur PROD et DR → SnapMirror entre
  clones + resync → sélection automatique du meilleur aggregate →
  `volume move` → sortie immédiate.

### 3.6 cleanup — coupure d'accès source

```bash
python3 netapp_cascade_migration.py --action cleanup \
    --source-cluster ... --pivot-cluster ... --dest-cluster ... \
    --volume vol_prod_01 --qtree q_fin
```

### 3.7 Options transverses

| Option | Rôle |
|---|---|
| `--transport rest\|ssh` | transport ONTAP (défaut : `rest`) |
| `--config PATH` | fichier de credentials REST |
| `--api-user USER` | force le login REST (mot de passe via env/config) |
| `--insecure` | désactive la vérification TLS (certifs auto-signés) |
| `--job-dir PATH` | répertoire des fichiers de job |
| `--dry-run` | simulation complète, aucun cluster contacté |
| `--yes` | (resume) saute la confirmation interactive |
| `--timeout` / `--poll-interval` | polling SnapMirror (s) |
| `--log-file PATH` | fichier de log (défaut : `migration_<action>_<date>.log`) |

La console n'affiche que la progression ; le **fichier de log** contient la
trace DEBUG complète (chaque appel REST avec payloads, ou chaque commande
SSH avec stdout/stderr).

---

## 4. Lancer l'API REST

### 4.1 Démarrage

```bash
source .venv/bin/activate
export NETAPP_MIGRATION_CONFIG=/chemin/vers/creds.json
export NETAPP_MIGRATION_JOB_DIR=/var/lib/netapp-migration/jobs

uvicorn netapp_migration.interfaces.api.app:app --host 0.0.0.0 --port 8000
```

Documentation interactive (Swagger UI) : `http://<serveur>:8000/docs`

> **Important** : lancer **un seul worker** (pas de `--workers N`) — le
> registre des actions en cours est en mémoire de processus.

### 4.2 Endpoints

| Méthode | Endpoint | Action | Réponse |
|---|---|---|---|
| `POST` | `/api/v1/migrations` | create | `202` + job_id (fond) |
| `GET`  | `/api/v1/migrations` | liste des jobs | `200` |
| `GET`  | `/api/v1/migrations/{id}` | fichier de job + dernière action (logs) | `200` |
| `GET`  | `/api/v1/migrations/{id}/status` | état réplication live (interroge ONTAP) | `200` |
| `POST` | `/api/v1/migrations/{id}/resume` | fan-out PROD + DR | `200` / `409` |
| `POST` | `/api/v1/migrations/{id}/retry` | reprise après échec | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/test` | env de test complet (clones + miroir, limité dans le temps) | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/clone` | clones définitifs (promotion du test, ou `fresh` / flux complet) | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/acl` | forçage DACL groupes AD sur un path | `200` |
| `POST` | `/api/v1/migrations/{id}/cleanup` | coupure accès source | `200` |
| `GET`  | `/api/v1/health` | disponibilité du service | `200` |

Les actions longues (`create`, `retry`, `test`, `clone`) répondent `202`
immédiatement et s'exécutent en tâche de fond ; suivre l'avancement avec
`GET /api/v1/migrations/{id}` (état + dernières lignes de log). Une seule
action à la fois par job (`409` sinon).

### 4.3 Exemples curl

```bash
BASE=http://localhost:8000/api/v1

# Créer la cascade
curl -s -X POST $BASE/migrations -H 'Content-Type: application/json' -d '{
  "source_cluster": "CMOPARTIGMUT100",
  "pivot_cluster":  "CMOPARTIGBKP110",
  "dest_cluster":   "CMOPARPA4SFS100",
  "dr_cluster":     "CMOPARDC5SFS100",
  "volume": "vol_prod_01",
  "create_mode": "pivot-only"
}'
# -> {"job_id": "20260704_105603_2e70af", ...}

JOB=20260704_105603_2e70af

# Suivre l'avancement (fichier de job + logs de la dernière action)
curl -s "$BASE/migrations/$JOB?logs=30"

# État réplication live (interroge les clusters)
curl -s $BASE/migrations/$JOB/status

# Fan-out PROD + DR : un premier appel sans confirm renvoie 409 si le
# pivot est prêt ; confirmer explicitement :
curl -s -X POST $BASE/migrations/$JOB/resume \
     -H 'Content-Type: application/json' -d '{"confirm": true}'

# Environnement de test complet (clones + miroir PROD->DR, limité à 7 jours)
curl -s -X POST $BASE/migrations/$JOB/test \
     -H 'Content-Type: application/json' \
     -d '{"qtrees": ["q_fin", "q_hr"], "validity_days": 7}'

# Forcer des groupes AD sur UN path précis (découplé de test/clone)
curl -s -X POST $BASE/migrations/$JOB/acl \
     -H 'Content-Type: application/json' \
     -d '{"ad_groups": ["CORP\\grp_rw"], "acl_path": "/v_q_fin_8072b8/projects",
          "acl_rights": "modify"}'

# Clones définitifs : promeut l'environnement de test s'il existe
# (volume moves uniquement), sinon flux complet
curl -s -X POST $BASE/migrations/$JOB/clone \
     -H 'Content-Type: application/json' -d '{"qtrees": "all"}'

# Clones définitifs sur base propre en ignorant l'environnement de test
# (les anciens clones de test restent à supprimer manuellement)
curl -s -X POST $BASE/migrations/$JOB/clone \
     -H 'Content-Type: application/json' -d '{"qtrees": "all", "fresh": true}'

# Coupure d'accès source pour un qtree migré
curl -s -X POST $BASE/migrations/$JOB/cleanup \
     -H 'Content-Type: application/json' -d '{"qtree": "q_fin"}'
```

### 4.4 Service systemd (exemple)

```ini
# /etc/systemd/system/netapp-migration-api.service
[Unit]
Description=NetApp Cascade Migration API
After=network.target

[Service]
User=migration
WorkingDirectory=/opt/netapp-migration
Environment=NETAPP_MIGRATION_CONFIG=/etc/netapp-migration/creds.json
Environment=NETAPP_MIGRATION_JOB_DIR=/var/lib/netapp-migration/jobs
ExecStart=/opt/netapp-migration/.venv/bin/uvicorn \
    netapp_migration.interfaces.api.app:app --host 0.0.0.0 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now netapp-migration-api
```

---

## 5. Cycle de vie d'une migration

```
create (pivot-only) ──> check-status ──> resume ──> check-status ... completed
        │                                                    │
        └── retry (si échec, reprend au dernier checkpoint)  │
                                                             v
              test (env complet : clones + miroir, sans move, limité N jours)
                                                             │
                     validation client (accès, permissions)  │
                                                             v
              ┌─ avant expiration : clone = PROMOTION (vol moves seulement)
              ├─ à tout moment    : clone --fresh = flux complet sur base
              │                     propre (anciens clones de test à supprimer)
              └─ après expiration : suppression des clones de test,
                                    puis clone = flux complet
                                                             │
                                                             v
                                                          cleanup

acl (indépendante) : à tout moment, sur n'importe quel path de destination
```

Checkpoints du fichier de job (`--action retry` reprend au dernier atteint) :

```
started → space_checked → volumes_created → relationships_created
        → pivot_initialized → dest_initialized → completed
```

Après `test`, le fichier de job contient `clone_uid`, `clone_volumes`,
`test_env`, `test_created_at` et `test_expires_at` ; la promotion par
`clone` bascule `test_env` à `false` et enregistre `clone_promoted_at`.
Un `clone --fresh` remplace `clone_uid`/`clone_volumes` par ceux du
nouveau run — les anciens clones de test restent sur les clusters et sont
listés en fin de run pour suppression manuelle.

---

## 6. Dépannage

* **401/403 sur l'API ONTAP** : vérifier le compte dans `creds.json` et que
  son rôle autorise l'accès REST (`security login show`).
* **Erreur TLS** : certificats auto-signés → `"verify_ssl": false` dans le
  fichier de credentials, ou `--insecure` en CLI.
* **`job file not found`** : lancer la commande depuis le même
  `NETAPP_MIGRATION_JOB_DIR` (ou le même répertoire courant) que le run
  d'origine.
* **`409 action already running`** (API) : une action est déjà en cours sur
  ce job ; attendre sa fin (`GET /migrations/{id}`).
* **Trace complète** : le fichier `migration_<action>_<date>.log` contient
  chaque appel REST/SSH en DEBUG.

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
setup-ontap-account.sh      # côté ONTAP : crée le compte de service + les rôles
install.sh                  # installeur hors-ligne, exige le checkout + wheels/
install-standalone.sh       # installeur en UN seul fichier autonome (généré)
repo-selfextract.sh         # tout le dépôt en un fichier, extraction seule (généré)
tools/
├── payload.py              # mécanique d'empaquetage partagée par les deux
├── installer_template.sh   # corps de install-standalone.sh
├── build_standalone_installer.py
├── selfextract_template.sh # corps de repo-selfextract.sh
├── build_selfextract.py
└── capture_swagger_guide.py     # régénère les captures de docs/api-guide.fr.md
docs/
├── api-guide.md            # parcours illustré de Swagger UI (+ .fr.md)
├── architecture.md         # schémas, carte de l'API, workflows
└── images/                 # captures utilisées par le guide
```

Les fichiers de job (`netapp_migration_<ID>.json`) sont **compatibles** avec
ceux générés par l'ancienne version mono-fichier du script.

Voir [docs/architecture.md](docs/architecture.md) pour le schéma
d'architecture, la carte complète des capacités de l'API et les workflows
détaillés de la migration.

### Vérifications préalables

Chaque action vérifie ses prérequis sur les clusters **avant** de toucher à
quoi que ce soit : SVMs, volumes, aggregates et capacité, peering
cluster/SVM, visibilité de la policy et du schedule SnapMirror, état des
relations, existence des qtrees, export policies et les clients qu'elles
portent, périmètre du chemin ACL, prévisualisation des partages CIFS pour
cleanup. Une action refusée ne modifie rien et
détaille chaque contrôle en échec avec ce qui a été observé et comment le
corriger (tableau en CLI, HTTP 422). Interroger sans exécuter :

```bash
curl -s -X POST $BASE/preflight/create -d @create.json           # avant de créer
curl -s -X POST $BASE/migrations/$JOB/preflight/clone \
     -d '{"qtrees":"all"}'    # avant de cloner
```

### Tests

```bash
pip install --no-index --find-links wheels/ -r requirements-dev.txt
python3 -m pytest            # 311 tests, hors-ligne, aucun cluster contacté
```

---

## 2. Installation

### 2.1 Prérequis

* Python **3.9+** (validé en 3.11)
* Accès réseau HTTPS (443) vers les LIF de management des clusters ONTAP
* Un compte ONTAP avec les droits API REST (rôle `admin` ou rôle dédié avec
  accès aux endpoints `storage`, `snapmirror`, `protocols`)

### 2.2 Installation automatisée sur une VM vierge (recommandé)

`install.sh` fait tout hors-ligne, depuis le répertoire `wheels/` embarqué :
vérification des prérequis, création de l'environnement virtuel, installation
des dépendances, vérification du package, exécution de la suite de tests,
écriture d'un modèle de credentials, installation de l'unité systemd et
initialisation du coffre de tokens.

```bash
sudo ./install.sh                       # /opt/netapp-migration, unité systemd
sudo ./install.sh --prefix /opt/nm --user migration --port 8000
./install.sh --prefix "$HOME/nm" --no-service     # installation sans droits root
./install.sh --check                    # vérifie les prérequis, ne change rien
```

La seule chose qu'il ne peut pas fournir, c'est Python lui-même : un Python
**3.9+** avec le module `venv` doit déjà être présent sur la VM
(`apt-get install python3-venv` ou `dnf install python3-libs`). Tout le reste
vient de `wheels/`.

Le script refuse de démarrer plutôt que d'installer à moitié : si le bundle
n'a pas de roue compilée pour la version de Python de la VM, il le dit et
indique comment la régénérer. Il est réexécutable sans risque — un venv, un
fichier de credentials ou un coffre existant est détecté et conservé.

L'unité systemd n'est volontairement **pas activée au démarrage** : l'API
réclame le token global saisi par un super admin à chaque lancement.

### 2.2a Installation en un seul fichier (aucun accès au dépôt)

`install-standalone.sh` embarque **toute l'application dans le fichier
lui-même** : un seul fichier à transporter sur la VM, pas de `git clone`, pas
de checkout, rien d'autre à copier. Il déballe sa propre charge utile
(vérifiée en SHA-256), puis installe les dépendances Python depuis l'index de
paquets déjà configuré sur la machine.

À utiliser quand la VM n'a pas accès au dépôt de code mais *a* accès à un
miroir PyPI. `install.sh` (section 2.2) couvre le cas inverse : checkout
complet disponible, aucun index de paquets.

```bash
# copier le fichier unique sur la VM, puis :
sudo bash install-standalone.sh

# derrière un miroir interne / Artifactory :
sudo bash install-standalone.sh \
    --index-url https://artifactory.example/api/pypi/pypi/simple \
    --trusted-host artifactory.example

bash install-standalone.sh --check                 # vérifie seulement
bash install-standalone.sh --extract-only ./src    # déballe juste les sources
```

Il accepte les mêmes options qu'`install.sh`, plus `--index-url`,
`--extra-index-url`, `--trusted-host` et `--pip-timeout`. Il doit être lancé
en tant que **fichier**, pas dans un tube (`curl … | bash` ne laisse rien à
déballer).

Régénération après une modification du code — la charge utile est un
instantané, elle devient sinon obsolète :

```bash
python3 tools/build_standalone_installer.py
```

La construction est déterministe : un arbre inchangé produit un script
identique à l'octet près, `git status` reste donc silencieux si rien n'a
réellement changé.

### 2.2a-bis Transporter le dépôt lui-même (sans installer)

`repo-selfextract.sh` embarque **tout le dépôt** dans un seul fichier
exécutable — tout sauf `wheels/` et l'historique git. Il n'installe rien : il
déballe, et s'arrête. À utiliser pour amener les sources sur une machine qui
n'a pas accès au dépôt de code, quand c'est l'arborescence que l'on veut et
non un service qui tourne.

```bash
bash repo-selfextract.sh                  # extrait dans ./netapp-migration
bash repo-selfextract.sh --into /opt/src  # ailleurs
bash repo-selfextract.sh --list           # liste le contenu, n'extrait rien
bash repo-selfextract.sh --check          # vérifie l'intégrité seulement
bash repo-selfextract.sh --sha256         # affiche l'empreinte de la charge utile
```

Il refuse d'extraire dans un répertoire non vide sauf avec `--force`, vérifie
le SHA-256 de sa charge utile avant d'écrire quoi que ce soit, et rejette les
entrées d'archive au chemin absolu ou remontant. Il ne demande que `bash`,
`tar` et coreutils ; à défaut, il bascule sur `python3`.

La copie extraite est un **instantané, pas un clone** — il n'y a pas de
`.git`, les commits qui y seraient faits ne peuvent pas être repoussés.

Régénération après une modification du code :

```bash
python3 tools/build_selfextract.py
```

`git ls-files` décide quels chemins partent (les caches et artefacts de build
restent donc dehors), et le contenu est lu dans l'arbre de travail : une
modification non commitée d'un fichier suivi est incluse telle quelle.

### 2.2b Installation manuelle

**Étapes :**

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

`requirements.txt` borne chaque dépendance (plancher **et** plafond). Les
plafonds sont volontaires : ils marquent la première version amont qui a
abandonné Python 3.9, la version installée sur les serveurs cibles. Un
résolveur pip ancien prend d'abord la version la plus récente puis échoue
avec `Package 'requests' requires a different Python: 3.9.25 not in
'>=3.10'` au lieu de revenir en arrière — les plafonds évitent ça. Ne les
relever qu'après un nouveau test sur 3.9.

#### Dépendances hors-ligne (serveur sans accès aux dépôts)

Le dépôt embarque un répertoire `wheels/` avec tous les paquets requis
pré-téléchargés (CPython 3.9 à 3.12 / Linux x86_64). Sur un serveur qui
n'a accès à aucun miroir PyPI :

```bash
python3 -m venv .venv
source .venv/bin/activate
# mettre pip à jour d'abord (les vieux pip rejettent les wheels manylinux récents) :
pip install --no-index --find-links wheels/ --upgrade pip setuptools wheel
pip install --no-index --find-links wheels/ -r requirements.txt
```

Pour régénérer les wheels pour une autre version de Python ou une autre
architecture, voir [wheels/README.md](wheels/README.md).

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

### 2.5 Créer le compte de service ONTAP `mutrepli` (moindre privilège)

`setup-ontap-account.sh` fait tout cela pour un cluster, de façon idempotente :

```bash
./setup-ontap-account.sh                    # demande le cluster, puis le mot de passe
./setup-ontap-account.sh --cluster clu01    # ne demande que le mot de passe
./setup-ontap-account.sh --check            # rapporte l'existant, ne modifie rien
./setup-ontap-account.sh --dry-run          # affiche les commandes, n'en lance aucune
```

Il suppose que **votre propre** accès SSH administrateur au cluster
fonctionne déjà (clé ou agent) — il ne fait jamais que
`ssh <cluster> <commande ONTAP>` et ne demande jamais votre mot de passe.
Celui du nouveau compte est saisi au terminal, deux fois, masqué, et remis
directement au cluster : jamais en ligne de commande, jamais dans un
fichier, jamais dans la sortie du script.

Le relancer est sans risque : rôles, droits et logins existants sont
détectés et conservés. `--account`, `--rest-role`, `--cli-role` et
`--vserver` remplacent les valeurs par défaut ; `--no-ssh-login` ne crée que
l'accès REST.

À lancer une fois par cluster de la topologie (source, pivot, PROD, DR).

Les commandes qu'il exécute sont ci-dessous, si vous préférez les passer à
la main depuis la CLI admin du cluster. Le rôle n'autorise que les commandes
/ endpoints réellement utilisés par l'outil ; tout le reste est refusé.

**Rôle REST (utilisé par le transport `rest` par défaut) :**

```
security login rest-role create -role mutrepli_rest -api /api/storage/volumes -access all
security login rest-role create -role mutrepli_rest -api /api/storage/qtrees -access all
security login rest-role create -role mutrepli_rest -api /api/storage/aggregates -access readonly
security login rest-role create -role mutrepli_rest -api /api/storage/quota/rules -access readonly
security login rest-role create -role mutrepli_rest -api /api/snapmirror/relationships -access all
security login rest-role create -role mutrepli_rest -api /api/protocols/cifs/shares -access all
security login rest-role create -role mutrepli_rest -api /api/protocols/nfs/export-policies -access all
security login rest-role create -role mutrepli_rest -api /api/protocols/file-security/permissions -access all
security login rest-role create -role mutrepli_rest -api /api/svm/svms -access readonly
security login rest-role create -role mutrepli_rest -api /api/cluster/jobs -access readonly
security login rest-role create -role mutrepli_rest -api /api/cluster/schedules -access readonly
security login rest-role create -role mutrepli_rest -api /api/snapmirror/policies -access readonly
security login rest-role create -role mutrepli_rest -api /api/svm/peers -access readonly
```

> **Piège RBAC** : ONTAP résout les objets référencés dans une requête
> (le schedule cron, la policy SnapMirror, le SVM peer…) **avec les
> permissions de l'appelant**. Si le rôle ne peut pas lire l'endpoint
> d'un objet référencé, ONTAP répond `... not found` alors que l'objet
> existe — d'où les trois accès lecture seule ci-dessus. Symptôme
> typique : `Schedule "hourly" not found in the Administrative SVM or
> the SVM for the relationship` alors que `job schedule cron show`
> l'affiche en tant qu'admin.

**Rôle CLI (utilisé par le transport `ssh` de secours) :**

```
security login role create -role mutrepli_cli -cmddirname "volume" -access all
security login role create -role mutrepli_cli -cmddirname "snapmirror" -access all
security login role create -role mutrepli_cli -cmddirname "storage aggregate" -access readonly
security login role create -role mutrepli_cli -cmddirname "volume quota policy" -access readonly
security login role create -role mutrepli_cli -cmddirname "vserver cifs share" -access all
security login role create -role mutrepli_cli -cmddirname "vserver export-policy" -access all
security login role create -role mutrepli_cli -cmddirname "vserver security file-directory" -access all
```

**Utilisateur `mutrepli` avec authentification par mot de passe :**

```
# Accès API REST (basic auth — c'est ce compte que creds.json référence) :
security login create -user-or-group-name mutrepli -application http \
    -authentication-method password -role mutrepli_rest

# Accès SSH (mot de passe interactif) :
security login create -user-or-group-name mutrepli -application ssh \
    -authentication-method password -role mutrepli_cli
```

Le mot de passe est demandé à la création. Référencer ensuite le compte
dans `creds.json` (section 2.3) : `"username": "mutrepli"`.

> Note pour le transport SSH : l'outil se connecte en non-interactif
> (`BatchMode=yes`), ce qui nécessite une clé en plus de la méthode
> mot de passe :
>
> ```
> security login create -user-or-group-name mutrepli -application ssh \
>     -authentication-method publickey -role mutrepli_cli
> security login publickey create -username mutrepli -publickey "ssh-ed25519 AAAA... migration@serveur"
> ```

Vérification : `security login show -user-or-group-name mutrepli` puis,
depuis le serveur,
`curl -sk -u mutrepli https://<cluster>/api/storage/volumes?max_records=1`.

### 2.6 Authentification : token global et tokens délégués

L'API et la CLI sont protégées par des tokens. Le contrôle s'active dès
qu'un coffre de tokens existe sur le serveur.

**Installation — une fois.** Le super admin choisit un token global ; il est
demandé interactivement et n'est écrit nulle part :

```bash
python3 netapp_cascade_migration.py --action tokens-init
#   New global token (super admin): ********
#   Confirm global token: ********
#   Token store created: netapp_tokens.enc
```

Ce token est la clé qui protège le coffre (PBKDF2-HMAC-SHA256, 600 000
itérations, Fernet AES-128-CBC + HMAC) **et** le token super-admin de l'API.
Il est irrécupérable : à conserver précieusement.

**Déléguer par qtree.** Le super admin fournit un CSV listant, pour chaque
qtree, le token qui le possède et les actions autorisées. `NEW_TOKEN`
demande à l'API d'en générer un :

```csv
qtree,token,actions,label
q_fin,NEW_TOKEN,"test,clone,acl",Finance
q_hr,NEW_TOKEN,test,RH
q_ops,mtk_existant...,"test,acl",Ops
```

```bash
python3 netapp_cascade_migration.py --action tokens-import \
    --scope-csv scopes.csv --scope-out tokens_emis.csv
```

`tokens_emis.csv` (mode 0600) est le **seul** endroit où un token généré
apparaît en clair — remets chacun à son propriétaire, puis supprime le
fichier. Le coffre ne conserve que des empreintes salées.

Les actions délégables sont celles au niveau qtree (`test`, `clone`, `acl`,
`cleanup`) plus la lecture (`status`, `preflight`, `read`). `create`,
`resume`, `retry`, `refresh` et l'administration des tokens restent au super
admin.

**Changer un scope** ensuite, sans réémettre le token :

```bash
python3 netapp_cascade_migration.py --action tokens-list
python3 netapp_cascade_migration.py --action tokens-set-scope \
    --token-id tok_0be346ea4f5b --grant-qtrees "q_hr,q_ops" --grant-actions "test,acl"
python3 netapp_cascade_migration.py --action tokens-revoke --token-id tok_xxx
```

**Démarrer l'API.** Le coffre n'est déchiffré qu'en mémoire : chaque
démarrage réclame le token global. Deux façons de le fournir, parce qu'un
terminal et un service ne sont pas la même situation.

*Dans un terminal* (avec `tmux`/`screen` pour survivre à la session SSH) —
l'API demande le token avant d'ouvrir le port :

```bash
python3 -m netapp_migration.interfaces.api.serve --host 127.0.0.1 --port 8000
#   Global token (super admin): ********
#   Token store unlocked: 3 delegated token(s).
```

*Sous systemd* — un service n'a aucun terminal sur lequel un administrateur
connecté en SSH pourrait taper. L'API démarre donc **verrouillée** : le port
est ouvert immédiatement et tous les endpoints répondent `503` jusqu'à ce
qu'un super admin la déverrouille depuis sa propre session :

```bash
systemctl start netapp-migration-api
python3 netapp_cascade_migration.py --action api-unlock \
    --unlock-socket /opt/netapp-migration/etc/unlock.sock
#   Global token (super admin): ********
#   API unlocked (unlocked, 3 delegated token(s)).
```

Le token transite par une socket unix en mode `0600`, propriété du compte de
service : jamais sur disque, jamais dans `argv`, jamais dans l'environnement.
L'état se vérifie à tout moment, sans authentification :

```bash
curl -s http://127.0.0.1:8000/api/v1/health
# {"status":"ok", ..., "auth":{"initialised":true,"unlocked":true}}
```

Si le service s'arrête, l'API revient **verrouillée** et répond `503` jusqu'à
ce qu'un super admin redonne le token global. C'est volontaire : un
redémarrage non supervisé ne rouvre jamais l'API silencieusement.

Les appels portent le token en en-tête :

```bash
curl -H "Authorization: Bearer $TOKEN" $BASE/auth/whoami
```

> La CLI est pilotée par le super admin : les tokens délégués sont destinés
> à l'API REST et ne peuvent pas ouvrir le coffre local.


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

#### Après le clone : l'inventaire de la destination

Une fois les clones créés, `check-status` cesse d'être une barre de
progression de réplication et devient la réponse à *qu'est-ce qui existe
maintenant, et comment le retrouver ?* Il rapporte chaque objet créé par la
migration sur **PROD et DR**, chacun avec l'identifiant sous lequel ONTAP le
connaît, à côté des valeurs qui décident du comportement :

| Objet | Rapporté |
|---|---|
| Volume | UUID, état, type, aggregate, junction path, taille, parent dont il est le clone, état des quotas |
| Qtrees | id, chemin, export policy en vigueur, security style |
| Export policies | id, et chaque règle — clients, RO/RW, protocoles |
| Quotas | UUID de la règle, type, cible, **limites d'espace hard et soft, limites de fichiers hard et soft** |
| Quota policy | la policy active du SVM, par côté |
| SnapMirror | UUID de la relation, chemin source, état, état du transfert, policy, schedule, dernier transfert |

```
+-------------+--------------------------------------+--------+------+-----------+------------+
| PROD volume | UUID                                 | State  | Type | Aggregate | Junction   |
+-------------+--------------------------------------+--------+------+-----------+------------+
| vol_q_fin   | 8f3a1c2e-9b44-11ee-b9d1-005056b0aa01 | online | rw   | aggr_prd2 | /vol_q_fin |
+-------------+--------------------------------------+--------+------+-----------+------------+
```

Les deux côtés sont affichés, et **DR est lu sur DR** — pas déduit de ce
qu'on a demandé à PROD. C'est ce qui rend le rapport utile : il montre ce que
la réplication a réellement livré.

Les mêmes données via l'API, sous `destination` :

```bash
curl -s $BASE/migrations/$JOB/status | jq '.result.destination'
```

C'est un **rapport** : strictement en lecture seule, et un objet illisible
coûte une ligne, jamais l'ensemble du tableau. Ce qui a échoué est listé à la
fin avec la raison — le plus souvent un droit RBAC manquant :

```
| WARN | 2 object(s) could not be read — the rest of the inventory is complete:
|          PROD CMOPARPA4SFS100/svm_dest:vol_q_fin quota rules: not authorized
```

> Le **nom** de la quota policy n'est pas exposé par l'API REST d'ONTAP — les
> règles sont appliquées à la policy active du SVM sans la nommer — donc le
> transport REST rapporte `not exposed by the REST API`. Le transport SSH le
> lit via `vserver show -fields quota-policy`.

### 3.2 retry — reprendre un create échoué

```bash
python3 netapp_cascade_migration.py --action retry --job-id <ID>
```

Reprend au dernier checkpoint (`space_checked`, `volumes_created`, ...) ;
les créations déjà faites sont ignorées (idempotence).

### 3.3 test — environnement de test complet (aucun espace consommé)

```bash
python3 netapp_cascade_migration.py --action test --job-id <ID> \
    --qtrees q_fin,q_hr --volume-map volumes.csv --test-validity-days 7
```

`--volume-map` est un CSV qui nomme, pour chaque qtree, le volume à créer et
— facultativement — le nom que prend le qtree lui-même dans ce volume. C'est
le client qui choisit les deux, rien n'est généré :

```csv
qtree,volume,new_qtree
q_fin,vol_finance_prod,finance
q_hr,vol_rh_prod,
```

La troisième colonne est facultative, en en-tête comme ligne par ligne :
laissée vide, le qtree garde le nom qu'il a sur la source. Un fichier à deux
colonnes écrit pour une version antérieure fonctionne toujours.

Le renommage a lieu sur le **clone PROD uniquement** — le clone DR est une
destination SnapMirror, donc en lecture seule — et **avant** l'établissement
du miroir entre clones, pour que le premier resync porte le nouveau nom
jusqu'à la DR. Un nom déjà utilisé par le volume source est refusé au pré-vol
(`QTREE_NAME_TAKEN`) avant toute création : un clone hérite de tous les
qtrees de son parent.

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
python3 netapp_cascade_migration.py --action clone --job-id <ID> \
    --qtrees q_fin,q_hr --volume-map volumes.csv
```

Une promotion réutilise les noms enregistrés par le test : `--volume-map`
peut alors être omis. Trois modes :

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
  propagation cascade → FlexClones sur PROD et DR → **élagage** → SnapMirror
  entre clones + resync → sélection automatique du meilleur aggregate →
  `volume move` → sortie immédiate.

#### Le miroir des clones : clone PROD -> clone DR

Une migration comporte deux sortes de relations SnapMirror, configurées
différemment. La **cascade** achemine les données une fois ; le **miroir des
clones** est la relation avec laquelle le client vit ensuite, et c'est donc
son schedule qui décide du retard toléré sur DR.

| | Cascade (source → pivot → PROD/DR) | Miroir des clones (clone PROD → clone DR) |
|---|---|---|
| Type | `XDP` | `XDP` |
| Policy | `MirrorAllSnapshots` | `MFA_MirrorAllSnapshots` |
| Schedule | `hourly` | `pg-15-minutely` |

Les deux jeux sont définis dans `netapp_migration/core/replication.py` — le
seul endroit que lisent le moteur, le pré-vol et les transports. Rien ne
retombe sur une valeur par défaut du transport : un pré-vol qui vérifierait
un autre nom que celui envoyé par le moteur serait pire que pas de contrôle
du tout.

```
>> [5/7]  SnapMirror (clone PROD -> clone DR) + resync
         type=XDP policy=MFA_MirrorAllSnapshots schedule=pg-15-minutely
```

Le pré-vol vérifie que la policy **et** le schedule sont **visibles par le
compte API** sur le cluster DR avant de lancer le clone, car ONTAP résout un
objet référencé avec les permissions de l'appelant et signale `not found`
celui que le rôle ne peut pas lire :

```
| FAIL | Clone mirror SnapMirror policy visible | policy 'MFA_MirrorAllSnapshots' not found |
| FAIL | Clone mirror schedule visible          | schedule 'pg-15-minutely' not found       |
```

> **Sur le transport REST il n'existe pas de champ `type`** : la nature de la
> relation découle du type de la *policy* — une policy async donne du XDP. Le
> type n'est donc pas envoyé mais **vérifié après création**, et le run échoue
> bruyamment si ONTAP a produit autre chose que du XDP. Le transport SSH
> passe `-type XDP` directement.

#### Élagage : un volume, les données d'un seul client

Un FlexClone copie le volume parent **en entier** : le volume créé pour
`q_fin` contient donc au départ `q_hr`, `q_ops` et tous les autres qtrees de
la source — les données d'autres clients dans le volume de ce client, et N
copies complètes pour N qtrees.

`clone` et `test` suppriment donc, dans chaque clone, tout qtree pour lequel
il n'a pas été créé. Actif par défaut ; `--no-prune` (API : `"prune": false`)
conserve tout, et le pré-vol émet alors un avertissement (`PRUNE_DISABLED`).

L'opération a lieu juste après la création des clones — **avant** le miroir
et **avant** le move — et c'est ce qui la rend efficace :

* le clone DR reçoit les suppressions dès son premier resync : il ne porte
  donc jamais le surplus non plus ;
* le `volume move` ne déplace que ce qui reste.

Garde-fous : **PROD uniquement** (le clone DR est en lecture seule), le
**volume source n'est jamais touché**, le qtree pour lequel le volume a été
créé n'est jamais supprimé, et un plan qui viderait un volume interrompt le
run. Le pré-vol liste les suppressions avant qu'elles n'arrivent :

```
| WARN | 'vol_fin' : qtrees hérités qui seront SUPPRIMÉS | keeps 'q_fin', deletes 2: q_hr, q_ops |
```

#### Export policies : les clients suivent leurs données

Une export policy est un **objet du SVM**. Elle ne voyage pas avec les
données. Un FlexClone hérite du *nom* de la policy de son volume parent, et
ce nom ne veut rien dire sur le SVM de destination — le qtree migré pointe
alors vers une policy absente, ou pire, appartenant à quelqu'un d'autre. Dans
les deux cas l'accès NFS du client casse dès que la source est coupée.

`clone` et `test` lisent donc, pour chaque qtree **à la source**, l'export
policy qui lui est appliquée et les **client matches** de ses règles, puis
créent `ep_<qtree de destination>` sur **PROD et DR**.

**Seul le client vient de la source.** Tous les autres paramètres sont
forcés, à l'identique sur chaque règle de chaque qtree migré — la destination
est un environnement neuf et uniforme, pas une copie de ce que chaque policy
source a accumulé au fil des années :

| Paramètre | Valeur |
|---|---|
| Access protocol | `nfs4` |
| RO / RW access rule | `any` / `any` |
| Superuser security types | `any` |
| Anonymous user mapping | `none` |
| Honor SetUID bits in SETATTR | `true` |
| Allow creation of devices | `true` |
| NTFS Unix security options | `fail` |
| Change ownership mode | `restricted` |

Ils sont définis à un seul endroit, `DESTINATION_RULE` dans
`netapp_migration/core/exports.py`. Y changer une valeur la change pour tous
les qtrees migrés.

**Une règle, un client.** Une règle source nommant trois machines devient
trois règles à la destination : on peut donc révoquer une machine plus tard
sans toucher aux deux autres.

```
source  rule 2 : 10.0.0.1, 10.0.0.2, 10.20.0.0/16, 192.168.1.222
                     |          |          |             |
dest    rule 1 : 10.0.0.1       |       (ignoré)         |
        rule 2 : ───────── 10.0.0.2                      |
        rule 3 : ─────────────────────────────── 192.168.1.222
```

**Les réseaux ne sont pas repris.** Un sous-réseau désigne ce qui vit dans
cette plage côté destination, pas nécessairement les mêmes machines. Il est
ignoré, jamais deviné et **jamais bloquant** — le reste de la policy vaut
toujours la peine :

```
'q_fin'  1 network(s) NOT carried over to 'ep_finance': 10.20.0.0/16 (source rule 2)
'q_fin'  add them by hand if those ranges really should reach the destination.
```

Un hôte écrit en préfixe complet (`10.0.0.9/32`, `2001:db8::1/128`) ne couvre
qu'une seule adresse : il compte comme un hôte, pas comme un réseau. Les noms
d'hôtes, domaines (`.example.com`) et netgroups (`@admins`) sont repris à
raison d'une règle chacun, comme tout autre client unique. Un même client
nommé par deux règles source ne donne qu'une règle à la destination, ONTAP
refusant un match dupliqué.

```
| Qtree | Source policy | Policy on PROD + DR | Rules  | Clients carried over          | Networks NOT carried |
| q_fin | ep_prod_nfs   | ep_finance          | 1 -> 3 | 10.0.0.1, 10.0.0.2, 192.168…  | 10.20.0.0/16         |
```

La policy porte le nom du qtree **tel qu'il existe à la destination** : quand
le client a renommé `q_fin` en `finance` via la carte des qtrees, la policy
s'appelle `ep_finance`, car c'est ce nom qu'un exploitant voit sur le cluster.

Elle est **appliquée sur PROD uniquement**, avant que le miroir des clones
n'existe, afin que le premier resync porte l'affectation jusqu'à DR. Sur DR
la policy est seulement *créée* — le clone DR est une destination SnapMirror
en lecture seule et ne peut pas y être modifié, mais l'objet doit exister sous
le même nom, sinon l'affectation répliquée ne résout plus rien le jour où DR
est activé.

Une policy existante portant ce nom est **réutilisée, jamais réécrite** :
elle peut appartenir à quelque chose que ce job ignore. Le pré-vol le
signale (`EXPORT_POLICY_NAME_TAKEN`) et annonce tout le plan avant exécution
— calculé par le code même qu'exécute le moteur, les deux ne peuvent donc pas
diverger :

```
| PASS | 'q_fin': policy 'ep_finance' on PROD and DR  | ep_prod_nfs -> ep_finance: 1 source rule(s) -> 3 rule(s), one per client |
| PASS | 'q_fin': parameters forced on every rule     | proto=nfs4 ro=any rw=any superuser=any anon=none suid=true dev=true ntfs-unix=fail chown=restricted |
| PASS | 'q_fin': at least one client is carried over | clients: 10.0.0.1, 10.0.0.2, 192.168.1.222 |
| WARN | 'q_fin': no client is a network              | NOT carried over: 10.20.0.0/16 (source rule 2) |
```

Se retrouver **sans aucun client** n'est pas une erreur — certains qtrees
sont réellement CIFS uniquement, et une policy composée uniquement de réseaux
est un cas réel — mais ce n'est jamais silencieux, puisque la destination
refuserait alors tous les clients NFS :

```
| WARN | 'q_ops': at least one client is carried over | nothing to carry from 'default' — 'ep_q_ops' will deny every NFS client |
```

### 3.6 cleanup — coupure d'accès source

La dernière action, et la seule que les utilisateurs voient immédiatement :
elle fait disparaître les partages source. Comme `clone`, elle accepte **un,
plusieurs ou tous** les qtrees.

```bash
python3 netapp_cascade_migration.py --action cleanup --job-id <ID> \
    --source-cluster ... --pivot-cluster ... --dest-cluster ... \
    --volume vol_prod_01 --qtrees q_fin,q_hr        # ou --qtrees all
```

`--qtree` (singulier) reste accepté comme alias.

Pour **chaque** qtree, sur la **source uniquement** :

1. **Export policy** — le qtree est basculé sur la policy no-access
   (`--noaccess-policy`, défaut `ep_noaccess`). Si elle n'existe pas, elle
   est **créée sans aucune règle** : c'est ainsi qu'ONTAP refuse tous les
   clients.
2. **Partages CIFS** — tout partage pointant vers le qtree est supprimé, afin
   que les clients Windows cessent d'atteindre l'ancienne copie. Le pré-vol
   les nomme d'abord ; le run ne devine jamais.
3. **Renommage** — le qtree est renommé pour dire où sont parties ses
   données :

   ```
   q_finance  ->  q_finance_MIG_2da725__vol_fin_prod__finance
                            └ job    └ nouveau volume └ nouveau qtree
   ```

   Le qtree de destination est omis quand le nom n'a pas changé. ONTAP limite
   les noms de qtree à 64 caractères : le nom d'origine est tronqué en
   premier, le marqueur et la référence du job en dernier — ce sont eux qui
   rendent le qtree identifiable.

**Aucune donnée n'est supprimée.** Le qtree reste en place, inaccessible et
marqué : un retour arrière est toujours possible. Le supprimer définitivement
est une décision distincte et manuelle.

Le run est enregistré sur le job (`cleaned_up`, `cleaned_up_at`), et le
pré-vol s'en sert pour refuser un second passage.

#### Ce que le pré-vol refuse

C'est l'action la plus sensible de l'outil, c'est donc la plus contrôlée.
Chacun de ces points interrompt le run avant le moindre appel :

| Code | Refuse quand |
|---|---|
| `CLEANUP_QTREE_MISSING` | aucun qtree fourni (une valeur vide correspondrait à tous les partages du SVM) |
| `CLEANUP_QTREE_SEPARATOR` | un chemin a été passé à la place d'un nom de qtree |
| `CLEANUP_QTREE_DUPLICATE` | le même qtree apparaît deux fois |
| `CLEANUP_MIGRATION_INCOMPLETE` | le job n'est pas `completed` |
| `CLEANUP_QTREE_NOT_FOUND` | le qtree n'est pas sur le volume source |
| `CLEANUP_ALREADY_DONE` | le nom porte déjà `_MIG_` |
| `CLEANUP_QTREE_NOT_MIGRATED` | le qtree n'est pas dans le `volume_map` du job — couper une source sans copie laisserait le client sans rien |
| `CLEANUP_TARGET_MISSING` | le volume migré est absent sur PROD ou DR |
| `CLEANUP_NEW_NAME_TAKEN` | le nouveau nom existe déjà sur le volume |

Et ceux-ci avertissent sans bloquer : `CLEANUP_CLONES_NOT_PROMOTED` (pas de
`clone_promoted_at`), `CLEANUP_NO_JOB_CONTEXT` (pas de `--job-id`, donc rien
n'est vérifiable), `EXPORT_POLICY_ABSENT` (elle sera créée),
`CLEANUP_SHARES_PREVIEW` — la liste exacte des partages qui vont
disparaître :

```
| WARN | 'q_fin' : partages CIFS qui seront SUPPRIMÉS | fin_share, fin_archive |
```

Un seul qtree en faute refuse tout le run : les contrôles se lisent d'un
bloc, et un ensemble de clients coupé à moitié est pire que rien.

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

**Première utilisation ? Suivez [docs/api-guide.fr.md](docs/api-guide.fr.md)**
— une migration complète pas à pas dans Swagger UI, avec une capture d'écran
réelle à chaque étape.

> **Important** : lancer **un seul worker** (pas de `--workers N`) — le
> registre des actions en cours est en mémoire de processus.

### 4.2 Endpoints

| Méthode | Endpoint | Action | Réponse |
|---|---|---|---|
| `POST` | `/api/v1/migrations` | create | `202` + job_id (fond) |
| `GET`  | `/api/v1/migrations` | liste des jobs | `200` |
| `GET`  | `/api/v1/migrations/{id}` | fichier de job + dernière action (logs) | `200` |
| `GET`  | `/api/v1/migrations/{id}/status` | état réplication live + inventaire de la destination (interroge ONTAP) | `200` |
| `POST` | `/api/v1/migrations/{id}/resume` | fan-out PROD + DR | `200` / `409` |
| `POST` | `/api/v1/migrations/{id}/retry` | reprise après échec | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/test` | env de test complet (clones + miroir, limité dans le temps) | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/clone` | clones définitifs (promotion du test, ou `fresh` / flux complet) | `202` (fond) |
| `POST` | `/api/v1/migrations/{id}/acl` | forçage DACL groupes AD sur un path | `200` |
| `POST` | `/api/v1/migrations/{id}/cleanup` | coupure accès source pour un, plusieurs ou tous les qtrees migrés | `200` |
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

### 4.4 Service systemd

`install.sh` et `install-standalone.sh` écrivent cette unité pour vous. Deux
points ne sont pas négociables, et expliquent pourquoi elle ne lance pas
simplement `uvicorn` :

* elle passe par `…api.serve`, qui possède le coffre de tokens — lancer
  `uvicorn app:app` directement laisse le coffre verrouillé et tous les
  endpoints en `503` pour toujours ;
* elle démarre **verrouillée** et ne tente jamais de poser une question. Un
  service n'a pas de terminal : lire le token sur `/dev/console` bloque
  invisiblement et le port n'est jamais ouvert.

```ini
# /etc/systemd/system/netapp-migration-api.service
[Unit]
Description=NetApp Cascade Migration API
After=network-online.target
Wants=network-online.target

[Service]
# notify: active only once the port is really bound — never a process that
# looks healthy while it is stuck starting up.
Type=notify
NotifyAccess=main
TimeoutStartSec=60
User=netappmig
Group=netappmig
WorkingDirectory=/opt/netapp-migration
Environment=NETAPP_MIGRATION_CONFIG=/opt/netapp-migration/etc/creds.json
Environment=NETAPP_MIGRATION_JOB_DIR=/opt/netapp-migration/jobs
Environment=NETAPP_TOKEN_STORE=/opt/netapp-migration/etc/netapp_tokens.enc
Environment=NETAPP_UNLOCK_SOCKET=/opt/netapp-migration/etc/unlock.sock

ExecStart=/opt/netapp-migration/.venv/bin/python \
    -m netapp_migration.interfaces.api.serve \
    --host 127.0.0.1 --port 8000 \
    --start-locked --unlock-socket /opt/netapp-migration/etc/unlock.sock
StandardInput=null

# A restart must be a deliberate act, followed by a deliberate unlock.
Restart=no

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/netapp-migration/jobs /opt/netapp-migration/logs /opt/netapp-migration/etc
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl start netapp-migration-api          # démarre verrouillée
python3 netapp_cascade_migration.py --action api-unlock \
    --unlock-socket /opt/netapp-migration/etc/unlock.sock
```

L'unité n'est volontairement **pas** activée au démarrage : un lancement non
supervisé ne produirait qu'une API verrouillée que personne n'a demandée.

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

#### Ce qu'un job dit de ce qui s'est passé

`status` est, et n'a jamais été, que le **point de reprise de la cascade
create** (`started` → … → `completed`). Aucune autre action ne l'écrit : il
ne dit donc rien de la réussite d'un `clone` ultérieur — un job dont le clone
a échoué affiche toujours `completed`, parce que la réplication, elle, est
bien terminée.

Chaque action enregistre donc son propre résultat à côté :

```json
"last_action": {
  "action": "clone",
  "state": "failed",
  "started_at": "2026-08-20T15:21:36",
  "ended_at": "2026-08-20T15:21:41",
  "error": "OntapError: [PRD] DELETE /storage/qtrees/... -> HTTP 400: ..."
},
"history": [ ... les 20 derniers ... ]
```

| `state` | Signification |
|---|---|
| `running` | démarrée, non terminée — un processus tué en route reste ici |
| `success` | terminée, rien n'a été levé |
| `failed` | échec ; `error` porte ce qu'a répondu le cluster |
| `refused` | le pré-vol a dit non — **rien n'a été modifié** |
| `needs_confirmation` | en attente d'un accord explicite |
| `unknown` | fichier de job écrit avant l'existence de ce champ |

`check-status` affiche les deux, et ne s'enregistre volontairement **pas**
lui-même : regarder un job ne doit pas effacer ce qu'on vient y chercher.

```
  Created: 2026-08-20T15:21:36  |  Cascade: completed
  Last action  : clone -> FAILED   (2026-08-20T15:21:41)
                 OntapError: [PRD] DELETE /storage/qtrees/... -> HTTP 400: ...
```

L'API renvoie la même chose sous `outcome` sur `GET /migrations/{id}` et sur
chaque ligne de `GET /migrations`. C'est lu dans le **fichier** de job : cela
survit donc à un redémarrage de l'API, contrairement à `last_run`, qui est
en mémoire.

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
* **`Schedule "..." not found` / `Policy "..." not found`** alors que
  l'objet existe sur le cluster : le rôle de l'utilisateur API ne peut
  pas LIRE l'endpoint de l'objet référencé, ONTAP le déclare donc
  introuvable. Accorder les accès lecture seule listés en section 2.5
  (`/api/cluster/schedules`, `/api/snapmirror/policies`,
  `/api/svm/peers`). Vérification rapide avec le compte de service :
  `curl -sk -u mutrepli "https://<cluster>/api/cluster/schedules?name=hourly"`.
* **Le service est « active » mais rien n'écoute sur 8000** (`ss -ltnp` ne
  montre aucun processus, `/docs` injoignable) : l'unité est bloquée avant
  l'ouverture du port. Voir `systemctl status` et le journal. Historiquement
  il s'agissait d'une unité qui lisait le token sur `/dev/console` — une
  invite que personne ne pouvait voir ni renseigner en SSH. L'unité actuelle
  démarre verrouillée et ne pose aucune question ; si vous avez encore
  l'ancienne, réinstallez ou remplacez-la par celle de la section 4.4.
* **`500 Internal Server Error` sur une action, avec `JSONDecodeError` en bas
  de la trace** : `creds.json` n'est pas du JSON valide. Le message donne la
  ligne et la colonne — le plus souvent un `"` ou un `\` non échappé dans le
  mot de passe. En JSON, un antislash s'écrit `\\` et un guillemet `\"` :

  ```json
  {"defaults": {"username": "mutrepli", "password": "pa\"ss\\word"}}
  ```

  Vérifiez le fichier avant de redémarrer :
  `python3 -m json.tool /chemin/creds.json`. Les versions récentes répondent
  `503 {"error":"configuration"}` en nommant le fichier, la ligne et la
  correction, au lieu d'un 500.
* **`SSLCertVerificationError` avec le pip du venv, alors que le pip système
  fonctionne** : les deux ne font pas confiance aux mêmes certificats. Un pip
  packagé par la distribution est « dé-vendorisé » — son `certifi` pointe sur
  le magasin de confiance **système**, qui contient votre CA d'entreprise. Le
  pip d'un venv embarque son propre `pip/_vendor/certifi/cacert.pem` :
  racines Mozilla uniquement, pas de CA d'entreprise. À vérifier vous-même :

  ```bash
  python3            -c 'from pip._vendor import certifi; print(certifi.where())'
  .venv/bin/python   -c 'from pip._vendor import certifi; print(certifi.where())'
  ```

  Pointez pip sur le magasin système — globalement, pour que tous les venvs
  de la machine en profitent :

  ```bash
  sudo tee /etc/pip.conf >/dev/null <<'EOF'
  [global]
  cert = /etc/pki/tls/certs/ca-bundle.crt
  EOF
  ```

  (Debian/Ubuntu : `/etc/ssl/certs/ca-certificates.crt`.) Pour un seul venv,
  `.venv/bin/pip config set --site global.cert <bundle>` ; pour une seule
  commande, `PIP_CERT=<bundle> pip install …` ou `pip install --cert
  <bundle>`. `install-standalone.sh` accepte `--cert PATH`, et réessaie tout
  seul avec le magasin système détecté quand il rencontre cette erreur.

  À préférer à `--trusted-host`, qui ne corrige pas le problème de confiance
  — il désactive la vérification.
* **`status=203/EXEC` — « Failed to execute command: Permission denied »** :
  systemd n'a même pas pu lancer `<prefix>/.venv/bin/python`. Identifiez le
  composant fautif du chemin :

  ```bash
  namei -l /opt/netapp-migration/.venv/bin/python   # le mode de chaque composant
  sudo -u netappmig /opt/netapp-migration/.venv/bin/python -V
  findmnt -no OPTIONS "$(df -P /opt/netapp-migration | awk 'NR==2{print $6}')"
  getenforce 2>/dev/null
  ```

  Quatre causes habituelles :

  1. **Le venv pointe vers un interpréteur privé.** Un venv n'est qu'un jeu
     de liens vers le Python qui l'a créé. Si `namei` montre une chaîne qui
     finit sur quelque chose comme `/root/bin/.../python3.12` et que `/root`
     est en `dr-xr-x---`, le compte de service ne l'atteindra jamais. Le
     `PATH` de root place volontiers le Python embarqué d'un agent avant
     celui du système. Reconstruire sur un interpréteur système — relancer
     l'installeur le fait pour vous, ou forcez-le avec
     `--python /usr/bin/python3.12`.
  2. Le répertoire d'installation n'est pas traversable par le compte de
     service (`chmod 755 /opt/netapp-migration`) ; une installation sous
     `umask 077` produisait exactement cela.
  3. Le système de fichiers est monté `noexec` — installer ailleurs avec
     `--prefix`.
  4. SELinux a mal étiqueté l'arborescence —
     `restorecon -R /opt/netapp-migration`.

  Relancer l'installeur détecte et signale les quatre, et répare les deux
  premières tout seul.
* **Tout répond `503 {"error":"locked"}`** : l'API tourne mais son coffre
  n'a pas été déverrouillé depuis le dernier démarrage. Déverrouillez-la :
  `--action api-unlock --unlock-socket <prefix>/etc/unlock.sock`.
  `curl /api/v1/health` affiche `auth.unlocked` sans authentification.
* **`no unlock socket at …`** : l'API ne tourne pas, ou elle a été lancée au
  premier plan (où elle demande le token) et non avec `--start-locked`.
* **`not allowed to open …/unlock.sock`** : la socket est en `0600` et
  appartient au compte de service — lancez le déverrouillage avec ce compte
  ou en root (`sudo -u netappmig …`).
* **`/docs` marche en local mais pas depuis un poste de travail** : l'API
  écoute sur `127.0.0.1` (valeur par défaut). Faites un tunnel
  `ssh -L 8000:127.0.0.1:8000 user@serveur`, ou démarrez-la avec
  `--host 0.0.0.0` une fois le port correctement filtré.
* **Page `/docs` vide** : les assets Swagger UI sont servis en local par
  l'API (`/static/`) précisément pour les serveurs sans Internet — si la
  page est vide, vérifier que le code est à jour (`git pull`) et relancer
  uvicorn.
* **Trace complète** : le fichier `migration_<action>_<date>.log` contient
  chaque appel REST/SSH en DEBUG.

# Utiliser l'API depuis Swagger UI — pas à pas

*English version: [api-guide.md](api-guide.md)*

Ce guide suit une migration complète, d'une API vide jusqu'à la promotion des
clones, entièrement depuis le navigateur. Chaque capture ci-dessous est une
copie d'écran réelle de l'API réelle — aucune n'est une maquette, et elles
sont régénérées depuis le code par `tools/capture_swagger_guide.py`.

> **À propos des tokens et des noms de clusters visibles.** Ils proviennent
> d'un coffre de démonstration jetable, créé pendant la capture et détruit
> juste après. `SuperAdmin-Demo-Token`, `DEMO-TOKEN-finance-only`,
> `clu-prod-01`… sont fictifs et ne sont valides nulle part. La capture
> tourne avec le **transport dry-run** : aucun cluster ONTAP n'est contacté
> pour produire ce guide.

**Sommaire**

1. [Accéder à Swagger UI](#1-accéder-à-swagger-ui)
2. [L'API démarre verrouillée](#2-lapi-démarre-verrouillée)
3. [La page d'accueil](#3-la-page-daccueil)
4. [S'authentifier](#4-sauthentifier)
5. [Déléguer des tokens aux clients](#5-déléguer-des-tokens-aux-clients)
6. [Vérifier la faisabilité avant d'agir](#6-vérifier-la-faisabilité-avant-dagir)
7. [Créer la cascade](#7-créer-la-cascade)
8. [Suivre une migration en cours](#8-suivre-une-migration-en-cours)
9. [Actions par qtree : test, clone, acl](#9-actions-par-qtree--test-clone-acl)
10. [Ce qu'un token à scope limité peut faire](#10-ce-quun-token-à-scope-limité-peut-faire)
11. [Lire les réponses](#11-lire-les-réponses)

---

## 1. Accéder à Swagger UI

Ouvrez `http://<serveur-api>:8000/docs`.

Si rien ne répond, l'API écoute sur `127.0.0.1` (valeur par défaut à
l'installation), c'est-à-dire *joignable depuis le serveur d'API uniquement*.
Soit vous faites un tunnel depuis votre poste :

```bash
ssh -L 8000:127.0.0.1:8000 <user>@<serveur-api>
# puis ouvrez http://127.0.0.1:8000/docs en local
```

soit vous démarrez l'API avec `--host 0.0.0.0` une fois le port correctement
filtré.

Les assets Swagger sont servis par l'API elle-même (`/static/`) : la page
fonctionne sur un serveur sans accès Internet.

---

## 2. L'API démarre verrouillée

Le coffre de tokens n'est déchiffré qu'en mémoire. Après chaque démarrage —
`systemctl start` compris — l'API est **verrouillée** : le port est ouvert et
Swagger UI s'affiche, mais tous les endpoints répondent `503`.

![API verrouillée répondant 503](images/01-locked-503.png)

Le corps de la réponse dit exactement quoi faire :

```json
{
  "detail": {
    "error": "locked",
    "message": "the API is locked: its token store has not been unlocked since the last restart",
    "hint": "a super admin must supply the global token: python3 netapp_cascade_migration.py --action api-unlock"
  }
}
```

Un super admin déverrouille depuis un shell **sur le serveur d'API** — c'est
la seule étape qui ne peut pas se faire depuis le navigateur, volontairement :
le token global ne transite jamais par HTTP.

```bash
python3 netapp_cascade_migration.py --action api-unlock \
    --unlock-socket /opt/netapp-migration/etc/unlock.sock
#   Global token (super admin): ********
#   API unlocked (unlocked, 3 delegated token(s)).
```

Rechargez la page ensuite.

> Lancée au premier plan (`python3 -m netapp_migration.interfaces.api.serve`),
> l'API demande le token avant d'ouvrir le port ; il n'y a alors rien à
> déverrouiller.

---

## 3. La page d'accueil

Une fois déverrouillée, `/docs` liste tout ce que l'API sait faire.

![Page d'accueil de Swagger UI](images/02-overview.png)

À lire comme quatre familles :

| Famille | Endpoints | Qui |
|---|---|---|
| Authentification | `/auth/whoami`, `/auth/scopes*` | super admin |
| Cycle de vie de la migration | `/migrations`, `/migrations/{job_id}`, `…/resume`, `…/retry`, `…/refresh` | super admin |
| Travail par qtree | `…/test`, `…/clone`, `…/acl`, `…/cleanup` | tokens clients, dans leur scope |
| Faisabilité | `/preflight/create`, `…/preflight/{action}` | quiconque a le scope correspondant |

---

## 4. S'authentifier

Tous les endpoints sauf `/health` exigent un token. Sans token, c'est `401` :

![401 sans token](images/03-no-token-401.png)

Cliquez sur **Authorize** (en haut à droite), collez le token, puis
**Authorize** et **Close**. Swagger UI ajoute désormais
`Authorization: Bearer …` à tous les appels lancés depuis cette page.

![La boîte de dialogue Authorize](images/04-authorize-dialog.png)

Vérifiez qui vous êtes avec `GET /api/v1/auth/whoami`. Dépliez l'opération,
**Try it out**, puis **Execute** :

![whoami en super admin](images/05-whoami-super-admin.png)

`"super_admin": true` signifie que le token global a été accepté : aucune
restriction d'action ni de qtree. Un token délégué répond avec ses propres
listes `qtrees` et `actions`.

`GET /api/v1/health` ne demande aucun token — utilisez-le pour vérifier que
l'API est debout *et* déverrouillée :

![endpoint health](images/06-health.png)

---

## 5. Déléguer des tokens aux clients

Réservé au super admin. `POST /api/v1/auth/scopes/import` prend un CSV
`qtree,token,actions[,label]`. Écrivez `NEW_TOKEN` dans la colonne token et
l'API en génère un :

![Import des scopes depuis un CSV](images/07-scopes-import.png)

La réponse renvoie un CSV contenant les **tokens générés en clair** — c'est
la seule fois où ils sont lisibles. Remettez chacun à son propriétaire et ne
conservez pas la réponse ; le coffre lui-même ne stocke que des empreintes
salées.

`GET /api/v1/auth/scopes` liste ce qui existe, sans jamais montrer un token :

![Liste des scopes délégués](images/08-scopes-list.png)

Les scopes se modifient ensuite avec `PATCH /auth/scopes/{token_id}` et se
révoquent avec `DELETE /auth/scopes/{token_id}`.

---

## 6. Vérifier la faisabilité avant d'agir

Chaque action vérifie ses prérequis avant de toucher à quoi que ce soit, et
chaque contrôle peut aussi être lancé seul. `POST /api/v1/preflight/create`
répond par un rapport et ne modifie rien :

![Rapport de pré-vol pour create](images/09-preflight-create.png)

Lisez `ok` d'abord (ainsi que `failed_count` / `warning_count`), puis la liste
`checks`. Chaque entrée porte un `code` stable, un `title` lisible, ce qui a
été réellement observé (`detail`), l'objet concerné (`target`) et un `hint`
quand il y a quelque chose à faire. Un contrôle en échec dit *pourquoi*, pas
seulement *non* :

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

`severity` compte : `error` bloque l'action, `warning` non. Un avertissement
fréquent est `*_UNREADABLE` — l'objet existe peut-être très bien, mais le
rôle ONTAP du compte d'API ne peut pas le lire, donc le contrôle n'a pas pu
conclure. Son `hint` nomme le droit manquant (par exemple
`grant readonly on /api/svm/peers`).

Le même endpoint existe par action et par job :
`POST /api/v1/migrations/{job_id}/preflight/{action}`, avec `action` parmi
`resume`, `retry`, `test`, `clone`, `acl` ou `cleanup`.

---

## 7. Créer la cascade

`POST /api/v1/migrations` lance la migration. Le pré-vol tourne **d'abord, de
façon synchrone** : un refus revient en `422` avec le rapport ci-dessus, et
rien n'est créé. Ce n'est qu'une fois les contrôles passés que le travail
démarre en arrière-plan et que l'appel répond `202` :

![Création d'une migration](images/10-create.png)

Conservez le `job_id` de la réponse : tous les appels suivants en ont besoin.

Deux champs à connaître :

* `create_mode` : `pivot-only` met en place Source → Pivot et s'arrête là ;
  `full` va jusqu'à PROD et DR.
* `dry_run` : `true` simule tout, ne contacte aucun cluster et n'écrit aucun
  état de job. Idéal pour répéter un run. *(Toutes les captures de ce guide
  ont été produites ainsi.)*

---

## 8. Suivre une migration en cours

`GET /api/v1/migrations` liste les jobs connus :

![Liste des migrations](images/11-list-migrations.png)

`GET /api/v1/migrations/{job_id}/status` donne l'état réel — collez le
`job_id` dans le champ, puis **Execute** :

![Statut du job](images/12-status.png)

Le statut décrit les relations SnapMirror en termes explicites
(`MIRROR_HEALTHY`, `TRANSFER_ACTIVE`, `TRANSFER_FAILED`, `MIRROR_BROKEN`,
`MIRROR_ABSENT`) plutôt qu'avec les chaînes brutes d'ONTAP, ainsi que le
point de reprise atteint. `POST …/refresh` relit les clusters et réécrit le
fichier de job ; `POST …/resume` reprend un job arrêté à un checkpoint et
`POST …/retry` rejoue la phase en échec.

---

## 9. Actions par qtree : test, clone, acl

Ce sont les opérations qu'un token client peut lancer, dans son propre scope.

### test — construire tout l'environnement futur, sans engagement

`POST /api/v1/migrations/{job_id}/test` crée les FlexClones sur la future
PROD **et** la future DR ainsi que la relation SnapMirror entre les deux —
tout sauf le split et le volume move. Le client valide accès et permissions
sur un environnement réel ; rien n'est engagé.

Vérifiez d'abord avec `preflight/test` :

![Pré-vol pour test](images/13-preflight-test.png)

Puis lancez :

![Exécution de test](images/14-test.png)

`volume_map` donne, pour chaque qtree, le nom du volume à créer — c'est le
client qui le choisit, rien n'est généré automatiquement. C'est obligatoire :
un qtree sans nom échoue au pré-vol avec `VOLUME_MAP_MISSING`. Un `clone`
ultérieur hérite du mapping enregistré dans le fichier de job par `test`, il
n'est donc à répéter que s'il change. Trois formes sont acceptées, au choix :

```json
{"volume_map": {"q_finance": "vol_fin_prod"}}
{"volume_map": [{"qtree": "q_finance", "volume": "vol_fin_prod"}]}
{"volume_map": "qtree,volume\nq_finance,vol_fin_prod\n"}
```

`validity_days` (7 par défaut) enregistre la date d'expiration de
l'environnement de test.

### clone — promouvoir l'environnement de test

`POST /api/v1/migrations/{job_id}/clone` avant expiration **promeut** ce que
`test` a construit : il ne fait que les volume moves qui détachent les clones
de leur parent. Rien n'est reconstruit.

![Exécution de clone](images/15-clone.png)

`"fresh": true` ignore un environnement de test existant et rejoue le flux
complet sur une base propre — les anciens clones de test restent sur les
clusters et sont listés en fin de run pour suppression manuelle.

### acl — forcer des groupes AD sur un chemin

`POST /api/v1/migrations/{job_id}/acl` est totalement indépendant de
test/clone et agit sur **un chemin explicite** :

![Application des ACL](images/16-acl.png)

Les antislashs doivent être échappés en JSON : `"CORP\\grp_finance_rw"`.

---

## 10. Ce qu'un token à scope limité peut faire

Ré-authentifiez-vous avec un token délégué pour voir l'application des droits
du côté client :

![Authentification avec un token délégué](images/17-authorize-scoped.png)

Un qtree hors du scope du token est refusé en `403`, et la réponse montre ce
que le token possède réellement — le client sait ainsi s'il s'est trompé de
nom ou si le droit ne lui a jamais été accordé :

![403 sur un qtree hors scope](images/18-scoped-forbidden-qtree.png)

Les actions réservées sont refusées de la même façon. `create`, `resume`,
`retry`, `refresh` et l'administration des tokens sont réservées au super
admin, quel que soit le scope qtree du token :

![403 sur une action réservée](images/19-scoped-forbidden-action.png)

---

## 11. Lire les réponses

| Code | Signification | Que faire |
|---|---|---|
| `200` | terminé, action synchrone | — |
| `202` | accepté, tourne en arrière-plan | interroger `GET …/status` |
| `401` | pas de token, ou token inconnu | cliquer sur **Authorize** |
| `403` | authentifié, mais hors scope | le corps liste le scope accordé |
| `404` | `job_id` inconnu | vérifier avec `GET /api/v1/migrations` |
| `409` | une autre action tourne déjà sur ce job | attendre, puis interroger le statut |
| `422` | refus du pré-vol, ou corps mal formé | lire `checks` : rien n'a été modifié |
| `503` | API verrouillée, ou pas de coffre | la déverrouiller (section 2) |

Un `422` de pré-vol a toujours cette forme :

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

`failed_checks` ne contient que ce qui bloque — commencez par là. `checks`
porte le rapport complet, contrôles réussis inclus.

Rien n'est jamais à moitié fait à cause d'un `422` : les contrôles tournent
avant la première écriture.

---

## Régénérer ce guide

Les captures sont produites depuis le code, contre une instance réellement
lancée : elles ne peuvent donc pas dériver silencieusement de l'API.

```bash
python3 -m pip install playwright pillow     # les navigateurs sont déjà là
python3 tools/capture_swagger_guide.py
```

Le script démarre une API sur le port 8321 avec un coffre temporaire et le
transport dry-run, pilote Chromium sur tout le parcours, écrit
`docs/images/*.png`, puis supprime tout ce qu'il a créé. À relancer dès que
la surface de l'API ou la version de Swagger change.

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netapp_cascade_migration.py
===========================

Outil d'orchestration pour la migration de stockage NetApp ONTAP selon une
topologie en cascade ("passe-plat" / cascading) sur 3 niveaux :

    Niveau Supérieur (Sources)        ->  Clusters de production "hauts"
                                          (ex: CMOPARPA4MUT100, CMOPARTIGMUT100)
                |
                |  SnapMirror (relation 1)
                v
    Niveau Intermédiaire (Pivot)      ->  Cluster de transit / gare de triage
                                          (CMOPARTIGBKP110)
                |
                |  SnapMirror (relation 2)
                v
    Niveau Inférieur (Destinations)   ->  Clusters cibles "bas"
                                          (ex: CMOPARPA4SFS100, CMOPARDC5SFS100)

Objectif : migrer la donnee tout en ECLATANT la structure de stockage. Les
volumes sources contiennent plusieurs Qtrees ; a la destination finale, chaque
Qtree doit etre isolee dans son propre volume dedie (regle : 1 Volume = 1 Qtree).

------------------------------------------------------------------------------
CONTRAINTES STRICTES IMPLEMENTEES
------------------------------------------------------------------------------
* EXECUTION VIA CLI ONTAP  : toutes les actions passent par des commandes
  ONTAP CLI standard, executees a travers SSH.
* AUTHENTIFICATION          : aucune gestion de login/password. On s'appuie
  exclusivement sur le trust SSH natif (echange de cles deja en place). Le
  transport SSH par defaut est le client SSH natif du systeme (via subprocess),
  ce qui respecte la configuration ~/.ssh/config et l'agent SSH. Un backend
  paramiko est egalement disponible (--ssh-backend paramiko) si paramiko est
  installe ; il utilise lui aussi les cles du systeme (aucun mot de passe).
* TRACABILITE ABSOLUE       : chaque commande envoyee a un cluster est journalisee
  de facon exhaustive (date/heure, cluster cible, commande brute, exit code,
  stdout et stderr complets) dans un fichier de log dedie.
* GESTION D'ERREURS         : la sortie textuelle (stdout/stderr) de chaque
  commande est analysee. Toute commande qui echoue (exit code != 0) ou dont la
  sortie contient un motif d'erreur ONTAP stoppe immediatement l'execution,
  archive l'erreur dans le log et leve une exception propre (OntapCliError).

------------------------------------------------------------------------------
INTERFACE CLI
------------------------------------------------------------------------------
Arguments imposes :
    --source-cluster   Nom/IP du cluster source (haut)
    --pivot-cluster    Nom/IP du cluster pivot (CMOPARTIGBKP110)
    --dest-cluster     Nom/IP du cluster destination (bas)
    --volume           Nom du volume source concerne
    --action           'create' | 'clone' | 'cleanup'

Arguments specifiques aux actions :
    --qtrees           (clone)   liste 'q1,q2' OU mot-cle 'all'
    --qtree            (cleanup) une seule Qtree cible

Arguments techniques additionnels (necessaires aux commandes ONTAP reelles,
fournis avec des valeurs par defaut raisonnables ; a adapter a votre contexte) :
    --source-vserver / --pivot-vserver / --dest-vserver   SVM par niveau
    --pivot-aggr / --dest-aggr                            Agregats cibles
    --noaccess-policy                                     export-policy restrictive
    --ssh-backend {subprocess,paramiko}                   transport SSH
    --ssh-user                                            user@host optionnel
    --log-file                                            chemin du log
    --dry-run                                             simulation (rien n'est execute)
    --timeout / --poll-interval                           pour le suivi SnapMirror

Exemples :
    python3 netapp_cascade_migration.py \\
        --source-cluster CMOPARTIGMUT100 \\
        --pivot-cluster  CMOPARTIGBKP110 \\
        --dest-cluster   CMOPARPA4SFS100 \\
        --volume vol_prod_01 \\
        --action create \\
        --pivot-aggr aggr1_pivot --dest-aggr aggr1_dest

    python3 netapp_cascade_migration.py ... --action clone  --qtrees all
    python3 netapp_cascade_migration.py ... --action cleanup --qtree qtree_finance
"""

import argparse
import datetime
import logging
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional


# =============================================================================
# 1. EXCEPTIONS ET STRUCTURES DE DONNEES
# =============================================================================

class OntapCliError(Exception):
    """Exception propre levee des qu'une commande ONTAP CLI echoue.

    On y embarque le contexte complet (cluster, commande, exit code, sorties)
    afin que l'erreur remontee soit immediatement exploitable.
    """

    def __init__(self, cluster: str, command: str, exit_code: int,
                 stdout: str, stderr: str, reason: str = ""):
        self.cluster = cluster
        self.command = command
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.reason = reason
        message = (
            f"Echec commande ONTAP sur '{cluster}' "
            f"(exit={exit_code}{', ' + reason if reason else ''}) : {command}"
        )
        super().__init__(message)


@dataclass
class CommandResult:
    """Resultat structure d'une commande ONTAP CLI executee via SSH."""
    cluster: str
    command: str
    exit_code: int
    stdout: str
    stderr: str
    started_at: datetime.datetime
    duration_s: float


# =============================================================================
# 2. EXECUTEUR DE COMMANDES ONTAP CLI (SSH + LOG + DETECTION D'ERREUR)
# =============================================================================

# Motifs d'erreur typiques renvoyes par l'ONTAP CLI meme lorsque le code de
# retour SSH est 0 (l'ONTAP CLI ecrit souvent l'erreur sur stdout/stderr en
# conservant un exit code SSH a 0). On reste volontairement strict.
ONTAP_ERROR_PATTERNS = [
    re.compile(r"^\s*Error:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\berror\b.*\bfailed\b", re.IGNORECASE),
    re.compile(r"\bcommand failed\b", re.IGNORECASE),
    re.compile(r"\bis not a recognized command\b", re.IGNORECASE),
    re.compile(r"\bnot authorized\b", re.IGNORECASE),
    re.compile(r"\bduplicate\b.*\bentry\b", re.IGNORECASE),
    re.compile(r"\bdoes not exist\b", re.IGNORECASE),
    re.compile(r"\binsufficient\b", re.IGNORECASE),
]

# Motifs autorises (faux positifs) : certaines sorties legitimes contiennent le
# mot "error" sans qu'il s'agisse d'une erreur (ex: colonne "Last Transfer
# Error" vide d'un snapmirror show). On les neutralise avant l'analyse.
ONTAP_BENIGN_PATTERNS = [
    re.compile(r"Last Transfer Error\s*:\s*-?\s*$", re.IGNORECASE | re.MULTILINE),
]


class OntapCliExecutor:
    """Encapsule l'execution d'une commande ONTAP CLI sur un cluster via SSH.

    - Respecte le trust SSH natif (aucun mot de passe gere).
    - Trace chaque commande de maniere exhaustive dans le logger fourni.
    - Analyse stdout/stderr et le code de retour pour detecter les erreurs.
    """

    def __init__(self, logger: logging.Logger, ssh_backend: str = "subprocess",
                 ssh_user: Optional[str] = None, dry_run: bool = False,
                 connect_timeout: int = 30):
        self.log = logger
        self.ssh_backend = ssh_backend
        self.ssh_user = ssh_user
        self.dry_run = dry_run
        self.connect_timeout = connect_timeout
        self._paramiko = None  # chargement paresseux

        if ssh_backend == "paramiko":
            self._init_paramiko()

    # ---- Initialisation paramiko (optionnelle) --------------------------- #
    def _init_paramiko(self):
        try:
            import paramiko  # import local pour ne pas l'imposer en dependance
            self._paramiko = paramiko
        except ImportError as exc:
            raise RuntimeError(
                "Backend SSH 'paramiko' demande mais le module paramiko n'est "
                "pas installe (pip install paramiko)."
            ) from exc

    # ---- Construction de la cible SSH ------------------------------------ #
    def _ssh_target(self, cluster: str) -> str:
        """Retourne 'user@cluster' si un user est fourni, sinon 'cluster'."""
        return f"{self.ssh_user}@{cluster}" if self.ssh_user else cluster

    # ---- Execution principale -------------------------------------------- #
    def run(self, cluster: str, command: str,
            allow_failure: bool = False) -> CommandResult:
        """Execute `command` sur `cluster` via SSH et retourne un CommandResult.

        :param allow_failure: si True, ne leve pas d'exception sur erreur
            (utile pour des commandes de decouverte tolerantes). L'erreur reste
            tracee dans le log.
        :raises OntapCliError: si la commande echoue et allow_failure=False.
        """
        started_at = datetime.datetime.now()
        t0 = time.monotonic()

        if self.dry_run:
            # En dry-run on ne contacte aucun cluster : on trace l'intention.
            duration = time.monotonic() - t0
            result = CommandResult(cluster, command, 0,
                                   "[DRY-RUN] commande non executee", "",
                                   started_at, duration)
            self._trace(result, dry_run=True)
            return result

        if self.ssh_backend == "paramiko":
            exit_code, stdout, stderr = self._run_paramiko(cluster, command)
        else:
            exit_code, stdout, stderr = self._run_subprocess(cluster, command)

        duration = time.monotonic() - t0
        result = CommandResult(cluster, command, exit_code, stdout, stderr,
                               started_at, duration)
        self._trace(result)

        # Detection d'erreur (exit code OU motif textuel dans la sortie).
        error_reason = self._detect_error(result)
        if error_reason and not allow_failure:
            # Archivage explicite de l'erreur puis levee d'une exception propre.
            self.log.error("ARRET : erreur detectee (%s) sur le cluster %s.",
                           error_reason, cluster)
            raise OntapCliError(cluster, command, exit_code, stdout, stderr,
                                reason=error_reason)
        if error_reason and allow_failure:
            self.log.warning("Erreur toleree (allow_failure) sur %s : %s",
                             cluster, error_reason)

        return result

    # ---- Backend subprocess (client SSH natif) --------------------------- #
    def _run_subprocess(self, cluster: str, command: str):
        """Execute via le client ssh du systeme (trust par cles natif)."""
        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",               # jamais de prompt interactif
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",
            self._ssh_target(cluster),
            command,                              # commande ONTAP CLI integrale
        ]
        try:
            proc = subprocess.run(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except FileNotFoundError as exc:
            raise RuntimeError("Client 'ssh' introuvable sur le systeme.") from exc

    # ---- Backend paramiko ------------------------------------------------ #
    def _run_paramiko(self, cluster: str, command: str):
        """Execute via paramiko en s'appuyant sur les cles SSH du systeme."""
        client = self._paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(self._paramiko.AutoAddPolicy())
        try:
            # look_for_keys + agent : aucun mot de passe, uniquement les cles.
            client.connect(
                hostname=cluster,
                username=self.ssh_user,            # peut etre None -> user courant
                timeout=self.connect_timeout,
                look_for_keys=True,
                allow_agent=True,
            )
            _stdin, stdout, stderr = client.exec_command(command)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return exit_code, out, err
        finally:
            client.close()

    # ---- Tracabilite exhaustive ------------------------------------------ #
    def _trace(self, r: CommandResult, dry_run: bool = False):
        """Journalise integralement la commande et son resultat."""
        banner = "DRY-RUN " if dry_run else ""
        self.log.info(
            "\n%s================ COMMANDE ONTAP CLI ================\n"
            "%sDate/heure   : %s\n"
            "%sCluster cible: %s\n"
            "%sCommande     : %s\n"
            "%sExit code    : %s\n"
            "%sDuree (s)    : %.2f\n"
            "%s---- STDOUT ----\n%s\n"
            "%s---- STDERR ----\n%s\n"
            "%s====================================================",
            banner,
            banner, r.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            banner, r.cluster,
            banner, r.command,
            banner, r.exit_code,
            banner, r.duration_s,
            banner, r.stdout.rstrip() or "(vide)",
            banner, r.stderr.rstrip() or "(vide)",
            banner,
        )

    # ---- Analyse d'erreur ------------------------------------------------- #
    def _detect_error(self, r: CommandResult) -> Optional[str]:
        """Retourne une raison d'erreur (str) si detectee, sinon None."""
        if r.exit_code != 0:
            return f"exit code non nul ({r.exit_code})"

        combined = f"{r.stdout}\n{r.stderr}"

        # Neutralisation des faux positifs connus avant l'analyse stricte.
        for benign in ONTAP_BENIGN_PATTERNS:
            combined = benign.sub("", combined)

        for pattern in ONTAP_ERROR_PATTERNS:
            if pattern.search(combined):
                return f"motif d'erreur ONTAP detecte ({pattern.pattern})"
        return None


# =============================================================================
# 3. UTILITAIRES DE PARSING DES SORTIES ONTAP
# =============================================================================

def parse_qtree_list(stdout: str) -> List[str]:
    """Extrait la liste des noms de Qtrees depuis 'volume qtree show -fields qtree'.

    L'ONTAP CLI expose une qtree par defaut '' (racine du volume) qu'on ignore.
    La sortie tabulaire ressemble a :

        vserver   volume        qtree
        --------- ------------- -----------
        svm1      vol_prod_01   ""
        svm1      vol_prod_01   qtree_finance
        svm1      vol_prod_01   qtree_rh
    """
    qtrees: List[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.lower().startswith("vserver"):
            continue
        if "entries were displayed" in line.lower():
            continue
        cols = line.split()
        if not cols:
            continue
        qtree_name = cols[-1].strip('"')
        # On ignore la qtree racine (vide) representee par "" ou '-'.
        if qtree_name and qtree_name not in ('""', "-"):
            qtrees.append(qtree_name)
    return qtrees


def parse_field_value(stdout: str, field_name: str) -> Optional[str]:
    """Extrait une valeur depuis une sortie ONTAP au format '-instance' (clef: valeur)."""
    pattern = re.compile(rf"^\s*{re.escape(field_name)}\s*:\s*(.+?)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    m = pattern.search(stdout)
    return m.group(1).strip() if m else None


def parse_size_to_bytes(size_str: str) -> Optional[int]:
    """Convertit une taille ONTAP ('100GB', '1.5TB', '512MB', '2048') en octets."""
    if not size_str:
        return None
    size_str = size_str.strip().upper().replace("B", "").replace("I", "")
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    m = re.match(r"^([\d.]+)\s*([KMGTP]?)$", size_str)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    return int(value * units.get(unit, 1))


def parse_cifs_shares_for_path(stdout: str, qtree_path_fragment: str) -> List[str]:
    """Retourne les noms de partages CIFS dont le path contient `qtree_path_fragment`.

    Attendu : sortie de 'vserver cifs share show -fields share-name,path'.
    """
    shares: List[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.lower().startswith("vserver"):
            continue
        if "entries were displayed" in line.lower():
            continue
        cols = line.split()
        if len(cols) < 2:
            continue
        # Heuristique : la derniere colonne est le path, l'avant-derniere le nom.
        path = cols[-1]
        share_name = cols[-2]
        if qtree_path_fragment in path:
            shares.append(share_name)
    return shares


# =============================================================================
# 4. ORCHESTRATEUR DE MIGRATION (LOGIQUE METIER DES 3 ACTIONS)
# =============================================================================

class MigrationOrchestrator:
    """Pilote la cascade Source -> Pivot -> Destination via l'ONTAP CLI."""

    def __init__(self, executor: OntapCliExecutor, args: argparse.Namespace):
        self.x = executor
        self.log = executor.log
        self.a = args  # raccourci vers les arguments CLI

        # Raccourcis lisibles vers les clusters / SVM.
        self.src_cluster = args.source_cluster
        self.pivot_cluster = args.pivot_cluster
        self.dest_cluster = args.dest_cluster
        self.volume = args.volume

        self.src_svm = args.source_vserver
        self.pivot_svm = args.pivot_vserver
        self.dest_svm = args.dest_vserver

    # ----- Helpers de chemins SnapMirror ---------------------------------- #
    def _path(self, svm: str, volume: str) -> str:
        """Construit un chemin SnapMirror 'svm:volume'."""
        return f"{svm}:{volume}"

    # =====================================================================
    # ACTION 1 : 'create' -> Initialisation de la cascade
    # =====================================================================
    def action_create(self):
        self.log.info("######## ACTION 'create' : initialisation de la cascade ########")

        # --- Etape 0 : recuperer les caracteristiques du volume source ----
        src_size = self._get_volume_size(self.src_cluster, self.src_svm, self.volume)
        self.log.info("Taille du volume source '%s' : %s", self.volume, src_size)
        src_bytes = parse_size_to_bytes(src_size) if src_size else None

        # --- Etape 1 : verification de l'espace sur Pivot puis Destination -
        self._check_aggregate_space(self.pivot_cluster, self.a.pivot_aggr, src_bytes)
        self._check_aggregate_space(self.dest_cluster, self.a.dest_aggr, src_bytes)

        # --- Etape 2 : creation des volumes DP (Pivot puis Destination) ----
        # Les volumes destinataires d'une relation SnapMirror sont de type 'DP'.
        self._create_dp_volume(self.pivot_cluster, self.pivot_svm, self.volume,
                               self.a.pivot_aggr, src_size)
        self._create_dp_volume(self.dest_cluster, self.dest_svm, self.volume,
                               self.a.dest_aggr, src_size)

        # --- Etape 3 : configuration des relations SnapMirror en cascade ---
        # Relation 1 : Source -> Pivot (creee/initialisee SUR le Pivot).
        self._snapmirror_create_init(
            run_on=self.pivot_cluster,
            source_path=self._path(self.src_svm, self.volume),
            dest_path=self._path(self.pivot_svm, self.volume),
        )
        # Relation 2 : Pivot -> Destination (creee/initialisee SUR la Destination).
        self._snapmirror_create_init(
            run_on=self.dest_cluster,
            source_path=self._path(self.pivot_svm, self.volume),
            dest_path=self._path(self.dest_svm, self.volume),
        )

        # --- Etape 4 : suivi jusqu'a Snapmirrored / Idle -------------------
        self._wait_snapmirror_ready(self.pivot_cluster,
                                    self._path(self.pivot_svm, self.volume))
        self._wait_snapmirror_ready(self.dest_cluster,
                                    self._path(self.dest_svm, self.volume))

        self.log.info("ACTION 'create' terminee : cascade initialisee et synchronisee.")

    # =====================================================================
    # ACTION 2 : 'clone' -> Split des Qtrees & creation des volumes cibles
    # =====================================================================
    def action_clone(self):
        self.log.info("######## ACTION 'clone' : split des Qtrees ########")

        # --- Etape 1 : determiner la liste des Qtrees a traiter ------------
        if self.a.qtrees.strip().lower() == "all":
            self.log.info("Mode 'all' : decouverte des Qtrees du volume source.")
            qtrees = self._list_source_qtrees()
        else:
            qtrees = [q.strip() for q in self.a.qtrees.split(",") if q.strip()]

        if not qtrees:
            raise OntapCliError(self.src_cluster, "volume qtree show", 0, "", "",
                                reason="aucune Qtree a traiter")
        self.log.info("Qtrees a migrer (%d) : %s", len(qtrees), ", ".join(qtrees))

        # On a besoin de l'ensemble complet des Qtrees pour l'operation de split
        # (suppression de toutes les autres Qtrees dans chaque volume clone).
        all_qtrees = self._list_source_qtrees()

        # --- Etape 2 : traitement Qtree par Qtree --------------------------
        for qtree in qtrees:
            self._clone_single_qtree(qtree, all_qtrees)

        self.log.info("ACTION 'clone' terminee : %d volume(s) cible(s) cree(s).",
                      len(qtrees))

    def _clone_single_qtree(self, qtree: str, all_qtrees: List[str]):
        """Traite une seule Qtree : snapshot, update cascade, verif, clone, split."""
        self.log.info("---- Traitement de la Qtree '%s' ----", qtree)

        # Nom du snapshot dedie (horodate pour tracabilite et unicite).
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_name = f"migr_{qtree}_{stamp}"
        clone_volume = f"v_{qtree}"  # regle de nommage imposee : v_[nom_qtree]

        # a. Creer un Snapshot sur le cluster SOURCE.
        self.x.run(self.src_cluster,
                   f"volume snapshot create -vserver {self.src_svm} "
                   f"-volume {self.volume} -snapshot {snap_name}")

        # b. Propager le snapshot via la cascade (Source->Pivot, puis Pivot->Dest).
        self.x.run(self.pivot_cluster,
                   f"snapmirror update "
                   f"-destination-path {self._path(self.pivot_svm, self.volume)}")
        self._wait_snapmirror_idle(self.pivot_cluster,
                                   self._path(self.pivot_svm, self.volume))

        self.x.run(self.dest_cluster,
                   f"snapmirror update "
                   f"-destination-path {self._path(self.dest_svm, self.volume)}")
        self._wait_snapmirror_idle(self.dest_cluster,
                                   self._path(self.dest_svm, self.volume))

        # c. Verifier que le snapshot est bien arrive sur la DESTINATION.
        self._verify_snapshot_present(self.dest_cluster, self.dest_svm,
                                      self.volume, snap_name)

        # d. Creer le FlexClone sur la DESTINATION en ciblant ce snapshot precis.
        self.x.run(self.dest_cluster,
                   f"volume clone create -vserver {self.dest_svm} "
                   f"-flexclone {clone_volume} -parent-volume {self.volume} "
                   f"-parent-snapshot {snap_name} -junction-active true")
        self.log.info("FlexClone '%s' cree a partir du snapshot '%s'.",
                      clone_volume, snap_name)

        # e. Operation de SPLIT : supprimer toutes les AUTRES Qtrees du clone
        #    pour ne garder que la Qtree destinee a ce volume (1 Volume = 1 Qtree).
        others = [q for q in all_qtrees if q != qtree]
        for other in others:
            self.x.run(self.dest_cluster,
                       f"volume qtree delete -vserver {self.dest_svm} "
                       f"-volume {clone_volume} -qtree {other} -force true")
            self.log.info("Qtree '%s' supprimee du clone '%s'.", other, clone_volume)

        self.log.info("Qtree '%s' isolee dans le volume '%s'.", qtree, clone_volume)

    # =====================================================================
    # ACTION 3 : 'cleanup' -> Cut-off des acces a la source
    # =====================================================================
    def action_cleanup(self):
        self.log.info("######## ACTION 'cleanup' : cut-off des acces source ########")
        qtree = self.a.qtree

        # 1. Isolation : appliquer l'export-policy restrictive a la Qtree source.
        self.x.run(self.src_cluster,
                   f"volume qtree modify -vserver {self.src_svm} "
                   f"-volume {self.volume} -qtree {qtree} "
                   f"-export-policy {self.a.noaccess_policy}")
        self.log.info("Export-policy '%s' appliquee a la Qtree '%s'.",
                      self.a.noaccess_policy, qtree)

        # 2. Suppression CIFS : identifier puis supprimer les partages pointant
        #    vers cette Qtree.
        shares = self._find_cifs_shares_for_qtree(qtree)
        if not shares:
            self.log.info("Aucun partage CIFS associe a la Qtree '%s'.", qtree)
        for share in shares:
            self.x.run(self.src_cluster,
                       f"vserver cifs share delete -vserver {self.src_svm} "
                       f"-share-name {share}")
            self.log.info("Partage CIFS '%s' supprime.", share)

        # 3. Renommage : injecter le suffixe horodate du jour.
        today = datetime.datetime.now().strftime("%d_%m_%Y")
        new_name = f"{qtree}_tobedeleted_migratedtosfs_{today}"
        self.x.run(self.src_cluster,
                   f"volume qtree rename -vserver {self.src_svm} "
                   f"-volume {self.volume} -qtree {qtree} "
                   f"-new-qtree-name {new_name}")
        self.log.info("Qtree source renommee : '%s' -> '%s'.", qtree, new_name)

        self.log.info("ACTION 'cleanup' terminee pour la Qtree '%s'.", qtree)

    # =====================================================================
    # PRIMITIVES ONTAP REUTILISABLES
    # =====================================================================
    def _get_volume_size(self, cluster: str, svm: str, volume: str) -> Optional[str]:
        """Recupere la taille provisionnee du volume (champ 'size')."""
        r = self.x.run(cluster,
                       f"volume show -vserver {svm} -volume {volume} "
                       f"-fields size -instance")
        size = parse_field_value(r.stdout, "Volume Size") or \
            parse_field_value(r.stdout, "size")
        return size

    def _check_aggregate_space(self, cluster: str, aggr: str,
                               required_bytes: Optional[int]):
        """Verifie que l'agregat dispose d'assez d'espace pour le volume source."""
        self.log.info("Verification de l'espace sur %s / agregat %s.", cluster, aggr)
        r = self.x.run(cluster,
                       f"storage aggregate show -aggregate {aggr} "
                       f"-fields availsize,size,usedsize -instance")
        avail_str = parse_field_value(r.stdout, "Available Size") or \
            parse_field_value(r.stdout, "availsize")
        avail_bytes = parse_size_to_bytes(avail_str) if avail_str else None

        if required_bytes is None or avail_bytes is None:
            self.log.warning(
                "Impossible de comparer precisement l'espace (requis=%s, dispo=%s). "
                "Verification manuelle recommandee.", required_bytes, avail_bytes)
            return

        self.log.info("Agregat %s : dispo=%d octets, requis=%d octets.",
                      aggr, avail_bytes, required_bytes)
        if avail_bytes < required_bytes:
            raise OntapCliError(
                cluster, f"storage aggregate show -aggregate {aggr}", 0,
                r.stdout, r.stderr,
                reason=(f"espace insuffisant sur l'agregat {aggr} "
                        f"(dispo={avail_bytes} < requis={required_bytes})"))
        self.log.info("Espace suffisant sur l'agregat %s.", aggr)

    def _create_dp_volume(self, cluster: str, svm: str, volume: str,
                          aggr: str, size: Optional[str]):
        """Cree un volume de type DP (destinataire SnapMirror) calque sur la source."""
        size_opt = f"-size {size} " if size else ""
        self.x.run(cluster,
                   f"volume create -vserver {svm} -volume {volume} "
                   f"-aggregate {aggr} {size_opt}-type DP")
        self.log.info("Volume DP '%s' cree sur %s (agregat %s, taille %s).",
                      volume, cluster, aggr, size or "auto")

    def _snapmirror_create_init(self, run_on: str, source_path: str,
                                dest_path: str):
        """Cree puis initialise une relation SnapMirror (executee cote destination)."""
        self.log.info("SnapMirror %s -> %s (commandes sur %s).",
                      source_path, dest_path, run_on)
        self.x.run(run_on,
                   f"snapmirror create -source-path {source_path} "
                   f"-destination-path {dest_path} -type XDP")
        self.x.run(run_on,
                   f"snapmirror initialize -destination-path {dest_path}")

    def _wait_snapmirror_ready(self, cluster: str, dest_path: str):
        """Attend que la relation atteigne l'etat 'Snapmirrored' (init terminee)."""
        self.log.info("Attente de l'etat 'Snapmirrored' pour %s ...", dest_path)
        deadline = time.monotonic() + self.a.timeout
        while True:
            r = self.x.run(cluster,
                           f"snapmirror show -destination-path {dest_path} "
                           f"-fields state,status -instance")
            state = (parse_field_value(r.stdout, "Mirror State") or
                     parse_field_value(r.stdout, "Relationship Status") or
                     parse_field_value(r.stdout, "state") or "").lower()
            self.log.info("Etat SnapMirror %s : '%s'.", dest_path, state or "inconnu")
            if state in ("snapmirrored", "idle"):
                self.log.info("Relation %s prete (etat='%s').", dest_path, state)
                return
            if time.monotonic() > deadline:
                raise OntapCliError(
                    cluster, f"snapmirror show -destination-path {dest_path}", 0,
                    r.stdout, r.stderr,
                    reason=(f"timeout ({self.a.timeout}s) en attendant "
                            f"'Snapmirrored' (etat actuel='{state}')"))
            time.sleep(self.a.poll_interval)

    def _wait_snapmirror_idle(self, cluster: str, dest_path: str):
        """Attend la fin d'un transfert (statut 'Idle') apres un snapmirror update."""
        self.log.info("Attente de la fin du transfert (Idle) pour %s ...", dest_path)
        deadline = time.monotonic() + self.a.timeout
        while True:
            r = self.x.run(cluster,
                           f"snapmirror show -destination-path {dest_path} "
                           f"-fields status -instance")
            status = (parse_field_value(r.stdout, "Relationship Status") or
                      parse_field_value(r.stdout, "status") or "").lower()
            self.log.info("Statut transfert %s : '%s'.", dest_path, status or "inconnu")
            if status == "idle":
                return
            if time.monotonic() > deadline:
                raise OntapCliError(
                    cluster, f"snapmirror show -destination-path {dest_path}", 0,
                    r.stdout, r.stderr,
                    reason=(f"timeout ({self.a.timeout}s) en attendant 'Idle' "
                            f"(statut actuel='{status}')"))
            time.sleep(self.a.poll_interval)

    def _list_source_qtrees(self) -> List[str]:
        """Liste les Qtrees du volume source via 'volume qtree show'."""
        r = self.x.run(self.src_cluster,
                       f"volume qtree show -vserver {self.src_svm} "
                       f"-volume {self.volume} -fields qtree")
        return parse_qtree_list(r.stdout)

    def _verify_snapshot_present(self, cluster: str, svm: str, volume: str,
                                 snapshot: str):
        """Verifie sur la destination que le snapshot attendu est bien present."""
        r = self.x.run(cluster,
                       f"volume snapshot show -vserver {svm} -volume {volume} "
                       f"-snapshot {snapshot} -fields snapshot")
        if snapshot not in r.stdout:
            raise OntapCliError(
                cluster,
                f"volume snapshot show -snapshot {snapshot}", 0,
                r.stdout, r.stderr,
                reason=f"snapshot '{snapshot}' absent sur la destination")
        self.log.info("Snapshot '%s' confirme present sur %s.", snapshot, cluster)

    def _find_cifs_shares_for_qtree(self, qtree: str) -> List[str]:
        """Identifie les partages CIFS pointant vers la Qtree (via leur path)."""
        r = self.x.run(self.src_cluster,
                       f"vserver cifs share show -vserver {self.src_svm} "
                       f"-fields share-name,path", allow_failure=True)
        # On recherche le fragment de path correspondant a la qtree, p.ex.
        # '/vol_prod_01/qtree_finance' ou simplement '/qtree'.
        fragment = f"/{qtree}"
        return parse_cifs_shares_for_path(r.stdout, fragment)


# =============================================================================
# 5. CONFIGURATION DU LOGGING (TRACABILITE EXHAUSTIVE)
# =============================================================================

def setup_logging(log_file: str) -> logging.Logger:
    """Configure un logger ecrivant a la fois dans un fichier dedie et la console."""
    logger = logging.getLogger("netapp_cascade_migration")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # evite les doublons en cas de reappel

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler fichier : trace exhaustive et persistante.
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Handler console : suivi en temps reel par l'operateur.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


# =============================================================================
# 6. PARSING DES ARGUMENTS CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestration de migration NetApp ONTAP en cascade "
                    "(Source -> Pivot -> Destination) avec eclatement des Qtrees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- Arguments imposes ---
    parser.add_argument("--source-cluster", required=True,
                        help="Nom/IP du cluster source (niveau haut).")
    parser.add_argument("--pivot-cluster", required=True,
                        help="Nom/IP du cluster pivot (CMOPARTIGBKP110).")
    parser.add_argument("--dest-cluster", required=True,
                        help="Nom/IP du cluster destination (niveau bas).")
    parser.add_argument("--volume", required=True,
                        help="Nom du volume source concerne.")
    parser.add_argument("--action", required=True,
                        choices=["create", "clone", "cleanup"],
                        help="Action a executer.")

    # --- Arguments specifiques aux actions ---
    parser.add_argument("--qtrees",
                        help="(clone) liste 'q1,q2' OU mot-cle 'all'.")
    parser.add_argument("--qtree",
                        help="(cleanup) une seule Qtree cible.")

    # --- Arguments techniques additionnels (necessaires aux commandes ONTAP) ---
    parser.add_argument("--source-vserver", default="svm_source",
                        help="SVM/vserver cote source (defaut: svm_source).")
    parser.add_argument("--pivot-vserver", default="svm_pivot",
                        help="SVM/vserver cote pivot (defaut: svm_pivot).")
    parser.add_argument("--dest-vserver", default="svm_dest",
                        help="SVM/vserver cote destination (defaut: svm_dest).")
    parser.add_argument("--pivot-aggr", default="aggr1_pivot",
                        help="Agregat cible sur le pivot (defaut: aggr1_pivot).")
    parser.add_argument("--dest-aggr", default="aggr1_dest",
                        help="Agregat cible sur la destination (defaut: aggr1_dest).")
    parser.add_argument("--noaccess-policy", default="ep_noaccess",
                        help="Export-policy restrictive (defaut: ep_noaccess).")

    # --- Transport SSH / tracabilite / securite d'execution ---
    parser.add_argument("--ssh-backend", choices=["subprocess", "paramiko"],
                        default="subprocess",
                        help="Transport SSH (defaut: subprocess = client ssh natif).")
    parser.add_argument("--ssh-user", default=None,
                        help="Utilisateur SSH optionnel (user@cluster). "
                             "Par defaut, l'utilisateur courant / la conf SSH.")
    parser.add_argument("--log-file", default=None,
                        help="Chemin du fichier de log "
                             "(defaut: migration_<action>_<timestamp>.log).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulation : trace les commandes sans les executer.")

    # --- Parametres de suivi SnapMirror ---
    parser.add_argument("--timeout", type=int, default=3600,
                        help="Timeout (s) d'attente des etats SnapMirror (defaut: 3600).")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Intervalle (s) entre deux 'snapmirror show' (defaut: 30).")

    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser):
    """Valide la coherence des arguments selon l'action demandee."""
    if args.action == "clone" and not args.qtrees:
        parser.error("--qtrees est obligatoire pour l'action 'clone' "
                     "(liste 'q1,q2' ou 'all').")
    if args.action == "cleanup" and not args.qtree:
        parser.error("--qtree est obligatoire pour l'action 'cleanup'.")


# =============================================================================
# 7. POINT D'ENTREE
# =============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)

    # Fichier de log dedie par defaut, horodate, par action.
    if not args.log_file:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.log_file = f"migration_{args.action}_{stamp}.log"

    logger = setup_logging(args.log_file)
    logger.info("================================================================")
    logger.info("Demarrage orchestration NetApp ONTAP - action '%s'.", args.action)
    logger.info("Source=%s | Pivot=%s | Destination=%s | Volume=%s",
                args.source_cluster, args.pivot_cluster,
                args.dest_cluster, args.volume)
    logger.info("Fichier de log : %s", args.log_file)
    if args.dry_run:
        logger.info("MODE DRY-RUN ACTIF : aucune commande ne sera reellement executee.")
    logger.info("================================================================")

    executor = OntapCliExecutor(
        logger=logger,
        ssh_backend=args.ssh_backend,
        ssh_user=args.ssh_user,
        dry_run=args.dry_run,
    )
    orchestrator = MigrationOrchestrator(executor, args)

    try:
        if args.action == "create":
            orchestrator.action_create()
        elif args.action == "clone":
            orchestrator.action_clone()
        elif args.action == "cleanup":
            orchestrator.action_cleanup()
        logger.info("SUCCES : action '%s' terminee sans erreur.", args.action)
        return 0

    except OntapCliError as exc:
        # Erreur metier ONTAP : deja tracee exhaustivement par l'executeur.
        logger.error("ECHEC ONTAP : %s", exc)
        logger.error("Execution interrompue. Consultez le log : %s", args.log_file)
        return 2
    except Exception as exc:  # garde-fou : toute autre erreur reste tracee.
        logger.exception("ECHEC INATTENDU : %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())

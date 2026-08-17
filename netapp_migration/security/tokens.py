"""Encrypted token store.

The super admin holds one **global token**. That token is never written
anywhere: it is the key material from which the store's encryption key is
derived (PBKDF2-HMAC-SHA256), and it doubles as the super-admin API token.

The store file is a small JSON envelope whose payload is encrypted with
Fernet (AES-128-CBC + HMAC-SHA256, authenticated):

    {"format": "netapp-migration-tokens", "version": 1,
     "kdf": "pbkdf2-sha256", "iterations": 600000,
     "salt": "<base64>", "payload": "<fernet token>"}

Inside the payload only **hashes** of the delegated tokens are kept, never
the tokens themselves: a clear token exists exactly once, in the CSV handed
back to the super admin at generation time. Losing it means re-issuing it.

The store lives locked on disk. Unlocking happens once, in memory, when the
super admin provides the global token on the command line — so a restart of
the API always requires a manual unlock.
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
from typing import Dict, List, Optional, Sequence

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ..models import (AuthError, GRANTABLE_ACTIONS, Principal, TokenScope,
                      ACTIONS_SUPER_ONLY)

FORMAT = "netapp-migration-tokens"
VERSION = 1
KDF_ITERATIONS = 600_000
TOKEN_PREFIX = "mtk_"
ENV_STORE = "NETAPP_TOKEN_STORE"

# Minimum length accepted for the global token: it is the only secret
# protecting every delegated token.
MIN_GLOBAL_TOKEN_LENGTH = 12


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def default_store_path() -> str:
    """Where the encrypted store lives (override with $NETAPP_TOKEN_STORE)."""
    return os.environ.get(ENV_STORE) or os.path.join(os.getcwd(),
                                                     "netapp_tokens.enc")


def generate_token() -> str:
    """A fresh delegated token (256 bits of entropy, URL-safe)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _hash_token(token: str, salt: bytes) -> str:
    """Salted SHA-256 of a token; only this ever reaches the disk."""
    return hashlib.sha256(salt + token.encode("utf-8")).hexdigest()


def _token_id(token_hash: str) -> str:
    return "tok_" + token_hash[:12]


class TokenStore:
    """Encrypted store of delegated tokens, unlocked with the global token."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_store_path()
        self._salt: Optional[bytes] = None
        self._fernet: Optional[Fernet] = None
        self._data: Optional[dict] = None
        self._super_hash: Optional[str] = None

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    @property
    def exists(self) -> bool:
        return os.path.isfile(self.path)

    @property
    def unlocked(self) -> bool:
        return self._data is not None

    def _require_unlocked(self):
        if not self.unlocked:
            raise AuthError(
                "the token store is locked",
                hint="restart the API with the global token "
                     "(python3 -m netapp_migration.interfaces.api.serve) "
                     "or pass --token on the CLI")

    # ------------------------------------------------------------------ #
    # Crypto
    # ------------------------------------------------------------------ #
    @staticmethod
    def _derive(global_token: str, salt: bytes) -> Fernet:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=KDF_ITERATIONS)
        key = base64.urlsafe_b64encode(kdf.derive(global_token.encode("utf-8")))
        return Fernet(key)

    def _write(self):
        """Persist the payload, encrypted, with restrictive permissions."""
        envelope = {
            "format": FORMAT,
            "version": VERSION,
            "kdf": "pbkdf2-sha256",
            "iterations": KDF_ITERATIONS,
            "salt": base64.b64encode(self._salt).decode("ascii"),
            "payload": self._fernet.encrypt(
                json.dumps(self._data).encode("utf-8")).decode("ascii"),
        }
        tmp = f"{self.path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, indent=2)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def initialise(self, global_token: str) -> None:
        """Create an empty store protected by `global_token`."""
        if self.exists:
            raise AuthError(f"a token store already exists at {self.path}",
                            hint="delete it deliberately to start over, or "
                                 "unlock it with its global token")
        if len(global_token) < MIN_GLOBAL_TOKEN_LENGTH:
            raise AuthError(
                f"the global token must be at least "
                f"{MIN_GLOBAL_TOKEN_LENGTH} characters long")
        self._salt = secrets.token_bytes(16)
        self._fernet = self._derive(global_token, self._salt)
        self._super_hash = _hash_token(global_token, self._salt)
        self._data = {"created_at": _now(),
                      "super_hash": self._super_hash,
                      "tokens": {}}
        self._write()

    def unlock(self, global_token: str) -> None:
        """Decrypt the store in memory. Wrong token -> AuthError."""
        if not self.exists:
            raise AuthError(f"no token store at {self.path}",
                            hint="initialise it first: --action tokens-init")
        with open(self.path, "r", encoding="utf-8") as fh:
            envelope = json.load(fh)
        if envelope.get("format") != FORMAT:
            raise AuthError(f"{self.path} is not a token store file")
        self._salt = base64.b64decode(envelope["salt"])
        fernet = self._derive(global_token, self._salt)
        try:
            payload = fernet.decrypt(envelope["payload"].encode("ascii"))
        except InvalidToken as exc:
            raise AuthError("invalid global token: the store could not be "
                            "decrypted") from exc
        self._fernet = fernet
        self._data = json.loads(payload.decode("utf-8"))
        self._super_hash = self._data.get("super_hash") or _hash_token(
            global_token, self._salt)

    def lock(self) -> None:
        """Forget every secret held in memory."""
        self._fernet = None
        self._data = None
        self._super_hash = None

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    def authenticate(self, presented: str) -> Principal:
        """Resolve a bearer token into a Principal, or raise AuthError."""
        self._require_unlocked()
        if not presented:
            raise AuthError("no token provided",
                            hint="send it as 'Authorization: Bearer <token>' "
                                 "or in the X-API-Token header")
        digest = _hash_token(presented, self._salt)

        if self._super_hash and hmac.compare_digest(digest, self._super_hash):
            return Principal(is_super_admin=True, token_id="super",
                             label="super-admin")

        for record in self._data["tokens"].values():
            if hmac.compare_digest(digest, record["hash"]):
                return Principal(is_super_admin=False,
                                 token_id=record["id"],
                                 qtrees=list(record.get("qtrees", [])),
                                 actions=list(record.get("actions", [])),
                                 label=record.get("label", ""))
        raise AuthError("unknown or revoked token")

    # ------------------------------------------------------------------ #
    # Scope management (super admin only)
    # ------------------------------------------------------------------ #
    def list_scopes(self) -> List[TokenScope]:
        self._require_unlocked()
        return [TokenScope(token_id=r["id"], qtrees=list(r.get("qtrees", [])),
                           actions=list(r.get("actions", [])),
                           label=r.get("label", ""),
                           created_at=r.get("created_at", ""),
                           updated_at=r.get("updated_at", ""))
                for r in self._data["tokens"].values()]

    def _find_by_token(self, token: str) -> Optional[dict]:
        digest = _hash_token(token, self._salt)
        return self._data["tokens"].get(_token_id(digest))

    def get_scope(self, token_id: str) -> TokenScope:
        self._require_unlocked()
        record = self._data["tokens"].get(token_id)
        if not record:
            raise AuthError(f"unknown token id '{token_id}'")
        return TokenScope(token_id=record["id"],
                          qtrees=list(record.get("qtrees", [])),
                          actions=list(record.get("actions", [])),
                          label=record.get("label", ""),
                          created_at=record.get("created_at", ""),
                          updated_at=record.get("updated_at", ""))

    @staticmethod
    def validate_actions(actions: Sequence[str]) -> List[str]:
        """Normalise and reject anything a scoped token may not receive."""
        cleaned = [a.strip().lower() for a in actions if a and a.strip()]
        if not cleaned:
            raise AuthError("no action granted",
                            hint=f"pick from: "
                                 f"{', '.join(sorted(GRANTABLE_ACTIONS))}")
        refused = [a for a in cleaned if a not in GRANTABLE_ACTIONS]
        if refused:
            reserved = [a for a in refused if a in ACTIONS_SUPER_ONLY]
            hint = (f"actions {', '.join(reserved)} act on the whole cascade "
                    f"and stay with the super admin" if reserved else
                    f"valid actions: {', '.join(sorted(GRANTABLE_ACTIONS))}")
            raise AuthError(f"cannot grant action(s): {', '.join(refused)}",
                            hint=hint)
        return sorted(set(cleaned))

    def upsert(self, qtree: str, actions: Sequence[str],
               token: Optional[str] = None, label: str = "") -> dict:
        """Create or update the grant for one qtree.

        `token` may be an existing delegated token (its scope is extended
        with this qtree) or None/NEW_TOKEN to mint a fresh one.

        Returns {"token": <clear token or "">, "token_id", "status"} —
        the clear token is present only when it was just generated.
        """
        self._require_unlocked()
        qtree = qtree.strip()
        if not qtree:
            raise AuthError("qtree is required to grant a scope")
        granted = self.validate_actions(actions)

        clear = ""
        record = None
        if token and token.strip() and token.strip().upper() != "NEW_TOKEN":
            record = self._find_by_token(token.strip())
            if record is None:
                raise AuthError(
                    f"token supplied for qtree '{qtree}' is unknown",
                    hint="use NEW_TOKEN to have the API generate one, or "
                         "paste a token this store already knows")
        if record is None:
            clear = generate_token()
            digest = _hash_token(clear, self._salt)
            record = {"id": _token_id(digest), "hash": digest, "qtrees": [],
                      "actions": [], "label": label, "created_at": _now()}
            self._data["tokens"][record["id"]] = record
            status = "created"
        else:
            status = "updated"

        qtrees = {q.lower(): q for q in record.get("qtrees", [])}
        qtrees[qtree.lower()] = qtree
        record["qtrees"] = sorted(qtrees.values())
        record["actions"] = granted
        if label:
            record["label"] = label
        record["updated_at"] = _now()
        self._write()
        return {"token": clear, "token_id": record["id"], "status": status,
                "qtree": qtree, "actions": granted}

    def set_scope(self, token_id: str, qtrees: Optional[Sequence[str]] = None,
                  actions: Optional[Sequence[str]] = None,
                  label: Optional[str] = None) -> TokenScope:
        """Change a scope dynamically (super admin), without re-issuing."""
        self._require_unlocked()
        record = self._data["tokens"].get(token_id)
        if not record:
            raise AuthError(f"unknown token id '{token_id}'")
        if actions is not None:
            record["actions"] = self.validate_actions(actions)
        if qtrees is not None:
            cleaned = sorted({q.strip() for q in qtrees if q and q.strip()})
            if not cleaned:
                raise AuthError("a scoped token must keep at least one qtree",
                                hint="revoke the token instead of emptying it")
            record["qtrees"] = cleaned
        if label is not None:
            record["label"] = label
        record["updated_at"] = _now()
        self._write()
        return self.get_scope(token_id)

    def revoke(self, token_id: str) -> None:
        self._require_unlocked()
        if token_id not in self._data["tokens"]:
            raise AuthError(f"unknown token id '{token_id}'")
        del self._data["tokens"][token_id]
        self._write()

    def rotate_global_token(self, new_global_token: str) -> None:
        """Re-encrypt the store under a new global token.

        Delegated tokens keep working: their hashes are re-computed with the
        new salt, which requires them to be re-issued — so this deliberately
        refuses when delegated tokens exist.
        """
        self._require_unlocked()
        if self._data["tokens"]:
            raise AuthError(
                "cannot rotate the global token while delegated tokens exist",
                hint="delegated token hashes are salted with the store salt; "
                     "revoke and re-issue them, then rotate")
        if len(new_global_token) < MIN_GLOBAL_TOKEN_LENGTH:
            raise AuthError(f"the global token must be at least "
                            f"{MIN_GLOBAL_TOKEN_LENGTH} characters long")
        self._salt = secrets.token_bytes(16)
        self._fernet = self._derive(new_global_token, self._salt)
        self._super_hash = _hash_token(new_global_token, self._salt)
        self._data["super_hash"] = self._super_hash
        self._write()

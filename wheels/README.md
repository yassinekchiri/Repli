# Offline wheels

Pre-downloaded packages for servers WITHOUT access to a PyPI mirror.
Install everything from this directory only (no network needed):

```bash
pip install --no-index --find-links wheels/ -r requirements.txt
```

## Platform compatibility

Every version shipped here is compatible with **CPython 3.9 through 3.12**
(the lowest-common-denominator releases were kept on purpose: e.g.
fastapi 0.128.8, requests 2.32.5 — newer releases dropped Python 3.9,
and old pip resolvers pick the highest version then fail with
"requires a different Python" instead of backtracking).

Compiled wheels (`pydantic_core`, `uvloop`, `httptools`, `pyyaml`,
`watchfiles`, `websockets`, `charset_normalizer`) are provided for
CPython 3.9 / 3.10 / 3.11 / 3.12, Linux x86_64 (manylinux). pip picks the
right variant automatically.

## Recommended install order

Upgrade pip FIRST, offline, from this same directory (pip 26.0.1 /
setuptools 82.0.1 / wheel / packaging are included — the last releases
still supporting Python 3.9):

```bash
python3 -m pip install --no-index --find-links wheels/ --upgrade pip setuptools wheel
python3 -m pip install --no-index --find-links wheels/ -r requirements.txt
```

An outdated pip may also reject recent manylinux tags ("from versions:
none") — the upgrade above fixes that too. If it still fails, check
`python3 --version`: below 3.9 nothing here can run — report it so the
requirements can be re-pinned.

## Regenerating

From any machine WITH repository access:

```bash
# current interpreter/arch:
pip download -r requirements.txt -d wheels/

# specific version (wheels only), e.g. 3.10:
pip download -r requirements.txt -d wheels/ \
    --only-binary :all: --python-version 3.10 \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    --platform manylinux1_x86_64 --platform any
```

Then commit the refreshed directory.

`requirements.txt` carries an upper bound on every direct dependency
(the first upstream release known to drop Python 3.9), so a re-download
already lands on 3.9-compatible versions — no manual pruning needed.
Two things still need checking by hand:

* **Transitive dependencies have no ceiling.** After downloading, run
  `python3 -m pip install --no-index --find-links wheels/ --dry-run
  -r requirements-dev.txt` with a Python 3.9 interpreter and delete any
  transitive wheel it rejects (past offenders: `anyio`, `urllib3`,
  `click`, `pycparser`, `iniconfig`, `python-dotenv`, `starlette`).
* **Conditional dependencies are resolved with the *downloading*
  interpreter.** `pip download` evaluates environment markers such as
  `python_version < "3.11"` against the machine you run it on, so
  `exceptiongroup` (needed by `anyio` on 3.9/3.10) is silently skipped
  when downloading from 3.11+. Add it explicitly if it disappears.

Note: `paramiko` (optional, only for `--transport ssh --ssh-backend
paramiko`) is NOT included.

# Offline wheels

Pre-downloaded packages for servers WITHOUT access to a PyPI mirror.
Install everything from this directory only (no network needed):

```bash
pip install --no-index --find-links wheels/ -r requirements.txt
```

## Platform compatibility

Pure-Python wheels (`*-py3-none-any.whl`) work everywhere. The compiled
ones (`pydantic_core`, `uvloop`, `httptools`, `pyyaml`, `watchfiles`,
`websockets`, `charset_normalizer`) are provided for:

    CPython 3.9, 3.10, 3.11 and 3.12  /  Linux x86_64 (manylinux)

pip picks the right variant automatically for the server's Python.

## "No matching distribution found" on an old pip

An outdated pip may not recognise recent manylinux tags and reject every
compiled wheel (`from versions: none`). Upgrade pip FIRST, offline, from
this same directory (pip/setuptools/wheel are included):

```bash
python3 -m pip install --no-index --find-links wheels/ --upgrade pip setuptools wheel
python3 -m pip install --no-index --find-links wheels/ -r requirements.txt
```

If it still fails, check `python3 --version`: below 3.9 the pinned
versions of fastapi/pydantic cannot run — report it so the requirements
can be re-pinned.

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

Note: `paramiko` (optional, only for `--transport ssh --ssh-backend
paramiko`) is NOT included.

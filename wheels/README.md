# Offline wheels

Pre-downloaded packages for servers WITHOUT access to a PyPI mirror.
Install everything from this directory only (no network needed):

```bash
pip install --no-index --find-links wheels/ -r requirements.txt
```

## Platform compatibility

Pure-Python wheels (`*-py3-none-any.whl`) work everywhere. The compiled
ones (`pydantic_core`, `uvloop`, `httptools`, `pyyaml`, `watchfiles`,
`websockets`, `charset_normalizer`) are built for:

    CPython 3.11 / Linux x86_64 (manylinux)

If the target server runs another Python version or architecture,
regenerate this directory from any machine that has repository access:

```bash
# from a machine matching the server's Python/arch:
pip download -r requirements.txt -d wheels/

# or cross-download for a specific version (wheels only):
pip download -r requirements.txt -d wheels/ \
    --only-binary :all: --python-version 3.9 --platform manylinux2014_x86_64
```

Then commit the refreshed directory.

Note: `paramiko` (optional, only for `--transport ssh --ssh-backend
paramiko`) is NOT included.

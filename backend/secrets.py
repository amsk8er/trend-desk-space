"""Load secrets.env (gitignored KEY=VALUE) into the process environment.

uvicorn does NOT source secrets.env, so the web backend started with
`uv run uvicorn backend.app:app` would run claude credential-less and 500 on the
first live call. The spike scripts already load secrets.env by hand; this is the
same idiom, shared so the backend gets it at startup too.

setdefault semantics: never clobber a value already exported in the shell.
Returns the list of keys it actually set (names only — never log values).
"""
import os
from pathlib import Path

from backend import config


def load_secrets_env(path: Path | None = None) -> list[str]:
    path = path or (config.ROOT / "secrets.env")
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded

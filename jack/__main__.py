import asyncio
import os
from pathlib import Path

# Load .env from the working directory (or the package's parent) BEFORE the
# watcher reads env. Existing environment always wins; values are raw (a single
# pair of quotes stripped, no export keyword).
def _load_env() -> None:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line or line.startswith("export "):
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        break

_load_env()

from .watcher import amain  # noqa: E402

asyncio.run(amain())

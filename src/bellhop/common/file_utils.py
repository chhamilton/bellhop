"""Owner-only (`0o600` / `0o700`) atomic file and directory persistence
helpers.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def ensure_private_dir(path: Path) -> Path:
  """Create `path` (and missing parents) and restrict created dirs to `0o700`.

  Walks missing ancestor directories from shallowest to deepest so intermediate
  parents are never left group/world-writable when the process `umask` is `0`.

  Args:
    path: Target directory path.

  Returns:
    The created/verified directory `Path`.
  """
  missing: list[Path] = []
  cur = path
  while not cur.exists() and cur != cur.parent:
    missing.append(cur)
    cur = cur.parent
  for directory in reversed(missing):
    directory.mkdir(exist_ok=True, mode=PRIVATE_DIR_MODE)
    with contextlib.suppress(OSError):
      os.chmod(directory, PRIVATE_DIR_MODE)
  if path.is_dir():
    with contextlib.suppress(OSError):
      os.chmod(path, PRIVATE_DIR_MODE)
  return path


def chmod_private_file(path: Path) -> None:
  """Restrict an existing file at `path` to owner-only read/write (`0o600`).

  Args:
    path: Target file path.
  """
  if path.is_file():
    with contextlib.suppress(OSError):
      os.chmod(path, PRIVATE_FILE_MODE)


def write_private_bytes(path: Path, data: bytes) -> None:
  """Atomically write `data` to `path` with strict `0o600` owner-only
  permissions.

  Creates the temporary sibling file with mode `0o600` at `os.open()` time so
  sensitive bytes are never readable by group or world users.

  Args:
    path: Destination file path.
    data: Raw byte content to write.
  """
  ensure_private_dir(path.parent)
  tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
  fd = os.open(
    str(tmp_path),
    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
    PRIVATE_FILE_MODE,
  )
  try:
    with os.fdopen(fd, "wb") as fh:
      fh.write(data)
    with contextlib.suppress(OSError):
      os.chmod(tmp_path, PRIVATE_FILE_MODE)
    os.replace(tmp_path, path)
  except BaseException:
    with contextlib.suppress(OSError):
      tmp_path.unlink(missing_ok=True)
    raise


def write_private_text(
  path: Path, content: str, *, encoding: str = "utf-8"
) -> None:
  """Atomically write UTF-8 text to `path` with strict `0o600` permissions.

  Args:
    path: Destination file path.
    content: String content to encode and write.
    encoding: Text encoding (defaults to UTF-8).
  """
  write_private_bytes(path, content.encode(encoding))

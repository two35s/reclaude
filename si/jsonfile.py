"""Atomic, private JSON persistence for credential files."""

import json
import os
import tempfile


def read_json(path):
    """Return the parsed JSON object, or None when missing/corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_private_json(path, data):
    """Replace ``path`` atomically with JSON that is user-only on POSIX.

    The temp file is created 0600, written and fsynced, then swapped in, so a
    crash leaves the old credentials intact instead of a truncated file.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

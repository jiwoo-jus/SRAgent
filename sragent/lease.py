"""One writer per run folder (docs/refactor/04-refactor-plan.md §4, phase 3).

An advisory lock on <run_dir>/.lease (fcntl.flock), released when the holder exits:

  - a command-line process that changes a review (every `sragent` command except status/runs, including the job
    subprocesses the web interface starts) holds it for its whole life, and waits for it when another holds it;
  - the web interface holds it only while it applies one change; when a job or a command holds it, the change is
    queued and applied as soon as the folder is free (never dropped, never applied under a running job).

The holder's pid and command are written into the file, so a waiting process can say what it waits for.
"""
from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path

_held: dict[str, tuple] = {}     # run_dir -> (open file, depth): re-entrant within one process


def _path(run_dir) -> Path:
    p = Path(run_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / ".lease"


def holder(run_dir) -> str:
    try:
        return _path(run_dir).read_text().strip()
    except OSError:
        return ""


def try_acquire(run_dir) -> bool:
    key = str(Path(run_dir).resolve())
    if key in _held:
        f, n = _held[key]
        _held[key] = (f, n + 1)
        return True
    f = open(_path(run_dir), "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()}: {' '.join(sys.argv[1:4]) or 'sragent'}")
    f.flush()
    _held[key] = (f, 1)
    return True


def acquire(run_dir, timeout: float | None = None, say=print) -> None:
    """Wait for the run folder (timeout None: as long as it takes)."""
    t0, told = time.time(), False
    while not try_acquire(run_dir):
        if not told:
            say(f"[lease] waiting for {run_dir}: in use by {holder(run_dir) or 'another process'}")
            told = True
        if timeout is not None and time.time() - t0 > timeout:
            raise TimeoutError(f"{run_dir} is in use by {holder(run_dir) or 'another process'}")
        time.sleep(0.5)


def release(run_dir) -> None:
    key = str(Path(run_dir).resolve())
    if key not in _held:
        return
    f, n = _held[key]
    if n > 1:
        _held[key] = (f, n - 1)
        return
    del _held[key]
    try:
        f.seek(0)
        f.truncate()
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()

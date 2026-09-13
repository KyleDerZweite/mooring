"""Private files, bounded subprocesses, and structured errors."""

import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


class Error(Exception):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def run(argv, *, cwd=None, timeout=300, env=None, input=None, limit=8 * 1024 * 1024):
    """Never include subprocess output/arguments in a public error: they may contain secrets."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError:
            raise Error(
                "missing_executable", f"Required executable is unavailable: {Path(argv[0]).name}"
            ) from None
        try:
            process.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise Error("command_timeout", f"{Path(argv[0]).name} exceeded its deadline") from None
        if process.returncode:
            raise Error(
                "command_failed", f"{Path(argv[0]).name} failed with exit status {process.returncode}"
            )
        if out.tell() > limit:
            raise Error("output_limit", "Command output exceeded the configured bound")
        out.seek(0)
        return out.read().decode()


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_write(path, value):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mooring-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def lock(path):
    try:
        stream = Path(path).open("a")  # noqa: SIM115 - close in finally after flock
    except PermissionError:
        raise Error("lock_access", "Cannot open the configured maintenance lock") from None
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error(
                "locked", "Another deployment or maintenance operation holds the lock", retryable=True
            )
        yield
    finally:
        stream.close()


def timestamp():
    return time.time()

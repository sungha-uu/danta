from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


class RuntimeAlreadyRunningError(RuntimeError):
    pass


class RuntimeInstanceLock:
    """Process-lifetime lock preventing two account runtimes from ordering."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError as exc:
                if attempt == 0 and self._remove_if_stale():
                    continue
                owner = self._owner_description()
                raise RuntimeAlreadyRunningError(
                    f"trading runtime is already running ({owner})"
                ) from exc
            try:
                payload = {
                    "pid": os.getpid(),
                    "started_at": datetime.now(UTC).isoformat(),
                }
                os.write(descriptor, json.dumps(payload).encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._acquired = True
            return
        raise RuntimeAlreadyRunningError("trading runtime lock could not be acquired")

    def release(self) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> RuntimeInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def _remove_if_stale(self) -> bool:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            pid = int(raw["pid"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.path.unlink(missing_ok=True)
            return True
        if _process_exists(pid):
            return False
        self.path.unlink(missing_ok=True)
        return True

    def _owner_description(self) -> str:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return f"pid={raw.get('pid', '?')}, started_at={raw.get('started_at', '?')}"
        except (OSError, json.JSONDecodeError):
            return "owner metadata unavailable"


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a pure existence probe on every Windows
        # runtime and can stall on invalid large PIDs. OpenProcess is.
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            # A terminated process object can remain open briefly. Only the
            # Windows STILL_ACTIVE code means the owner is actually running.
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows reports an invalid parameter for missing processes.
        return False
    return True

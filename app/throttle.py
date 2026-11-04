"""Brakes on guessing: too many wrong sign-ins / security answers for one
name, or from one address, and that name / address must wait.

In memory (one server process): a restart clears it, which only the
server's admin can do. For an AD name it also keeps a password-guesser from
locking the person's Windows account through this app.
"""

from __future__ import annotations

import threading
import time

WINDOW = 15 * 60  # failures counted over the last 15 minutes
LOCK = 15 * 60  # then refused for 15 minutes


class Throttle:
    def __init__(self, limit: int, window: int = WINDOW, lock: int = LOCK, clock=time.monotonic):
        self.limit, self.window, self.lock = limit, window, lock
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._mutex = threading.Lock()

    def wait_seconds(self, key: str) -> int:
        """0 = go ahead; otherwise how long this key is still refused."""
        with self._mutex:
            until = self._locked.get(key, 0)
            left = until - self._clock()
            if left <= 0:
                self._locked.pop(key, None)
                return 0
            return int(left) + 1

    def fail(self, key: str) -> bool:
        """Count a failure. -> True when it just locked the key."""
        with self._mutex:
            now = self._clock()
            fails = [t for t in self._fails.get(key, []) if now - t < self.window]
            fails.append(now)
            if len(fails) >= self.limit:
                self._locked[key] = now + self.lock
                self._fails.pop(key, None)
                self._prune(now)
                return True
            self._fails[key] = fails
            self._prune(now)
            return False

    def clear(self, key: str) -> None:
        with self._mutex:
            self._fails.pop(key, None)
            self._locked.pop(key, None)

    def reset(self) -> None:
        with self._mutex:
            self._fails.clear()
            self._locked.clear()

    def _prune(self, now: float) -> None:
        if len(self._fails) > 5000:
            for k in [k for k, v in self._fails.items() if not v or now - v[-1] >= self.window]:
                del self._fails[k]


# sign-in (form, local or AD): per name, and per client address
sign_in_by_name = Throttle(limit=5)
sign_in_by_address = Throttle(limit=20)
# forgot password: wrong answers per name, name lookups per address
reset_by_name = Throttle(limit=5)
reset_lookups_by_address = Throttle(limit=10)

ALL = (sign_in_by_name, sign_in_by_address, reset_by_name, reset_lookups_by_address)


def name_key(username: str) -> str:
    """CORP\\nva, nva@corp.local and nva count as one name."""
    name = (username or "").strip().lower()
    name = name.rsplit("\\", 1)[-1]
    return name.split("@", 1)[0]


def wait_message(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    return (f"Too many failed attempts - try again in {minutes} minute{'s' if minutes > 1 else ''}, "
            "or ask an administrator.")

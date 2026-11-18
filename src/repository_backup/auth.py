"""Installation owner credentials, independent of GitHub authorization."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path


class OwnerAuth:
    def __init__(self, state_dir: Path, legacy_token: str = ""):
        self.path = state_dir / "private" / "owner.json"
        self.legacy_token = legacy_token
        self.lock = threading.RLock()
        self.failures = []

    def exists(self) -> bool:
        if self.path.is_symlink():
            raise ValueError("Owner credentials must not be a symlink")
        return self.path.exists()

    @staticmethod
    def derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=64*1024*1024)

    def create(self, password, confirmation) -> None:
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError("Choose an owner password between 12 and 128 characters.")
        if password != confirmation:
            raise ValueError("The passwords do not match.")
        with self.lock:
            if self.exists():
                raise FileExistsError("An owner password is already configured.")
            salt = secrets.token_bytes(16)
            record = {"version": 1, "salt": salt.hex(), "hash": self.derive(password, salt).hex()}
            # O_EXCL prevents a second process from claiming or replacing the owner.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as target:
                    json.dump(record, target)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                self.path.unlink(missing_ok=True)
                raise

    def verify(self, password) -> bool:
        if not isinstance(password, str) or len(password) > 1024:
            return False
        with self.lock:
            now = time.monotonic()
            self.failures = [stamp for stamp in self.failures if stamp > now - 60]
            if len(self.failures) >= 5:
                raise ValueError("Too many sign-in attempts. Wait one minute and try again.")
            if self.exists():
                record = json.loads(self.path.read_text())
                valid = hmac.compare_digest(self.derive(password, bytes.fromhex(record["salt"])).hex(), record["hash"])
            else:
                valid = bool(self.legacy_token) and hmac.compare_digest(password.encode(), self.legacy_token.encode())
            if valid:
                self.failures.clear()
            else:
                self.failures.append(now)
            return valid

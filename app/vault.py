from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.key_path = data_dir / ".credential.key"
        self.secret_dir = data_dir / "credentials"

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secret_dir.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass

    def _fernet(self) -> Fernet:
        self.initialize()
        return Fernet(self.key_path.read_bytes())

    def save(self, name: str, value: str) -> None:
        if not value:
            self.delete(name)
            return
        target = self.secret_dir / f"{name}.token"
        target.write_bytes(self._fernet().encrypt(value.encode("utf-8")))
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    def load(self, name: str) -> str:
        target = self.secret_dir / f"{name}.token"
        if not target.exists():
            return ""
        try:
            return self._fernet().decrypt(target.read_bytes()).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
            return ""

    def delete(self, name: str) -> None:
        target = self.secret_dir / f"{name}.token"
        if target.exists():
            target.unlink()

    def configured(self, name: str) -> bool:
        return bool(self.load(name))

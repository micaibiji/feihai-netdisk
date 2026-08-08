from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    """Encrypt provider credentials at rest with a NAS-local key."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.key_path = data_dir / ".credential.key"
        self.vault_dir = data_dir / "credentials"

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            self.key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass

    def _fernet(self) -> Fernet:
        self.initialize()
        return Fernet(self.key_path.read_bytes().strip())

    @staticmethod
    def _safe_key(key: str) -> str:
        if not key or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for character in key):
            raise ValueError("密钥名称不安全")
        return key

    def save_secret(self, key: str, value: str) -> None:
        target = self.vault_dir / f"{self._safe_key(key)}.token"
        target.write_bytes(self._fernet().encrypt(value.strip().encode("utf-8")))
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    def load_secret(self, key: str) -> str:
        target = self.vault_dir / f"{self._safe_key(key)}.token"
        if not target.exists():
            return ""
        try:
            return self._fernet().decrypt(target.read_bytes()).decode("utf-8")
        except InvalidToken:
            return ""

    def delete_secret(self, key: str) -> None:
        target = self.vault_dir / f"{self._safe_key(key)}.token"
        if target.exists():
            target.unlink()

    def save(self, provider: str, credential: str) -> None:
        if provider not in {"115", "baidu", "quark", "china_mobile"}:
            raise ValueError("未知网盘")
        self.save_secret(provider, credential)

    def load(self, provider: str) -> str:
        return self.load_secret(provider)

    def configured(self, provider: str) -> bool:
        return bool(self.load(provider))

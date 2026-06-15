"""RSA key generation, persistence, and rotation."""

import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class KeyManager:
    """Manages RSA keypair lifecycle: generation, loading, rotation."""

    PRIVATE_KEY_FILE = "private_key.pem"
    PUBLIC_KEY_FILE = "public_key.pem"
    OLD_KEYS_DIR = "old"

    def __init__(self, keys_dir: Path):
        self.keys_dir = Path(keys_dir)
        self.old_keys_dir = self.keys_dir / self.OLD_KEYS_DIR
        self._private_key = None
        self._public_key_pem = None
        self._ensure_keys_exist()

    def _ensure_keys_exist(self):
        """Generate keys if they don't exist, otherwise load them."""
        priv_path = self.keys_dir / self.PRIVATE_KEY_FILE
        pub_path = self.keys_dir / self.PUBLIC_KEY_FILE

        if priv_path.exists() and pub_path.exists():
            self._load_keys(priv_path, pub_path)
        else:
            self._generate_and_save_keys(priv_path, pub_path)

    def _generate_and_save_keys(self, priv_path: Path, pub_path: Path):
        """Generate RSA-2048 keypair and save to disk."""
        self._private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        # Save private key
        priv_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        priv_path.write_bytes(priv_pem)
        os.chmod(priv_path, 0o600)

        # Derive and save public key
        public_key = self._private_key.public_key()
        pub_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        pub_path.write_bytes(pub_pem)
        self._public_key_pem = pub_pem.decode("utf-8")

    def _load_keys(self, priv_path: Path, pub_path: Path):
        """Load existing keypair from disk."""
        self._private_key = serialization.load_pem_private_key(
            priv_path.read_bytes(),
            password=None,
        )
        self._public_key_pem = pub_path.read_text("utf-8")

    @property
    def public_key_pem(self) -> str:
        """Return the current public key as a PEM string."""
        if self._public_key_pem is None:
            pub_path = self.keys_dir / self.PUBLIC_KEY_FILE
            self._public_key_pem = pub_path.read_text("utf-8")
        return self._public_key_pem

    @property
    def private_key(self):
        """Return the current private key object."""
        return self._private_key

    def rotate_keys(self) -> str:
        """Rotate keys: archive current keys, generate new ones.

        Returns the new public key PEM.
        """
        self.old_keys_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Move current keys to old directory
        priv_path = self.keys_dir / self.PRIVATE_KEY_FILE
        pub_path = self.keys_dir / self.PUBLIC_KEY_FILE

        if priv_path.exists():
            priv_path.rename(self.old_keys_dir / f"{timestamp}_{self.PRIVATE_KEY_FILE}")
        if pub_path.exists():
            pub_path.rename(self.old_keys_dir / f"{timestamp}_{self.PUBLIC_KEY_FILE}")

        # Generate new keys
        self._generate_and_save_keys(
            self.keys_dir / self.PRIVATE_KEY_FILE,
            self.keys_dir / self.PUBLIC_KEY_FILE,
        )

        return self.public_key_pem

    def decrypt_session_key(self, encrypted_key_b64: str) -> bytes:
        """Decrypt an AES session key using the RSA private key."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        import base64

        encrypted_key = base64.b64decode(encrypted_key_b64)
        return self._private_key.decrypt(
            encrypted_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

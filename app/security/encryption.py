"""Application-layer encryption using hybrid RSA + AES-256-GCM.

Flow:
  Client: AES-GCM(payload, session_key, nonce) → ciphertext
  Client: RSA-OAEP(session_key, server_public_key) → encrypted_key
  Server: RSA-OAEP-decrypt(encrypted_key, server_private_key) → session_key
  Server: AES-GCM-decrypt(ciphertext, session_key, nonce) → payload
  Server: … process …
  Server: AES-GCM(response_json, session_key, new_nonce) → encrypted response
"""

import base64
import json
import os
from typing import Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_session_key() -> bytes:
    """Generate a random 256-bit AES key."""
    return os.urandom(32)


def generate_nonce() -> bytes:
    """Generate a random 96-bit nonce for AES-GCM."""
    return os.urandom(12)


def encrypt_payload(plaintext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns ciphertext with auth tag appended."""
    aesgcm = AESGCM(key)
    return aesgcm.encrypt(nonce, plaintext, None)


def decrypt_payload(ciphertext: bytes, key: bytes, nonce: bytes) -> bytes:
    """Decrypt AES-256-GCM ciphertext. Raises InvalidTag on tampering."""
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


def encrypt_request(request_dict: dict, public_key_pem: str) -> dict:
    """Encrypt a request dict for sending to the server.

    Args:
        request_dict: The JSON-serializable request payload.
        public_key_pem: Server's RSA public key in PEM format.

    Returns:
        Dict with 'encrypted_key', 'encrypted_payload', 'nonce' — all base64-encoded.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))

    session_key = generate_session_key()
    nonce = generate_nonce()

    plaintext = json.dumps(request_dict).encode("utf-8")
    ciphertext = encrypt_payload(plaintext, session_key, nonce)

    encrypted_key = public_key.encrypt(
        session_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    return {
        "encrypted_key": base64.b64encode(encrypted_key).decode("utf-8"),
        "encrypted_payload": base64.b64encode(ciphertext).decode("utf-8"),
        "nonce": base64.b64encode(nonce).decode("utf-8"),
    }


def decrypt_request(encrypted_body: dict, key_manager) -> dict:
    """Decrypt an encrypted request body on the server side.

    Args:
        encrypted_body: Dict with 'encrypted_key', 'encrypted_payload', 'nonce' (base64 strings).
        key_manager: KeyManager instance for RSA decryption.

    Returns:
        Decrypted request as a dict.
    """
    encrypted_key_b64 = encrypted_body["encrypted_key"]
    encrypted_payload_b64 = encrypted_body["encrypted_payload"]
    nonce_b64 = encrypted_body["nonce"]

    session_key = key_manager.decrypt_session_key(encrypted_key_b64)
    ciphertext = base64.b64decode(encrypted_payload_b64)
    nonce = base64.b64decode(nonce_b64)

    plaintext = decrypt_payload(ciphertext, session_key, nonce)
    return json.loads(plaintext.decode("utf-8")), session_key


def encrypt_response(response_dict: dict, session_key: bytes) -> dict:
    """Encrypt a response dict for sending back to the client.

    Args:
        response_dict: The JSON-serializable response payload.
        session_key: The AES session key from the request.

    Returns:
        Dict with 'encrypted_payload' and 'nonce' — both base64-encoded.
    """
    nonce = generate_nonce()
    plaintext = json.dumps(response_dict).encode("utf-8")
    ciphertext = encrypt_payload(plaintext, session_key, nonce)

    return {
        "encrypted_payload": base64.b64encode(ciphertext).decode("utf-8"),
        "nonce": base64.b64encode(nonce).decode("utf-8"),
    }

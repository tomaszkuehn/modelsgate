#!/usr/bin/env python3
"""
Test client for the AI Model Backend.

Demonstrates the full encryption flow:
  1. Fetch server's RSA public key
  2. Build a task-based request (task_type + messages)
  3. Encrypt with hybrid RSA + AES-256-GCM
  4. Send to /api/v1/request
  5. Decrypt and display the response

Usage:
  python test_client.py [--server http://localhost:8000] [--task chat_with_context]
  python test_client.py --task vision_describe
  python test_client.py --task image_compare
  python test_client.py --legacy --model gpt-4o   # old-style backward compat

Requirements: pip install requests cryptography
"""

import argparse
import base64
import json
import os
import sys
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ── Task types ───────────────────────────────────────────────────────────

TASK_TYPES = [
    "chat_with_context",
    "image_compare",
    "image_edit",
    "image_generate",
    "vision_describe",
    "vision_qa",
]

# ── Placeholder images (1x1 pixel PNGs in base64) ──
PLACEHOLDER_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PLACEHOLDER_PNG2 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)


class BackendClient:
    """Client for the AI Model Backend with application-layer encryption."""

    def __init__(self, server_url: str):
        self.server_url = server_url.rstrip("/")
        self._public_key = None

    def fetch_public_key(self):
        """Fetch the server's RSA public key."""
        resp = requests.get(f"{self.server_url}/api/v1/public-key")
        resp.raise_for_status()
        data = resp.json()
        print(f"[*] Fetched public key ({data['key_size']}-bit, {data['algorithm']})")
        self._public_key = serialization.load_pem_public_key(
            data["public_key"].encode("utf-8")
        )

    def encrypt_request(self, payload: dict) -> dict:
        """Encrypt a request payload using hybrid RSA + AES-256-GCM."""
        session_key = os.urandom(32)
        nonce = os.urandom(12)

        plaintext = json.dumps(payload).encode("utf-8")
        aesgcm = AESGCM(session_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)

        encrypted_key = self._public_key.encrypt(
            session_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

        self._session_key = session_key
        return {
            "encrypted_key": base64.b64encode(encrypted_key).decode("utf-8"),
            "encrypted_payload": base64.b64encode(ciphertext).decode("utf-8"),
            "nonce": base64.b64encode(nonce).decode("utf-8"),
        }

    def decrypt_response(self, encrypted: dict) -> dict:
        """Decrypt an encrypted response from the server."""
        ciphertext = base64.b64decode(encrypted["encrypted_payload"])
        nonce = base64.b64decode(encrypted["nonce"])
        aesgcm = AESGCM(self._session_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))

    def send_task_request(
        self,
        task_type: str,
        messages: list,
        model_override: Optional[str] = None,
        parameters: Optional[dict] = None,
    ) -> dict:
        """Send a task-based request (new style — uses task_type)."""
        if self._public_key is None:
            self.fetch_public_key()

        payload = {
            "task_type": task_type,
            "messages": messages,
        }
        if model_override:
            payload["model"] = model_override
        if parameters:
            payload["parameters"] = parameters

        encrypted = self.encrypt_request(payload)
        resp = requests.post(
            f"{self.server_url}/api/v1/request",
            json=encrypted,
        )
        resp.raise_for_status()
        return self.decrypt_response(resp.json())

    def send_legacy_request(
        self,
        model: str,
        messages: list,
        parameters: Optional[dict] = None,
    ) -> dict:
        """Send an old-style request (backward compat — model-only, no task_type)."""
        if self._public_key is None:
            self.fetch_public_key()

        payload = {
            "model": model,
            "messages": messages,
        }
        if parameters:
            payload["parameters"] = parameters

        encrypted = self.encrypt_request(payload)
        resp = requests.post(
            f"{self.server_url}/api/v1/request",
            json=encrypted,
        )
        resp.raise_for_status()
        return self.decrypt_response(resp.json())


def make_text_message(text: str, role: str = "user") -> dict:
    """Build a text-only message."""
    return {"role": role, "content": [{"type": "text", "text": text}]}


def make_vision_message(text: str, image_b64: str, role: str = "user") -> dict:
    """Build a message with text + one image."""
    return {
        "role": role,
        "content": [
            {"type": "text", "text": text},
            {"type": "image", "image": image_b64},
        ],
    }


def make_multi_vision_message(text: str, image1_b64: str, image2_b64: str, role: str = "user") -> dict:
    """Build a message with text + two images."""
    return {
        "role": role,
        "content": [
            {"type": "text", "text": text},
            {"type": "image", "image": image1_b64},
            {"type": "image", "image": image2_b64},
        ],
    }


# ── Task → messages builder ──────────────────────────────────────────────

def build_messages_for_task(task_type: str) -> list:
    """Build appropriate messages based on the task type."""
    if task_type == "chat_with_context":
        return [
            make_text_message("What is the capital of France? Answer in one sentence."),
            {"role": "assistant", "content": [{"type": "text", "text": "Paris."}]},
            make_text_message("What is its approximate population?"),
        ]
    elif task_type == "vision_describe":
        return [
            make_vision_message(
                "Describe what you see in this image in detail.",
                PLACEHOLDER_PNG,
            )
        ]
    elif task_type == "vision_qa":
        return [
            make_vision_message(
                "What is the main subject of this image? Answer briefly.",
                PLACEHOLDER_PNG,
            )
        ]
    elif task_type == "image_compare":
        return [
            make_multi_vision_message(
                "Compare these two images. What are the key differences and similarities?",
                PLACEHOLDER_PNG,
                PLACEHOLDER_PNG2,
            )
        ]
    elif task_type == "image_generate":
        return [
            make_text_message(
                "Generate an image of a serene mountain lake at sunset with pine trees."
            )
        ]
    elif task_type == "image_edit":
        return [
            make_vision_message(
                "Add a rainbow to this image.",
                PLACEHOLDER_PNG,
            )
        ]
    return [make_text_message("Hello!")]


def main():
    parser = argparse.ArgumentParser(
        description="Test client for AI Model Backend",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python test_client.py --task chat_with_context\n"
            "  python test_client.py --task vision_describe\n"
            "  python test_client.py --task image_compare\n"
            "  python test_client.py --legacy --model gpt-4o-mini\n"
        ),
    )
    parser.add_argument("--server", default="http://localhost:8000", help="Backend server URL")
    parser.add_argument(
        "--task", choices=TASK_TYPES, default="chat_with_context",
        help="Task type to test (default: chat_with_context)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model alias override (optional — backend picks default for task if omitted)",
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="Use old-style model-only request (no task_type) for backward compat test",
    )
    args = parser.parse_args()

    client = BackendClient(args.server)

    # Check server health
    try:
        health = requests.get(f"{args.server}/")
        print(f"[*] Server status: {health.json()}")
    except Exception as e:
        print(f"[!] Cannot reach server at {args.server}: {e}")
        print("[!] Start the server first: uvicorn app.main:app --reload")
        sys.exit(1)

    # Build messages based on task
    messages = build_messages_for_task(args.task)

    if args.legacy:
        # ── Backward compat mode ──
        model = args.model or "gpt-4o-mini"
        print(f"\n--- Legacy Request (model-only) ---")
        print(f"[*] Model: {model}")
        print(f"[*] Task:  {args.task} (inferred by server as chat_with_context)")
        print(f"[*] Messages: {len(messages)}")

        try:
            response = client.send_legacy_request(model, messages)
        except Exception as e:
            print(f"[!] Error: {e}")
            sys.exit(1)
    else:
        # ── New task-based mode ──
        model_info = f" (override: {args.model})" if args.model else " (auto)"
        print(f"\n--- Task Request: {args.task}{model_info} ---")
        print(f"[*] Task:     {args.task}")
        print(f"[*] Model:    {args.model or 'auto-selected by backend'}")
        print(f"[*] Messages: {len(messages)}")

        try:
            response = client.send_task_request(
                task_type=args.task,
                messages=messages,
                model_override=args.model,
            )
        except Exception as e:
            print(f"[!] Error: {e}")
            sys.exit(1)

    # ── Display response ──
    print("\n--- Response ---")
    print(f"ID:        {response.get('id')}")
    print(f"Task type: {response.get('task_type', 'N/A')}")
    print(f"Model:     {response.get('model')}")

    if response.get("error"):
        print(f"ERROR:     {response['error']}")
        print("\n[!] This is expected if no API keys are configured.")
        print("[!] Set provider API keys in .env and restart the server.")
    else:
        for block in response.get("content", []):
            if block["type"] == "text":
                print(f"Text:      {block['text'][:500]}")
            elif block["type"] == "image":
                print(f"Image:     <base64, {len(block['image'])} chars>")

        usage = response.get("usage")
        if usage:
            print(
                f"Tokens:    {usage.get('total_tokens', 0)} "
                f"(prompt: {usage.get('prompt_tokens', 0)}, "
                f"completion: {usage.get('completion_tokens', 0)})"
            )


if __name__ == "__main__":
    main()

"""API playground — build/send encrypted requests."""

from app.admin.routes_pkg._shared import (
    router, templates, settings,
    get_current_admin,
    get_stats_summary, get_usage_by_model, get_usage_by_day,
    get_usage_by_task_type, get_recent_requests,
    get_distinct_task_types, get_client_stats, get_routing_failures,
)

from fastapi import Request, Depends
from fastapi.responses import HTMLResponse
# === API Playground ===

@router.get("/playground", response_class=HTMLResponse)
async def playground_page(
    request: Request,
    admin: str = Depends(get_current_admin),
):
    """API testing playground — build and send requests, see responses."""
    from app.models.registry import ModelRegistry

    registry = ModelRegistry()
    models = [{"name": n, "provider": c.provider} for n, c in registry.list_models().items()]

    from app.api.schemas import TaskType
    return templates.TemplateResponse(
        "playground.html",
        {
            "request": request,
            "task_types": [t.value for t in TaskType],
            "models": models,
            "public_key": request.app.state.key_manager.public_key_pem[:100] + "...",
        },
    )


@router.get("/playground/clients")
async def playground_clients(
    request: Request,
    admin=Depends(get_current_admin),
):
    """Return current client list as JSON for the playground dropdown."""
    from app.database import async_session
    from app.stats.models import Client, ClientGroup
    from sqlalchemy import select as _sel

    async with async_session() as session:
        clients = (await session.execute(
            _sel(Client).where(Client.is_active == True).order_by(Client.registered_at.desc()).limit(50)
        )).scalars().all()
        groups = (await session.execute(_sel(ClientGroup))).scalars().all()
        group_map = {g.id: g.group_key for g in groups}

    return [
        {
            "key": c.client_key,
            "plan": c.plan,
            "group": group_map.get(c.client_group_id, "default"),
        }
        for c in clients
    ]


@router.post("/playground/send")
async def playground_send(
    request: Request,
    admin=Depends(get_current_admin),
):
    """Proxy: encrypts a plain request, sends it to the API, decrypts the response.

    Accepts JSON body with: task_type, messages, client_id, and optional
    model, output_type, plan_tier, cost_class, preferred_provider, parameters.

    Returns: {original, encrypted_envelope, response}
    """
    import json as _json
    from app.security.encryption import encrypt_request
    from app.security.keys import KeyManager

    body = await request.json()
    payload = {
        "task_type": body.get("task_type", "chat_with_context"),
        "messages": body.get("messages", []),
        "client_id": body.get("client_id"),
    }
    for opt in ("model", "output_type", "plan_tier", "cost_class", "preferred_provider"):
        if body.get(opt):
            payload[opt] = body[opt]
    if body.get("parameters"):
        payload["parameters"] = body["parameters"]

    key_manager: KeyManager = request.app.state.key_manager

    # Encrypt with a known session key so we can decrypt the response
    import os as _os, base64 as _b64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    session_key = _os.urandom(32)
    nonce = _os.urandom(12)
    plaintext = _json.dumps(payload).encode("utf-8")
    aesgcm = AESGCM(session_key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    pubkey = serialization.load_pem_public_key(key_manager.public_key_pem.encode())
    encrypted_key = pubkey.encrypt(
        session_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )

    envelope = {
        "encrypted_key": _b64.b64encode(encrypted_key).decode(),
        "encrypted_payload": _b64.b64encode(ciphertext).decode(),
        "nonce": _b64.b64encode(nonce).decode(),
    }

    # Send to internal API
    import httpx
    decrypted = None
    async with httpx.AsyncClient() as client:
        api_resp = await client.post(
            f"{settings.public_url}/api/v1/request",
            json=envelope,
            timeout=120.0,
        )
        if api_resp.status_code == 200:
            enc_resp = api_resp.json()
            resp_nonce = _b64.b64decode(enc_resp["nonce"])
            resp_ct = _b64.b64decode(enc_resp["encrypted_payload"])
            decrypted = _json.loads(aesgcm.decrypt(resp_nonce, resp_ct, None))
        else:
            decrypted = {"error": f"HTTP {api_resp.status_code}", "detail": api_resp.text}

    return {
        "original": payload,
        "encrypted_envelope": envelope,
        "response": decrypted,
    }
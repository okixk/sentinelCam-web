from __future__ import annotations

import json
import logging
from typing import Optional

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
)

from app.config import settings

log = logging.getLogger("sentinelCam.webauthn")

# Temporary in-memory challenge store (session-based in practice)
# Key: session_id or username, Value: challenge bytes
_pending_challenges: dict[str, bytes] = {}


def store_challenge(key: str, challenge: bytes) -> None:
    _pending_challenges[key] = challenge


def pop_challenge(key: str) -> Optional[bytes]:
    return _pending_challenges.pop(key, None)


def get_rp_id(request_host: str) -> str:
    rp_id = settings.webauthn_rp_id
    if rp_id and rp_id != "localhost":
        return rp_id
    # Use request host without port
    host = request_host.split(":")[0]
    return host


def generate_registration_options(user_id: int, username: str, existing_credentials: list[bytes], rp_id: str | None = None) -> dict:
    options = webauthn.generate_registration_options(
        rp_id=rp_id or settings.webauthn_rp_id,
        rp_name=settings.webauthn_rp_name,
        user_id=str(user_id).encode(),
        user_name=username,
        user_display_name=username,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=cred_id)
            for cred_id in existing_credentials
        ],
    )
    return json.loads(webauthn.options_to_json(options))


def verify_registration_response(challenge: bytes, response_data: dict, rp_id: str, origin: str) -> dict:
    verification = webauthn.verify_registration_response(
        credential=response_data,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
    )
    return {
        "credential_id": bytes(verification.credential_id),
        "public_key": bytes(verification.credential_public_key),
        "sign_count": verification.sign_count,
    }


def generate_authentication_options(credentials: list[dict], rp_id: str | None = None) -> dict:
    options = webauthn.generate_authentication_options(
        rp_id=rp_id or settings.webauthn_rp_id,
        user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(c["credential_id"]))
            for c in credentials
        ],
    )
    return json.loads(webauthn.options_to_json(options))


def verify_authentication_response(
    challenge: bytes,
    response_data: dict,
    credential_public_key: bytes,
    sign_count: int,
    rp_id: str,
    origin: str,
) -> int:
    verification = webauthn.verify_authentication_response(
        credential=response_data,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=credential_public_key,
        credential_current_sign_count=sign_count,
    )
    return verification.new_sign_count

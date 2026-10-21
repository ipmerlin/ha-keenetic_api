"""KeeneticOS 5.2 interactive authentication (Argon2id / HMAC-SHA3-512).

Protocol reference: hoaxisr/awg-manager, internal/auth/ndw4.go (MIT).
Copyright (c) 2026 hoaxisr. See LICENSES/awg-manager-MIT.txt in this repository.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets


class NDW4ProtocolError(Exception):
    """An unsupported or malformed authentication exchange."""


class NDW4CredentialsError(Exception):
    """Credentials rejected, or the server proof did not match."""


def _hmac(key, message):
    return hmac.digest(key, message.encode('utf-8'), 'sha3_512')


def _xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def _derive_keys(password, salt, iterations, memory):
    # Both the import and the expensive KDF run in a worker, outside HA's loop.
    from argon2.low_level import Type, hash_secret_raw

    key = hash_secret_raw(
        password.encode('utf-8'), salt, time_cost=iterations, memory_cost=memory,
        parallelism=1, hash_len=64, type=Type.ID, version=19,
    )
    client_key = _hmac(key, 'NDW4 Interactive Client Key')
    return client_key, hashlib.sha3_512(client_key).digest(), _hmac(key, 'NDW4 Interactive Server Key')


def _data(response, phase):
    if response.status != 401:
        raise NDW4ProtocolError(f'NDW4 phase {phase}: unexpected HTTP status {response.status}')
    encoded = response.headers.get('X-NDM-Data', '')
    if not encoded:
        if phase == 1:
            raise NDW4CredentialsError('NDW4 login rejected')
        raise NDW4ProtocolError(f'NDW4 phase {phase}: missing exchange data')
    try:
        if len(encoded) > 16384:
            raise ValueError()
        result = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        raise NDW4ProtocolError(f'NDW4 phase {phase}: malformed exchange data') from None


async def authenticate(request, login, password):
    """Perform exactly three POST phases, verifying the router's proof first."""
    client_nonce = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
    params = _data(await request({'login': login, 'nonce': client_nonce}), 1)
    try:
        salt_text = params['salt']
        salt = base64.b64decode(salt_text, validate=True)
        nonce = params['nonce']
        iterations = params['iter']
        memory = params['memcost']
        if (
            len(salt) != 16 or not isinstance(nonce, str) or not 1 <= len(nonce) <= 1024
            or type(iterations) is not int or not 1 <= iterations <= 10
            or type(memory) is not int or not 8 <= memory <= 262144
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError, binascii.Error):
        raise NDW4ProtocolError('NDW4 phase 1: unsupported key derivation parameters') from None

    client_key, stored_key, server_key = await asyncio.to_thread(
        _derive_keys, password, salt, iterations, memory,
    )
    message = (
        f'login1={login},nonce1={client_nonce};'
        f'iter2={iterations},memcost2={memory},nonce2={nonce},salt2={salt_text};'
        f'login3={login},nonce3={nonce}'
    )
    proof = base64.b64encode(_xor(client_key, _hmac(stored_key, message))).decode('ascii')
    signature_text = _data(await request({'login': login, 'nonce': nonce, 'proof': proof}), 2).get('signature')
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise NDW4ProtocolError('NDW4 phase 2: malformed server proof') from None
    if not hmac.compare_digest(signature, _hmac(server_key, message)):
        raise NDW4CredentialsError('NDW4 server proof did not match')

    final_proof = base64.b64encode(_xor(
        client_key, _hmac(stored_key, message + ';signature4=' + signature_text),
    )).decode('ascii')
    response = await request({'login': login, 'nonce': nonce, 'signature-proof': final_proof})
    if response.status in (401, 403):
        raise NDW4CredentialsError('NDW4 final authentication rejected')
    if response.status != 200:
        raise NDW4ProtocolError(f'NDW4 phase 3: unexpected HTTP status {response.status}')
    return True

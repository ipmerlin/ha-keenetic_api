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
import re
from urllib.request import parse_http_list


class NDW4ProtocolError(Exception):
    """An unsupported or malformed authentication exchange."""


class NDW4CredentialsError(Exception):
    """Credentials rejected, or the server proof did not match."""


class NDW4LoginRejected(NDW4CredentialsError):
    """The initial login request was refused, before password verification."""


def get_endpoint(headers):
    """Read the advertised NDW4 path without permitting a different origin."""
    values = headers.getall('WWW-Authenticate', []) if hasattr(headers, 'getall') else [headers.get('WWW-Authenticate', '')]
    for value in values:
        parts = parse_http_list(value)
        for index, part in enumerate(parts):
            match = re.match(r'^x-ndw4-interactive(?=\s|$)', part.strip(), re.IGNORECASE)
            if not match:
                continue
            params = [part.strip()[match.end():]]
            for following in parts[index + 1:]:
                if re.match(r'^[A-Za-z][A-Za-z0-9_-]*\s+', following):
                    break
                params.append(following)
            endpoint_match = re.search(r'\bendpoint\s*=\s*"([^"]*)"', ','.join(params))
            endpoint = endpoint_match.group(1) if endpoint_match else ''
            if not re.fullmatch(r'/(?!/)[A-Za-z0-9._~/-]*', endpoint):
                raise NDW4ProtocolError('NDW4 offer has no safe authentication endpoint')
            return endpoint
    return None


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
            raise NDW4LoginRejected('NDW4 initial login rejected before password verification')
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
        iterations_raw = params['iter']
        memory_raw = params['memcost']
        if any(isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) for value in (iterations_raw, memory_raw)):
            raise ValueError()
        iterations = int(iterations_raw)
        memory = int(memory_raw)
        if (
            len(salt) != 16 or not isinstance(nonce, str) or not 1 <= len(nonce) <= 1024
            or not 1 <= iterations <= 64
            or not 8 <= memory <= 262144
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError, binascii.Error):
        raise NDW4ProtocolError('NDW4 phase 1: unsupported key derivation parameters') from None

    client_key, stored_key, server_key = await asyncio.to_thread(
        _derive_keys, password, salt, iterations, memory,
    )
    message = (
        f'login1={login},nonce1={client_nonce};'
        f'iter2={iterations_raw},memcost2={memory_raw},nonce2={nonce},salt2={salt_text};'
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

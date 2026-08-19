"""
iifl_crypto.py
==============
Reimplementation (in Python) of the client-side crypto used by IIFL's SSO
login pages (idaas.iiflsecurities.com), reverse-engineered from the app's
own JS bundle (`index-CDyuiHt3.js`, functions $W/UW/HW/BW/FW/NW/MW).

This is UNDOCUMENTED / UNOFFICIAL. IIFL can change it at any time without
notice, which would break this module. It was reconstructed by:
  1. Reading the actual JS source functions that build/parse `cEncData`.
  2. Byte-for-byte verifying the envelope layout against a real captured
     `cEncData` payload (confirmed block lengths: pubkey text / 16-byte IV /
     256-byte RSA block / AES ciphertext, in that order).

Scheme
------
Client generates one RSA-2048 keypair per login session (SPKI/PKCS8 DER,
base64-encoded — mirrors `HW()`, which used WebCrypto `generateKey` with
RSA-OAEP just as a key-generation parameter; the keys themselves are
algorithm-agnostic and are actually used with PKCS#1 v1.5 padding below).

To encrypt a JSON payload for the server (mirrors `$W`):
    1. Generate a random 32-byte AES key and 16-byte IV.
    2. AES-256-CBC encrypt the JSON (PKCS7 padded) with that key/IV.
    3. RSA-PKCS1v1.5 encrypt the 32-byte AES key using the *server's*
       public key (obtained from POST /v2/access/get/encKey).
    4. Assemble, big-endian length-prefixed:
         [len][client's own RSA public key, as base64 TEXT bytes]
         [len][IV]
         [len][RSA-encrypted AES key]
         [AES-CBC ciphertext]                (no length prefix - runs to EOF)
    5. Base64-encode the whole thing -> this is the `cEncData` string.

To decrypt a server response (mirrors `UW`), the envelope is the same
shape but block 1 is the *server's* public key (unused, just skip it):
         [len][server public key text]  (skip/ignore)
         [len][IV]
         [len][RSA-encrypted AES key]   (encrypted with the CLIENT's pubkey)
         [AES-CBC ciphertext]
    RSA-PKCS1v1.5 *decrypt* the AES key with the client's own private key,
    then AES-CBC decrypt (and PKCS7-unpad) the payload with that key/IV.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asympad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.backends import default_backend

_BACKEND = default_backend()


# ─────────────────────────────────────────────────────────────────────────
# Keypair generation  (mirrors HW())
# ─────────────────────────────────────────────────────────────────────────

def generate_keypair() -> tuple[str, str, rsa.RSAPrivateKey]:
    """Generate an RSA-2048 keypair.

    Returns (public_key_b64, private_key_b64, private_key_obj):
      - public_key_b64:  base64 of the DER SubjectPublicKeyInfo (SPKI)
      - private_key_b64: base64 of the DER PKCS8 (unencrypted)
    These match the `publicKeyBase64` / `privateKeyBase64` shape produced
    by the browser's `window.crypto.subtle.generateKey` + `exportKey`.
    """
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=_BACKEND)
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_der = priv.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return (
        base64.b64encode(pub_der).decode("ascii"),
        base64.b64encode(priv_der).decode("ascii"),
        priv,
    )


def _load_public_key_from_b64(pub_b64: str) -> rsa.RSAPublicKey:
    der = base64.b64decode(pub_b64)
    return serialization.load_der_public_key(der, backend=_BACKEND)


# ─────────────────────────────────────────────────────────────────────────
# Low-level primitives (mirror NW / FW / their decrypt counterparts)
# ─────────────────────────────────────────────────────────────────────────

def _rsa_encrypt_pkcs1v15(data: bytes, pub_key: rsa.RSAPublicKey) -> bytes:
    return pub_key.encrypt(data, asympad.PKCS1v15())


def _rsa_decrypt_pkcs1v15(data: bytes, priv_key: rsa.RSAPrivateKey) -> bytes:
    return priv_key.decrypt(data, asympad.PKCS1v15())


def _aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv), backend=_BACKEND).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ─────────────────────────────────────────────────────────────────────────
# Envelope assembly / parsing
# ─────────────────────────────────────────────────────────────────────────

def _pack_block(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def client_side_encrypt(payload: dict[str, Any], server_pub_b64: str, client_pub_b64_text: str) -> str:
    """Build the `cEncData` value to send to the server. Mirrors `$W(e, t, r)`
    where e=payload json, t=server_pub_b64, r=client_pub_b64_text."""
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    aes_key = __import__("os").urandom(32)   # confirmed: 32 bytes (AES-256)
    iv = __import__("os").urandom(16)

    ciphertext = _aes_cbc_encrypt(plaintext, aes_key, iv)

    server_pub_key = _load_public_key_from_b64(server_pub_b64)
    enc_aes_key = _rsa_encrypt_pkcs1v15(aes_key, server_pub_key)

    client_pub_text_bytes = client_pub_b64_text.encode("utf-8")

    envelope = (
        _pack_block(client_pub_text_bytes)
        + _pack_block(iv)
        + _pack_block(enc_aes_key)
        + ciphertext
    )
    return base64.b64encode(envelope).decode("ascii")


def client_side_decrypt(c_resp_enc_data_b64: str, client_priv_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    """Decrypt a `cRespEncData` value from the server. Mirrors `UW(e, t)`."""
    raw = base64.b64decode(c_resp_enc_data_b64)
    pos = 0

    def read_block() -> bytes:
        nonlocal pos
        (length,) = struct.unpack_from(">I", raw, pos)
        pos += 4
        block = raw[pos:pos + length]
        pos += length
        return block

    read_block()   # server pub key text - discarded, mirrors JS which ignores it too
    iv = read_block()
    enc_aes_key = read_block()
    ciphertext = raw[pos:]

    aes_key = _rsa_decrypt_pkcs1v15(enc_aes_key, client_priv_key)
    plaintext = _aes_cbc_decrypt(ciphertext, aes_key, iv)
    return json.loads(plaintext.decode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────
# Self-test: round-trip using our own generated keys (does NOT validate
# against IIFL's server - just proves the envelope/crypto code is internally
# consistent, matching the byte layout we verified against a real capture).
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client_pub_b64, client_priv_b64, client_priv_obj = generate_keypair()
    server_pub_b64, server_priv_b64, server_priv_obj = generate_keypair()

    payload = {"userId": "TESTUSER", "password": "hunter2", "deviceId": "abc-123"}
    enc = client_side_encrypt(payload, server_pub_b64, client_pub_b64)
    print("cEncData length (b64 chars):", len(enc))

    # Simulate server decrypting the request with its own private key,
    # then building a response envelope back using the CLIENT's public key
    # (this is what the server does; we replicate it here purely to prove
    # our encrypt+decrypt functions are mutually consistent).
    raw = base64.b64decode(enc)
    pos = 0
    (l1,) = struct.unpack_from(">I", raw, pos); pos += 4
    pos += l1
    (l2,) = struct.unpack_from(">I", raw, pos); pos += 4
    iv = raw[pos:pos + l2]; pos += l2
    (l3,) = struct.unpack_from(">I", raw, pos); pos += 4
    enc_key = raw[pos:pos + l3]; pos += l3
    ciphertext = raw[pos:]

    recovered_key = _rsa_decrypt_pkcs1v15(enc_key, server_priv_obj)
    recovered_plain = json.loads(_aes_cbc_decrypt(ciphertext, recovered_key, iv))
    assert recovered_plain == payload, "round-trip encrypt/decrypt MISMATCH"
    print("Round-trip OK:", recovered_plain)

    resp_payload = {"status": "Ok", "result": {"token": "abc123"}}
    resp_enc = client_side_encrypt(resp_payload, client_pub_b64, server_pub_b64)
    decoded = client_side_decrypt(resp_enc, client_priv_obj)
    assert decoded == resp_payload, "response decrypt MISMATCH"
    print("Response decrypt OK:", decoded)
    print("\nAll self-tests passed.")

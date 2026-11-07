from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Signer:
    """Makes signed JWTs and the matching JWKS, like an identity provider would."""

    def __init__(self):
        self.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.ec = ec.generate_private_key(ec.SECP256R1())

    def jwks(self):
        n = self.rsa.public_key().public_numbers()
        e = self.ec.public_key().public_numbers()
        i = lambda v, ln=None: b64(v.to_bytes(ln or (v.bit_length() + 7) // 8, "big"))   # noqa: E731
        return {"keys": [{"kty": "RSA", "kid": "r1", "use": "sig", "alg": "RS256", "n": i(n.n), "e": i(n.e)},
                         {"kty": "EC", "kid": "e1", "use": "sig", "alg": "ES256", "crv": "P-256", "x": i(e.x, 32), "y": i(e.y, 32)}]}

    def sign(self, claims, alg="RS256", kid=None, secret=None):
        header = {"alg": alg, "typ": "JWT", **({"kid": kid} if kid else {})}
        si = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}".encode()
        if alg == "RS256":
            sig = self.rsa.sign(si, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256":
            r, s = utils.decode_dss_signature(self.ec.sign(si, ec.ECDSA(hashes.SHA256())))
            sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        elif alg == "HS256":
            import hashlib
            import hmac
            sig = hmac.new(secret.encode(), si, hashlib.sha256).digest()
        else:
            sig = b""
        return f"{si.decode()}.{b64(sig)}"


def claims(**kw):
    now = int(time.time())
    return {"iss": "https://idp.test/app/", "aud": "stormtracker", "sub": "u1", "exp": now + 300, "iat": now, "nonce": "N", **kw}

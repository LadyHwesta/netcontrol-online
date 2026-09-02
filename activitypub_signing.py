"""
NetControl Online — ActivityPub HTTP Signatures (issue follow-up)

Hand-rolled against `cryptography` rather than a PyPI signature package
(see the plan for this feature) -- the Cavage-draft signing scheme
Mastodon and most of the fediverse actually require is narrow and stable:
RSA-SHA256 over a deterministic, newline-joined signing string built from
a handful of request headers. That's the whole surface here.

Two call sites use this module:
  - activitypub_delivery.py, signing OUR outbound requests (Create/Accept
    activities delivered to a follower's inbox) with an org's own private
    key.
  - routers/activitypub.py's inbox handler, verifying an INBOUND request's
    signature against the sending actor's publicKeyPem (fetched fresh via
    httpx -- see that file for why there's no actor-document cache here).
"""

import base64
import hashlib
import logging
from email.utils import formatdate

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_log = logging.getLogger("ham_net_tracker.activitypub")

SIGNED_HEADERS = "(request-target) host date digest content-type"


def generate_keypair() -> tuple[str, str]:
    """RSA-2048, PKCS#8 PEM for the private key (broadest compatibility --
    see the plan's HTTP Signatures section), SubjectPublicKeyInfo PEM for
    the public key. Called once, the first time an org enables Fediverse
    participation (routers/orgs.py) -- never regenerated after that."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def http_date() -> str:
    """RFC 7231 HTTP-date, e.g. 'Sun, 30 Aug 2026 12:00:00 GMT' -- Mastodon
    requires the Date header be in this exact format and within a small
    clock-skew tolerance of its own clock."""
    return formatdate(timeval=None, localtime=False, usegmt=True)


def digest_header(body: bytes) -> str:
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()


def _signing_string(method: str, path: str, host: str, date: str, digest: str, content_type: str = "application/activity+json") -> str:
    return (
        f"(request-target): {method.lower()} {path}\n"
        f"host: {host}\n"
        f"date: {date}\n"
        f"digest: {digest}\n"
        f"content-type: {content_type}"
    )


def sign_request(private_key_pem: str, method: str, path: str, host: str, date: str, body: bytes, key_id: str) -> tuple[str, str]:
    """Returns (digest_header_value, signature_header_value) for a signed
    outbound POST to a remote inbox. Caller sends Host/Date/Digest/
    Content-Type/Signature exactly as given -- the signing string is
    recomputed from these same values on delivery, so don't reserialize
    the body differently between digest computation and the actual send."""
    digest = digest_header(body)
    signing_string = _signing_string(method, path, host, date, digest)
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = private_key.sign(signing_string.encode(), padding.PKCS1v15(), hashes.SHA256())
    sig_b64 = base64.b64encode(signature).decode()
    signature_header = (
        f'keyId="{key_id}",algorithm="rsa-sha256",'
        f'headers="{SIGNED_HEADERS}",signature="{sig_b64}"'
    )
    return digest, signature_header


def _parse_signature_header(value: str) -> dict:
    """Parses `keyId="...",algorithm="...",headers="...",signature="..."`
    into a dict -- deliberately tolerant of field order and extra fields
    (real-world senders vary), but every value here is quoted per spec."""
    parts = {}
    for chunk in value.split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        k, _, v = chunk.partition("=")
        parts[k.strip()] = v.strip().strip('"')
    return parts


def verify_signature(headers: dict, method: str, path: str, public_key_pem: str) -> bool:
    """Verifies an inbound request's Signature header against the sending
    actor's public key. `headers` must be the actual received request
    headers (lowercase keys) -- the signing string is rebuilt from
    whatever `headers=` list the sender declared, using THOSE header
    values, not recomputed independently. Returns False (never raises) on
    any malformed/missing/mismatched input -- callers 401 on False."""
    sig_header = headers.get("signature")
    if not sig_header:
        return False
    parsed = _parse_signature_header(sig_header)
    signature_b64 = parsed.get("signature")
    signed_headers = parsed.get("headers", "(request-target)").split()
    if not signature_b64 or not signed_headers:
        return False

    lines = []
    for h in signed_headers:
        if h == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        else:
            value = headers.get(h)
            if value is None:
                return False
            lines.append(f"{h}: {value}")
    signing_string = "\n".join(lines)

    try:
        signature = base64.b64decode(signature_b64)
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        public_key.verify(signature, signing_string.encode(), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception as exc:
        _log.info("Inbound ActivityPub signature verification failed: %s", exc)
        return False

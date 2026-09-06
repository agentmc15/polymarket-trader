"""Kalshi Trade API v2 request signing (PLAN.md §3 "Kalshi", T12).

Source: https://docs.kalshi.com (Trade API v2 authentication). The facts
below were fetched ONCE by the architect on 2026-09-04 and pinned in
PLAN.md §3; GUARDRAILS.md §1.4 forbids re-fetching them from task code,
so this module cites the pin rather than the live page.

Every authenticated Kalshi request carries three headers:

  * ``KALSHI-ACCESS-KEY``       — the API key id (a public identifier)
  * ``KALSHI-ACCESS-TIMESTAMP`` — the current time in MILLISECONDS since
    the Unix epoch, as a decimal string (NOT seconds — a seconds-valued
    timestamp is ~1000x too small and the venue rejects it)
  * ``KALSHI-ACCESS-SIGNATURE`` — base64 of an RSA-PSS signature over
    ``f"{timestamp_ms}{METHOD}{path}"``

where ``path`` is the request's URL path WITHOUT its query string and
INCLUDING the ``/trade-api/v2`` prefix — e.g. a request to
``https://external-api.kalshi.com/trade-api/v2/markets?status=open``
signs the string ``"1767225600000GET/trade-api/v2/markets"``. Signing the
query string as well would produce a signature the venue cannot verify.

Signature parameters (all pinned, all mandatory — a mismatch on any one
produces a signature that verifies as invalid rather than a helpful
error): RSA-PSS padding, SHA-256 digest, MGF1 with SHA-256, and a salt
length equal to the digest length (32 bytes for SHA-256).

GUARDRAILS.md §1.3: nothing here reads, prints, or logs the private key.
The key is passed in as an already-loaded object (or a PEM the caller
sourced from `Settings.kalshi_private_key_pem`, a `SecretStr`); no
function in this module returns, formats, or logs key material, and the
`ValueError` raised by `load_private_key` deliberately carries no part of
the PEM it failed to parse.
"""
import base64
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.utils.time import utcnow

#: Header carrying the (public) API key id.
ACCESS_KEY_HEADER = "KALSHI-ACCESS-KEY"

#: Header carrying the request timestamp, in MILLISECONDS since the epoch.
ACCESS_TIMESTAMP_HEADER = "KALSHI-ACCESS-TIMESTAMP"

#: Header carrying the base64 RSA-PSS signature.
ACCESS_SIGNATURE_HEADER = "KALSHI-ACCESS-SIGNATURE"

#: The digest used for both the signature itself and MGF1 (PLAN.md §3).
#: A single instance is not reused across calls — `cryptography`'s hash
#: objects are cheap and constructing fresh ones per call avoids any
#: question of shared state; this constant records the CHOICE, and
#: `_SALT_LENGTH` below derives the salt length from it so the two can
#: never drift apart.
_DIGEST_SIZE_BYTES = hashes.SHA256().digest_size

#: Salt length in bytes: equal to the digest length (32 for SHA-256),
#: per PLAN.md §3. `cryptography` also spells this
#: `padding.PSS.DIGEST_LENGTH`; the explicit integer is used so the value
#: is visible at the call site and so verification in tests can assert
#: the exact same length (`padding.PSS.AUTO` would accept ANY salt length
#: and would therefore not actually prove this parameter is right).
_SALT_LENGTH = _DIGEST_SIZE_BYTES


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Load an unencrypted RSA private key from PEM text.

    Args:
        pem: PEM-encoded private key (``-----BEGIN PRIVATE KEY-----`` …).
            Sourced from `Settings.kalshi_private_key_pem`, a `SecretStr`
            — the caller unwraps it immediately before this call and
            never logs it (GUARDRAILS.md §1.3).

    Returns:
        rsa.RSAPrivateKey: The loaded key, ready for `sign_request`.

    Raises:
        ValueError: If `pem` is empty, is not a parseable private key, or
            is not an RSA key (Kalshi's scheme is RSA-PSS specifically —
            an Ed25519 or EC key would load fine and then fail to sign,
            so it is rejected here where the message can say why). The
            message never includes any part of `pem`.
    """
    if not pem.strip():
        raise ValueError("Kalshi private key PEM is empty")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        # Deliberately does not chain `exc`'s message into ours: some
        # backends include surrounding key bytes in their error text.
        raise ValueError("Kalshi private key PEM could not be parsed") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            f"Kalshi requires an RSA private key, got {type(key).__name__}"
        )
    return key


def sign_request(
    private_key: rsa.RSAPrivateKey, timestamp_ms: int, method: str, path: str
) -> str:
    """Sign one Kalshi request, returning the base64 signature.

    The signed message is ``f"{timestamp_ms}{METHOD}{path}"`` where
    `METHOD` is upper-cased and `path` has had any query string (and any
    scheme/host, if a full URL was passed) removed. See the module
    docstring for the pinned parameters.

    Args:
        private_key: The account's RSA private key.
        timestamp_ms: Request time in MILLISECONDS since the Unix epoch.
            Must match the `KALSHI-ACCESS-TIMESTAMP` header exactly —
            `auth_headers` guarantees that by generating both from one
            value.
        method: HTTP method, any case (`"get"` and `"GET"` sign the same).
        path: Request path INCLUDING the `/trade-api/v2` prefix, e.g.
            `"/trade-api/v2/portfolio/balance"`. A full URL or a path
            with a query string is accepted and reduced to its path
            component — signing the query string would produce an invalid
            signature (PLAN.md §3).

    Returns:
        str: Base64-encoded RSA-PSS signature, ready for the
            `KALSHI-ACCESS-SIGNATURE` header.
    """
    signed_path = urlsplit(path).path
    message = f"{timestamp_ms}{method.upper()}{signed_path}"
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=_SALT_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def auth_headers(
    key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    url: str,
    timestamp_ms: int | None = None,
) -> dict[str, str]:
    """Build the three Kalshi auth headers for one request.

    Args:
        key_id: The API key id (public identifier), e.g. `"test-key-id"`
            in fixtures (GUARDRAILS.md §1.3 — never a real credential).
        private_key: The account's RSA private key.
        method: HTTP method, any case.
        url: The request URL or path. Its path component (query string
            stripped) is what gets signed; pass the FULL, already-built
            request URL so the signed path can never drift from the path
            actually sent.
        timestamp_ms: Override the request timestamp, in milliseconds.
            Defaults to `utcnow()` in milliseconds. Present so tests can
            pin a timestamp; production always leaves it `None`.

    Returns:
        dict[str, str]: `{KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP,
            KALSHI-ACCESS-SIGNATURE}`. The timestamp is milliseconds as a
            decimal string, and the signature is over that exact same
            value.
    """
    if timestamp_ms is None:
        timestamp_ms = int(utcnow().timestamp() * 1000)
    signature = sign_request(private_key, timestamp_ms, method, url)
    return {
        ACCESS_KEY_HEADER: key_id,
        ACCESS_TIMESTAMP_HEADER: str(timestamp_ms),
        ACCESS_SIGNATURE_HEADER: signature,
    }

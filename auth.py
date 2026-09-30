"""Supabase user-JWT verification (reusable FastAPI dependency).

Used by the `upload_pending` mode of POST /jobs. The user id ALWAYS comes from the
verified token's `sub` claim, never from a request body. Designed so the legacy
`POST /jobs` / `GET /jobs/{id}` paths can adopt `get_current_user` behind a flag later
(nothing here is wired to them today).

Key material is configured from the environment (read at call time, so tests and
key rotations do not need an import-time reload):

  SUPABASE_JWT_SECRET       HS256 shared secret (Dashboard -> Settings -> API -> JWT
                            secret; the "legacy" signing key). Enables HS256 tokens.
  SUPABASE_JWT_JWKS_URL     JWKS endpoint for asymmetric signing keys (ES256/RS256),
                            e.g. https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
                            Enables ES256/RS256 tokens. If unset but
                            SUPABASE_JWT_USE_JWKS=true, it is derived from SUPABASE_URL.
  SUPABASE_JWT_AUDIENCE     default "authenticated"
  SUPABASE_JWT_ISSUER       default "<SUPABASE_URL>/auth/v1" when SUPABASE_URL is set;
                            set to the empty string to skip the issuer check.
  SUPABASE_JWT_LEEWAY_SECONDS  clock-skew leeway, default 10

Both key types may be enabled at once (useful while a project rotates from the legacy
secret to asymmetric keys): the token header's `alg` selects the key, and only the
algorithms whose key is configured are accepted (`none` is never accepted). With
neither configured every request fails closed with 503.
"""
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import jwt
from fastapi import Header

HS_ALGS = ('HS256',)
ASYM_ALGS = ('ES256', 'RS256')


class AuthError(Exception):
    """Carries the HTTP status to answer with. Messages never contain the token."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass
class AuthenticatedUser:
    user_id: str
    email: Optional[str] = None
    claims: Dict[str, Any] = field(default_factory=dict, repr=False)


_jwks_clients: Dict[str, 'jwt.PyJWKClient'] = {}


def _jwks_url() -> Optional[str]:
    url = os.getenv('SUPABASE_JWT_JWKS_URL', '').strip()
    if url:
        return url
    if os.getenv('SUPABASE_JWT_USE_JWKS', '').strip().lower() in ('1', 'true', 'yes', 'on'):
        base = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
        if base:
            return f'{base}/auth/v1/.well-known/jwks.json'
    return None


def _jwks_client(url: str) -> 'jwt.PyJWKClient':
    client = _jwks_clients.get(url)
    if client is None:
        # PyJWKClient caches the key set (1 h) and refetches on an unknown `kid`.
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=10)
        _jwks_clients[url] = client
    return client


def _issuer() -> Optional[str]:
    if 'SUPABASE_JWT_ISSUER' in os.environ:
        return os.environ['SUPABASE_JWT_ISSUER'].strip() or None
    base = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
    return f'{base}/auth/v1' if base else None


def verify_supabase_jwt(token: str) -> AuthenticatedUser:
    """Verify a Supabase access token and return the user. Raises AuthError."""
    secret = os.getenv('SUPABASE_JWT_SECRET', '')
    jwks_url = _jwks_url()
    if not secret and not jwks_url:
        raise AuthError(503, 'Authentication is not configured on the server')
    if not token:
        raise AuthError(401, 'Missing bearer token')

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise AuthError(401, 'Invalid token') from None
    alg = header.get('alg')

    if alg in HS_ALGS and secret:
        key: Any = secret
    elif alg in ASYM_ALGS and jwks_url:
        try:
            key = _jwks_client(jwks_url).get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError:
            raise AuthError(503, 'Unable to fetch signing keys') from None
        except jwt.PyJWTError:
            raise AuthError(401, 'Invalid token') from None
    else:
        # Includes alg=none and algorithm-confusion attempts.
        raise AuthError(401, 'Invalid token')

    issuer = _issuer()
    audience = os.getenv('SUPABASE_JWT_AUDIENCE', 'authenticated')
    try:
        claims = jwt.decode(
            token, key, algorithms=[alg], audience=audience, issuer=issuer,
            leeway=float(os.getenv('SUPABASE_JWT_LEEWAY_SECONDS', '10') or 10),
            options={'require': ['exp', 'sub', 'aud']},
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, 'Token expired') from None
    except jwt.PyJWTError:
        raise AuthError(401, 'Invalid token') from None

    if claims.get('role') not in (None, 'authenticated'):
        raise AuthError(401, 'Invalid token')
    try:
        user_id = str(uuid.UUID(str(claims['sub'])))
    except ValueError:
        raise AuthError(401, 'Invalid token') from None
    email = claims.get('email')
    return AuthenticatedUser(user_id=user_id, email=email if isinstance(email, str) else None, claims=claims)


def bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise AuthError(401, 'Missing bearer token')
    scheme, _, value = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not value.strip():
        raise AuthError(401, 'Missing bearer token')
    return value.strip()


def authenticate_header(authorization: Optional[str]) -> AuthenticatedUser:
    return verify_supabase_jwt(bearer_token(authorization))


def get_current_user(authorization: Optional[str] = Header(None)) -> AuthenticatedUser:
    """FastAPI dependency. Sync on purpose: FastAPI runs it in a worker thread, so a
    JWKS fetch never blocks the event loop. Raises HTTPException-compatible errors via
    the registered AuthError handler (see main.py)."""
    return authenticate_header(authorization)

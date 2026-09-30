"""Supabase user-JWT verification (reusable FastAPI dependency).

Used by POST /jobs (upload_pending mode always; legacy mode per AUTH_MODE) and by
GET /jobs/{id} (per AUTH_MODE). The user id ALWAYS comes from the verified token, never
from a request body.

ONE mechanism, two ways to check a token (chosen by configuration, never mixed per request):

  1. Local verification, when SUPABASE_JWT_SECRET (HS256) and/or a JWKS URL (ES256/RS256)
     is configured. No network call per request.
  2. Remote verification, when neither is configured (the default on the VPS today):
     `GET {SUPABASE_URL}/auth/v1/user` with the user's bearer token, answers 200 + the user
     for a valid, non-revoked session. Results are cached for 60 s (bounded by the token's
     own `exp`) so a client polling every 5 s costs one upstream call per minute. Same
     approach as the `openai-proxy` Edge Function's `auth.getUser`. Set
     SUPABASE_AUTH_REMOTE_VERIFY=false to disable it (then an unconfigured server answers
     503, failing closed, as before).

AUTH_MODE (legacy endpoints only; upload_pending is always authenticated):
  off      ignore tokens entirely (previous behaviour)
  log      DEFAULT. Requests without a valid token are allowed and counted/logged (no PII);
           the response to GET /jobs/{id} hides `audio_url`. A VALID token of another user is
           always refused. This keeps old app builds (no Authorization header) working.
  enforce  401 without a valid token, 404 unless the job belongs to the token's user.

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
import hashlib
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx
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
    if not token:
        raise AuthError(401, 'Missing bearer token')
    if not secret and not jwks_url:
        if remote_verify_enabled():
            return verify_remote(token)
        raise AuthError(503, 'Authentication is not configured on the server')

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


# ---------------------------------------------------------------------------
# Remote verification (GET {SUPABASE_URL}/auth/v1/user) with a short cache
# ---------------------------------------------------------------------------
REMOTE_CACHE_SECONDS = 60
REMOTE_CACHE_MAX_ENTRIES = 2048
_remote_http = httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0))
_remote_cache: Dict[str, tuple] = {}   # sha256(token) -> (valid_until_monotonic, AuthenticatedUser)
_remote_lock = threading.Lock()


def remote_verify_enabled() -> bool:
    return os.getenv('SUPABASE_AUTH_REMOTE_VERIFY', 'true').strip().lower() not in ('0', 'false', 'no', 'off')


def _unverified_claims(token: str) -> Dict[str, Any]:
    """Claims WITHOUT signature verification: only used to reject junk cheaply and to bound
    the cache lifetime. Identity always comes from Supabase's answer, never from here."""
    try:
        jwt.get_unverified_header(token)
        return jwt.decode(token, options={'verify_signature': False, 'verify_exp': False, 'verify_aud': False})
    except jwt.PyJWTError:
        raise AuthError(401, 'Invalid token') from None


def verify_remote(token: str) -> AuthenticatedUser:
    """Verify a user token with Supabase Auth. Raises AuthError(401) for a bad/expired token,
    AuthError(503) when Supabase cannot be reached (never cached, fails closed)."""
    claims = _unverified_claims(token)   # garbage never costs an upstream call
    exp = claims.get('exp')
    now = time.time()
    if not isinstance(exp, (int, float)) or exp <= now:
        raise AuthError(401, 'Token expired' if isinstance(exp, (int, float)) else 'Invalid token')

    key = hashlib.sha256(token.encode()).hexdigest()
    with _remote_lock:
        cached = _remote_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

    base = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
    api_key = os.getenv('SUPABASE_ANON_KEY', '').strip() or os.getenv('SUPABASE_SERVICE_KEY', '').strip()
    if not base or not api_key:
        raise AuthError(503, 'Authentication is not configured on the server')
    try:
        response = _remote_http.get(f'{base}/auth/v1/user', headers={'Authorization': f'Bearer {token}', 'apikey': api_key})
    except httpx.HTTPError:
        raise AuthError(503, 'Authentication service unavailable') from None
    if response.status_code in (401, 403):
        raise AuthError(401, 'Invalid token')
    if response.status_code != 200:
        raise AuthError(503, 'Authentication service unavailable')
    try:
        body = response.json()
        user_id = str(uuid.UUID(str(body['id'])))
    except (ValueError, KeyError, TypeError):
        raise AuthError(503, 'Authentication service returned an unexpected answer') from None
    if body.get('role') not in (None, 'authenticated'):
        raise AuthError(401, 'Invalid token')
    email = body.get('email')
    user = AuthenticatedUser(user_id=user_id, email=email if isinstance(email, str) else None, claims={})

    ttl = max(0.0, min(REMOTE_CACHE_SECONDS, exp - now))
    with _remote_lock:
        if len(_remote_cache) >= REMOTE_CACHE_MAX_ENTRIES:
            mono = time.monotonic()
            for stale in [k for k, (until, _) in _remote_cache.items() if until <= mono]:
                _remote_cache.pop(stale, None)
            while len(_remote_cache) >= REMOTE_CACHE_MAX_ENTRIES:
                _remote_cache.pop(next(iter(_remote_cache)), None)
        _remote_cache[key] = (time.monotonic() + ttl, user)
    return user


def clear_remote_cache() -> None:
    with _remote_lock:
        _remote_cache.clear()


# ---------------------------------------------------------------------------
# AUTH_MODE for the legacy endpoints
# ---------------------------------------------------------------------------
AUTH_MODES = ('off', 'log', 'enforce')
DEFAULT_AUTH_MODE = 'log'
_warned_mode: set = set()


def auth_mode() -> str:
    """AUTH_MODE=off|log|enforce, default log. Read per call so it can change with a restart only
    (env), and tests can patch it. An unknown value falls back to the default, with a warning."""
    value = os.getenv('AUTH_MODE', '').strip().lower()
    if not value:
        return DEFAULT_AUTH_MODE
    if value not in AUTH_MODES:
        if value not in _warned_mode:
            _warned_mode.add(value)
            print(f'⚠️ [auth] unknown AUTH_MODE "{value[:20]}", using "{DEFAULT_AUTH_MODE}" (valid: off, log, enforce)')
        return DEFAULT_AUTH_MODE
    return value


@dataclass
class AuthResult:
    user: Optional[AuthenticatedUser] = None
    problem: Optional[str] = None     # None | 'missing' | 'invalid' | 'unavailable'


def resolve_user(authorization: Optional[str]) -> AuthResult:
    """Never raises. Blocking (may call Supabase): run it in a worker thread."""
    if not authorization or not authorization.strip():
        return AuthResult(problem='missing')
    try:
        return AuthResult(user=authenticate_header(authorization))
    except AuthError as error:
        if error.status_code == 503:
            return AuthResult(problem='unavailable')
        return AuthResult(problem='missing' if error.detail == 'Missing bearer token' else 'invalid')


# Counters for the rollout ("how many requests still come without a token?"). No ids, no IPs.
auth_counters: Counter = Counter()
_counter_lock = threading.Lock()
LOG_FIRST = 5
LOG_EVERY = 50


def note_request(endpoint: str, outcome: str) -> None:
    """Count one legacy-endpoint request (outcome: authenticated|missing|invalid|unavailable) and
    log a summary line for the first few and then every LOG_EVERY-th non-authenticated request."""
    with _counter_lock:
        auth_counters[f'{endpoint}:{outcome}'] += 1
        n = auth_counters[f'{endpoint}:{outcome}']
        summary = {k.split(':', 1)[1]: v for k, v in auth_counters.items() if k.startswith(f'{endpoint}:')}
    if outcome != 'authenticated' and (n <= LOG_FIRST or n % LOG_EVERY == 0):
        print(f'🔐 [auth] mode={auth_mode()} {endpoint}: {outcome} (this kind so far: {n}); totals {summary}', flush=True)

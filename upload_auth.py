"""Authenticate only through Supabase Auth's verified user endpoint."""
from uuid import UUID
from fastapi import HTTPException

def verify_upload_user(authorization: str | None, get_user=None) -> str:
    if not authorization or not authorization.startswith('Bearer ') or not authorization[7:].strip():
        raise HTTPException(401, detail={'code':'unauthenticated'})
    if get_user is None:
        from supabase_client import supabase
        get_user = supabase.auth.get_user
    try:
        user = get_user(authorization[7:]).user
        if user is None or getattr(user, 'is_anonymous', False): raise ValueError('Unverified user')
        return str(UUID(str(user.id)))
    except Exception:
        raise HTTPException(401, detail={'code':'unauthenticated'}) from None

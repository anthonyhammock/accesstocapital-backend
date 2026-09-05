import os
from datetime import datetime, timedelta

from fastapi import Header, HTTPException
from jose import jwt, JWTError

# Signs and verifies the session token issued at login. Every data-access
# endpoint depends on get_current_user_id instead of trusting a
# client-supplied user_id — closing the gap where any caller could read or
# write another user's data just by passing a different id.
#
# No insecure fallback here on purpose: a hardcoded default secret would be
# public (it's in git history) the moment this file is committed, so if
# JWT_SECRET were ever unset in production the service would silently sign
# and verify tokens with a secret anyone can find — letting them forge a
# valid token for any user_id. Failing to start is the correct behavior for
# a missing signing secret; set JWT_SECRET even for local runs.
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 14  # 2 weeks


def create_access_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_user_id(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header.")

    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session. Please sign in again.")

    # A calendar-OAuth state token (see create_oauth_state_token below) is
    # signed with this same secret and also carries a "sub" claim, since it
    # has to survive a round trip through Google's/Microsoft's redirect as
    # a single opaque string. Without this check it would double as a valid
    # session token for its whole (short) lifetime — and unlike a normal
    # session token, it travels as a URL query parameter on the OAuth
    # callback, which is exactly the kind of value that ends up in access
    # logs. Only a token with no "purpose" claim is a real session token.
    if "purpose" in payload:
        raise HTTPException(status_code=401, detail="Invalid session token.")

    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid session token.")


OAUTH_STATE_EXPIRE_MINUTES = 10


def create_oauth_state_token(user_id: int, provider: str) -> str:
    """Carries identity + CSRF protection across a calendar OAuth redirect.
    The callback is a plain browser navigation from Google/Microsoft, so it
    can't carry our normal Authorization header — this signed, short-lived,
    provider-pinned token (round-tripped as the OAuth `state` param) is how
    the callback knows which user it's for without trusting the request
    itself, and the provider pin stops a state token issued for one
    provider's flow from being replayed against the other's callback."""
    expire = datetime.utcnow() + timedelta(minutes=OAUTH_STATE_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": str(user_id), "provider": provider, "purpose": "calendar_oauth_state", "exp": expire},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def verify_oauth_state_token(token: str, expected_provider: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="This connection request has expired. Please try connecting again.")

    if payload.get("purpose") != "calendar_oauth_state" or payload.get("provider") != expected_provider:
        raise HTTPException(status_code=400, detail="Invalid connection request.")

    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid connection request.")

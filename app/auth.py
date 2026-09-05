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

    try:
        return int(payload["sub"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid session token.")

"""
authenticate_reporting_request(request) -- Story 3.1's reporting-endpoint
auth check.

Replicates units-backend's real JWT validation faithfully
(`utilities/decorator.py:is_request_authenticated`, confirmed by
investigation, spec Intent): signature + expiry (PyJWT, HS256, the shared
`JWT_SECRET_KEY`) AND the DB-backed single-token match against
`UserProfileRef.token` (a revocation/single-session check units-backend's
own decorator also performs) AND the underlying `auth_user.is_active` check
-- never a stateless-only check, and never units-backend's own `user_id`
claim as the profile lookup key (the real mechanism resolves by `email`).

Post-review fix: resolves via `AuthUserRef.email` (the related `User`'s
own email), not `UserProfileRef.email` -- units-backend's real lookup is
`UserProfile.objects.filter(user__email=...)`, i.e. the related User's
email, and `UserProfile` has its own separate, independently-settable
`email` field that can diverge from it. Filtering on `UserProfileRef.email`
directly was a confirmed bug (would silently resolve the wrong profile if
the two ever differ for a given user).

Deliberately simplified from the real decorator (human-confirmed, not
fixed): status codes are flattened to a uniform 401 for every rejection
reason (units-backend itself varies 404/403/401 by reason), and the
password-expiry check (`is_password_expire`) is not replicated -- both
accepted as reasonable simplifications for this internal-facing reporting
auth helper, not oversights.

Finance never imports units-backend's Python code (`utilities.decorator`,
`user_service.models`, etc.) -- this module re-implements the same sequence
against Finance's own unmanaged `*Ref` models (AD-19, spec Never).
"""
import jwt
from django.conf import settings

from ledger.models import AuthUserRef, UserProfileRef

# Rejection reasons -- returned as the second tuple element on failure, used
# by callers (and tests) to distinguish *why* a request was rejected without
# string-matching a human-readable message. Every reason maps to 401 (spec
# I/O matrix: every rejection row in this story is a 401).
REASON_MISSING_TOKEN = "missing_token"
REASON_EXPIRED_TOKEN = "expired_token"
REASON_INVALID_TOKEN = "invalid_token"
REASON_PROFILE_NOT_FOUND = "profile_not_found"
REASON_TOKEN_MISMATCH = "token_mismatch"
REASON_INACTIVE_USER = "inactive_user"


def _extract_bearer_token(request):
    """Extract the raw token string from an `Authorization: Bearer <token>`
    header. Returns None if the header is missing or malformed."""
    header = request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer":
        return None
    return parts[1]


def authenticate_reporting_request(request):
    """Authenticate a Finance reporting request.

    Returns `(user_profile_ref, None)` on success, or `(None, reason)` on
    failure, where `reason` is one of the `REASON_*` constants above (every
    failure is a 401, per the spec's I/O matrix -- callers translate the
    reason into `prepare_response(..., status=401)`).
    """
    token = _extract_bearer_token(request)
    if not token:
        return None, REASON_MISSING_TOKEN

    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        return None, REASON_EXPIRED_TOKEN
    except jwt.DecodeError:
        return None, REASON_INVALID_TOKEN
    except jwt.InvalidTokenError:
        # Catch-all for any other PyJWT decode failure (e.g. malformed
        # payload) -- reject with 401 before any query runs, matching the
        # spec's Boundaries & Constraints ("on any decode failure, reject
        # with 401 before any query runs").
        return None, REASON_INVALID_TOKEN

    user_email = payload.get("email")
    if not user_email:
        # A token missing the email claim entirely -- reject outright
        # rather than filtering on email=None, which could otherwise match
        # an unrelated profile whose email is also null.
        return None, REASON_INVALID_TOKEN

    # Resolve via AuthUserRef.email (the related User's own email), NOT
    # UserProfileRef.email -- units-backend's real lookup is
    # `UserProfile.objects.filter(user__email=...)` (post-review fix, see
    # module docstring).
    auth_user_ref = AuthUserRef.objects.filter(email=user_email).first()
    if not auth_user_ref:
        return None, REASON_PROFILE_NOT_FOUND

    user_profile_ref = UserProfileRef.objects.filter(
        user_id=auth_user_ref.id
    ).first()
    if not user_profile_ref:
        return None, REASON_PROFILE_NOT_FOUND

    if user_profile_ref.token != token:
        # Revocation/single-session check -- replicates
        # `utilities/decorator.py`'s `user_profile.token != token` branch
        # exactly (spec Intent, Design Notes).
        return None, REASON_TOKEN_MISMATCH

    if not auth_user_ref.is_active:
        return None, REASON_INACTIVE_USER

    return user_profile_ref, None

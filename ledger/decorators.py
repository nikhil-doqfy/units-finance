"""
@require_internal_token -- the shared-secret guard for every endpoint under
Finance's /internal/ namespace (AD-7).

The token check happens before any view logic runs, and does not rely on
Docker network isolation as the trust boundary. This is the first auth guard
in Finance and the pattern every later Epic 2 /internal/ endpoint reuses.
"""
import hmac
from functools import wraps

from django.conf import settings

from ledger.response_envelope import prepare_response


def require_internal_token(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not settings.FINANCE_INTERNAL_TOKEN:
            # Fail loud on a misconfigured environment rather than letting
            # every request fall through to a generic "invalid token" 403 --
            # an unset FINANCE_INTERNAL_TOKEN is an operator error, not a
            # caller error.
            return prepare_response(
                content={},
                message="FINANCE_INTERNAL_TOKEN is not configured",
                status=500,
            )

        token = request.headers.get("X-Internal-Token")
        if not token:
            return prepare_response(
                content={}, message="Missing X-Internal-Token", status=401
            )
        if not hmac.compare_digest(token, settings.FINANCE_INTERNAL_TOKEN):
            return prepare_response(
                content={}, message="Invalid X-Internal-Token", status=403
            )
        return view_func(request, *args, **kwargs)

    return wrapper

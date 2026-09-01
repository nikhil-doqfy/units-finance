"""
Finance's own minimal {content, message, status} response envelope.

Matches units-backend's prepare_response() shape (see
microservices/units-backend/utilities/helper_functions.py:42-59) but is
implemented fresh here -- Finance is a separate Django project and does not
import units-backend code (AD-1, inherited platform AD-3).

Story 2.1b is Finance's first endpoint, so this only needs the plain
{content, message, status} shape for now; later Epic 3 reporting stories may
extend this with the paginator argument units-backend's version supports,
if/when a paginated Finance endpoint needs it.
"""
from rest_framework.response import Response


def prepare_response(content=None, message="", status=200):
    if content is None:
        content = {}
    return Response(
        {
            "content": content,
            "message": message,
            "status": status,
        },
        status=status,
    )

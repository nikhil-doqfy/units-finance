"""
Finance's own minimal {content, message, status} response envelope.

Matches units-backend's prepare_response() shape (see
microservices/units-backend/utilities/helper_functions.py:42-59) but is
implemented fresh here -- Finance is a separate Django project and does not
import units-backend code (AD-1, inherited platform AD-3).

Story 2.1b was Finance's first endpoint, needing only the plain
{content, message, status} shape. Story 3.5 (Ageing report) extends this
with the `paginator`/`total_records` argument pair this module's own
docstring already anticipated -- Ageing is the only one of the four Epic 3
reports that paginates (Structural Seed, inherited platform AD-3). When
`paginator` is given, a sibling top-level `pagination` key is added to the
response -- never nested inside `content` (spec Never) -- adapting the
platform precedent's exact field-name shape
(`utilities/helper_functions.py:42-59`: `has_previous`, `has_next`,
`previous_page_number`, `next_page_number`, `page_number`, `total_records`)
while still returning DRF's `Response`, never `JsonResponse` (spec Never).
"""
from rest_framework.response import Response


def prepare_response(content=None, message="", status=200, paginator=None, total_records=0):
    if content is None:
        content = {}
    body = {
        "content": content,
        "message": message,
        "status": status,
    }
    if paginator is not None:
        body["pagination"] = {
            "has_previous": paginator.has_previous(),
            "has_next": paginator.has_next(),
            "previous_page_number": (
                paginator.previous_page_number() if paginator.has_previous() else None
            ),
            "next_page_number": (
                paginator.next_page_number() if paginator.has_next() else None
            ),
            "page_number": paginator.number,
            "total_records": total_records,
        }
    return Response(body, status=status)

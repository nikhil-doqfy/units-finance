"""
Story 2.1b: the receiving end of units-backend's internal sync channel
(Story 2.1a, `microservices/units-backend/lease/finance_sync.py`).

`sync_lease_transaction` is a function-based @api_view (AD-1) guarded by
@require_internal_token (AD-7). It proves the authenticated channel and the
{content, message, status} envelope end-to-end with a bare acknowledgment --
it does not post any JournalEntry or touch the Ledger (Stories 2.2+ own all
posting logic).
"""
from rest_framework.decorators import api_view

from ledger.decorators import require_internal_token
from ledger.response_envelope import prepare_response


@api_view(["POST"])
@require_internal_token
def sync_lease_transaction(request, lease_transaction_id):
    body_id = request.data.get("lease_transaction_id")

    if body_id is not None:
        try:
            body_id = int(body_id)
        except (TypeError, ValueError):
            return prepare_response(
                content={"body_lease_transaction_id": body_id},
                message="lease_transaction_id in the request body is not a valid integer",
                status=400,
            )

    if body_id is not None and body_id != lease_transaction_id:
        return prepare_response(
            content={
                "path_lease_transaction_id": lease_transaction_id,
                "body_lease_transaction_id": body_id,
            },
            message="lease_transaction_id in URL path does not match the request body",
            status=400,
        )

    return prepare_response(
        content={"lease_transaction_id": lease_transaction_id},
        message="Lease transaction sync acknowledged",
        status=200,
    )

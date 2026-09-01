"""
Story 2.1b: the receiving end of units-backend's internal sync channel
(Story 2.1a, `microservices/units-backend/lease/finance_sync.py`).

`sync_lease_transaction` is a function-based @api_view (AD-1) guarded by
@require_internal_token (AD-7). Story 2.2b wires the actual posting engine
in here in place of the previous bare acknowledgment: a RENT-type
LeaseTransaction (`cheque_type == RENT_CHEQUE`) now triggers `post_rent_ar`;
any other cheque_type still just acknowledges (no posting -- out of scope
for this story).
"""
from rest_framework.decorators import api_view

from ledger.decorators import require_internal_token
from ledger.posting import RENT_CHEQUE, post_rent_ar
from ledger.models import LeaseTransactionRef
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

    txn = LeaseTransactionRef.objects.filter(pk=lease_transaction_id).first()
    posting_result = None
    if txn is not None and txn.cheque_type == RENT_CHEQUE:
        # Unresolvable-PMC and duplicate-skip are both non-5xx, logged
        # outcomes (spec Boundaries & Constraints) -- a resolution failure
        # is a Finance-side data/config issue, not a caller error, so the
        # sync endpoint always returns a normal response either way.
        # Pass the already-fetched txn through so post_rent_ar doesn't
        # re-query the same row a second time (post-review patch).
        posting_result = post_rent_ar(lease_transaction_id, txn=txn)

    return prepare_response(
        content={
            "lease_transaction_id": lease_transaction_id,
            **({"posting": posting_result} if posting_result is not None else {}),
        },
        message="Lease transaction sync acknowledged",
        status=200,
    )

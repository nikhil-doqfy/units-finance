"""
Story 2.1b: the receiving end of units-backend's internal sync channel
(Story 2.1a, `microservices/units-backend/lease/finance_sync.py`).

`sync_lease_transaction` is a function-based @api_view (AD-1) guarded by
@require_internal_token (AD-7). Story 2.2b wired the Rent AR posting engine
in here in place of the previous bare acknowledgment: a RENT-type
LeaseTransaction (`cheque_type == RENT_CHEQUE`) triggers `post_rent_ar`.
Story 2.3 adds `post_cheque_clearing` alongside it -- both checks run on
every sync call, each independently no-oping when its own trigger condition
isn't met (deliberately not gated on cheque_type, per Spec Change Log).
"""
from rest_framework.decorators import api_view

from ledger.decorators import require_internal_token
from ledger.posting import RENT_CHEQUE, post_cheque_clearing, post_rent_ar
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
    posting_results = {}
    if txn is not None:
        # Unresolvable-PMC and duplicate-skip are both non-5xx, logged
        # outcomes (spec Boundaries & Constraints) -- a resolution failure
        # is a Finance-side data/config issue, not a caller error, so the
        # sync endpoint always returns a normal response either way.
        # Pass the already-fetched txn through so neither posting function
        # re-queries the same row a second time.
        if txn.cheque_type == RENT_CHEQUE:
            posting_results["rent_ar"] = post_rent_ar(lease_transaction_id, txn=txn)

        # Deliberately NOT gated on cheque_type (Spec Change Log) -- runs on
        # every sync regardless of cheque_type; the existing prior-
        # JournalEntry check inside post_cheque_clearing is the sole gate.
        posting_results["cheque_clearing"] = post_cheque_clearing(
            lease_transaction_id, txn=txn
        )

    return prepare_response(
        content={
            "lease_transaction_id": lease_transaction_id,
            **({"posting": posting_results} if posting_results else {}),
        },
        message="Lease transaction sync acknowledged",
        status=200,
    )

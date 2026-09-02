"""
Story 2.1b: the receiving end of units-backend's internal sync channel
(Story 2.1a, `microservices/units-backend/lease/finance_sync.py`).

`sync_lease_transaction` is a function-based @api_view (AD-1) guarded by
@require_internal_token (AD-7). Story 2.2b wired the Rent AR posting engine
in here in place of the previous bare acknowledgment: a RENT-type
LeaseTransaction (`cheque_type == RENT_CHEQUE`) triggers `post_rent_ar`.
Story 2.3 adds `post_cheque_clearing` alongside it, and Story 2.4 adds
`post_bounce_reversal` alongside both -- all three checks run on every sync
call, each independently no-oping when its own trigger condition isn't met
(deliberately not gated on cheque_type, per Spec Change Log). Story 2.5 adds
`post_bounce_fee` alongside all three -- also not gated on cheque_type at
the view level (its own `cheque_type == OTHER_CHARGE` check lives inside
the function itself, same pattern as the others' internal gates).

Story 2.6 adds `sync_lease`, a SEPARATE view parallel to
`sync_lease_transaction` (not merged into it -- different source model,
different units-backend signal). It receives units-backend's second-ever
signal (a `post_save` on `Lease`, not `LeaseTransaction`) and calls
`post_security_deposit`.

Story 2.7 adds `post_commission_split`, called from `sync_lease_transaction`
immediately after `post_rent_ar` -- but ONLY when `post_rent_ar`'s own
result is `{"posted": True}` (spec Boundaries & Constraints). Unlike
Story 2.3-2.5's checks, this is not run unconditionally on every sync: it
piggybacks entirely on a successful rent AR post in the same call.
"""
from rest_framework.decorators import api_view

from ledger.decorators import require_internal_token
from ledger.posting import (
    RENT_CHEQUE,
    post_bounce_fee,
    post_bounce_reversal,
    post_cheque_clearing,
    post_commission_split,
    post_rent_ar,
    post_security_deposit,
)
from ledger.models import LeaseRef, LeaseTransactionRef
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
            rent_ar_result = post_rent_ar(lease_transaction_id, txn=txn)
            posting_results["rent_ar"] = rent_ar_result

            # Story 2.7: runs immediately after a successful rent AR post,
            # same gate (cheque_type == RENT_CHEQUE), only when post_rent_ar
            # itself posted -- this story never runs its own independent
            # PMC/CoA resolution failure path separately from post_rent_ar's
            # (spec Boundaries & Constraints, Never).
            if rent_ar_result.get("posted"):
                posting_results["commission_split"] = post_commission_split(
                    lease_transaction_id, txn=txn
                )

        # Deliberately NOT gated on cheque_type (Spec Change Log) -- runs on
        # every sync regardless of cheque_type; the existing prior-
        # JournalEntry check inside post_cheque_clearing is the sole gate.
        posting_results["cheque_clearing"] = post_cheque_clearing(
            lease_transaction_id, txn=txn
        )

        # Story 2.4: also NOT gated on cheque_type (same precedent) -- the
        # sole gate is status == BOUNCED plus post_bounce_reversal's own
        # prior-posting/idempotency checks.
        posting_results["bounce_reversal"] = post_bounce_reversal(
            lease_transaction_id, txn=txn
        )

        # Story 2.5: runs on every sync regardless of cheque_type -- its own
        # cheque_type == OTHER_CHARGE / charge_id-not-null gate lives inside
        # post_bounce_fee itself (spec Boundaries & Constraints).
        posting_results["bounce_fee"] = post_bounce_fee(
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


@api_view(["POST"])
@require_internal_token
def sync_lease(request, lease_id):
    body_id = request.data.get("lease_id")

    if body_id is not None:
        try:
            body_id = int(body_id)
        except (TypeError, ValueError):
            return prepare_response(
                content={"body_lease_id": body_id},
                message="lease_id in the request body is not a valid integer",
                status=400,
            )

    if body_id is not None and body_id != lease_id:
        return prepare_response(
            content={
                "path_lease_id": lease_id,
                "body_lease_id": body_id,
            },
            message="lease_id in URL path does not match the request body",
            status=400,
        )

    lease = LeaseRef.objects.filter(pk=lease_id).first()
    posting_results = {}
    if lease is not None:
        # Same non-5xx, logged-outcome contract as sync_lease_transaction --
        # unresolvable-PMC and duplicate-skip (and "not active"/"no deposit
        # amount") are all Finance-side data/steady-state outcomes, not
        # caller errors.
        posting_results["security_deposit"] = post_security_deposit(
            lease_id, lease=lease
        )

    return prepare_response(
        content={
            "lease_id": lease_id,
            **({"posting": posting_results} if posting_results else {}),
        },
        message="Lease sync acknowledged",
        status=200,
    )

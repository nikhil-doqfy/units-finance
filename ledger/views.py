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
import datetime

from rest_framework.decorators import api_view

from ledger.auth import authenticate_reporting_request
from ledger.decorators import require_internal_token
from ledger.models import FinancePMCProfile, LeaseRef, LeaseTransactionRef
from ledger.org_scope import get_pmc_ids_for_user_profile
from ledger.posting import (
    RENT_CHEQUE,
    post_bounce_fee,
    post_bounce_reversal,
    post_cheque_clearing,
    post_commission_split,
    post_rent_ar,
    post_security_deposit,
)
from ledger.reports import compute_balance_sheet, compute_profit_loss, compute_trial_balance
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


def _parse_iso_date(value):
    """Parse an ISO 8601 date string (YYYY-MM-DD); returns None if missing
    or unparseable (spec: invalid/missing date range -> 400, before any
    query runs)."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@api_view(["GET"])
def trial_balance_report(request):
    """Story 3.2: `GET /reports/trial-balance`.

    `pmc_id`, `start_date`, `end_date` are required query params.
    Ordering, per spec Boundaries & Constraints:
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id`/`start_date`/`end_date` -- 400 on failure,
         before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Aggregate via `compute_trial_balance` and respond.
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")
    start_date_str = request.query_params.get("start_date")
    end_date_str = request.query_params.get("end_date")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    start_date = _parse_iso_date(start_date_str)
    end_date = _parse_iso_date(end_date_str)

    if pmc_id is None or start_date is None or end_date is None:
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "start_date": start_date_str,
                "end_date": end_date_str,
            },
            message="pmc_id, start_date, and end_date are required "
            "(start_date/end_date must be valid ISO 8601 dates)",
            status=400,
        )

    if start_date > end_date:
        # Post-review patch: an inverted range (start after end) would
        # otherwise silently run a query that always returns zero-activity
        # accounts rather than surfacing the caller's mistake.
        return prepare_response(
            content={
                "start_date": start_date_str,
                "end_date": end_date_str,
            },
            message="start_date must not be after end_date",
            status=400,
        )

    finance_pmc_profile = FinancePMCProfile.objects.filter(pmc_id=pmc_id).first()
    if finance_pmc_profile is None:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="No FinancePMCProfile exists for the given pmc_id",
            status=404,
        )

    reachable_pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
    if pmc_id not in reachable_pmc_ids:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="You are not authorized to view this PMC's reports",
            status=403,
        )

    trial_balance = compute_trial_balance(finance_pmc_profile, start_date, end_date)

    return prepare_response(
        content={
            "pmc_id": pmc_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "accounts": trial_balance["accounts"],
            "balanced": trial_balance["balanced"],
        },
        message="Trial Balance report generated",
        status=200,
    )


@api_view(["GET"])
def profit_loss_report(request):
    """Story 3.3: `GET /reports/profit-loss`.

    `pmc_id`, `start_date`, `end_date` are required query params. Reuses the
    exact same request-handling sequence as `trial_balance_report` (spec
    Boundaries & Constraints):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id`/`start_date`/`end_date` (including the
         inverted-range check) -- 400 on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Aggregate via `compute_profit_loss` (which itself calls
         `compute_trial_balance` -- never re-derives the aggregation query
         independently, spec Never) and respond.
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")
    start_date_str = request.query_params.get("start_date")
    end_date_str = request.query_params.get("end_date")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    start_date = _parse_iso_date(start_date_str)
    end_date = _parse_iso_date(end_date_str)

    if pmc_id is None or start_date is None or end_date is None:
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "start_date": start_date_str,
                "end_date": end_date_str,
            },
            message="pmc_id, start_date, and end_date are required "
            "(start_date/end_date must be valid ISO 8601 dates)",
            status=400,
        )

    if start_date > end_date:
        return prepare_response(
            content={
                "start_date": start_date_str,
                "end_date": end_date_str,
            },
            message="start_date must not be after end_date",
            status=400,
        )

    finance_pmc_profile = FinancePMCProfile.objects.filter(pmc_id=pmc_id).first()
    if finance_pmc_profile is None:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="No FinancePMCProfile exists for the given pmc_id",
            status=404,
        )

    reachable_pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
    if pmc_id not in reachable_pmc_ids:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="You are not authorized to view this PMC's reports",
            status=403,
        )

    profit_loss = compute_profit_loss(finance_pmc_profile, start_date, end_date)

    return prepare_response(
        content={
            "pmc_id": pmc_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "income_accounts": profit_loss["income_accounts"],
            "expense_accounts": profit_loss["expense_accounts"],
            "net_profit_loss": profit_loss["net_profit_loss"],
        },
        message="Profit & Loss report generated",
        status=200,
    )


@api_view(["GET"])
def balance_sheet_report(request):
    """Story 3.4: `GET /reports/balance-sheet`.

    `pmc_id` and `as_of_date` are required query params -- exactly one date,
    no `start_date`/`end_date` pair (spec Boundaries & Constraints: a
    Balance Sheet is inherently point-in-time). Same auth -> parse/validate
    -> resolve-profile -> scope-check sequence as Stories 3.2/3.3, adapted
    for the single date param (no inverted-range check applies -- there's
    only one date):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id`/`as_of_date` -- 400 on failure, before any
         query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Aggregate via `compute_balance_sheet` (which itself calls
         `compute_trial_balance`/`compute_profit_loss` with the
         since-inception window -- never re-derives the aggregation query
         independently) and respond.
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")
    as_of_date_str = request.query_params.get("as_of_date")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    as_of_date = _parse_iso_date(as_of_date_str)

    if pmc_id is None or as_of_date is None:
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "as_of_date": as_of_date_str,
            },
            message="pmc_id and as_of_date are required "
            "(as_of_date must be a valid ISO 8601 date)",
            status=400,
        )

    finance_pmc_profile = FinancePMCProfile.objects.filter(pmc_id=pmc_id).first()
    if finance_pmc_profile is None:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="No FinancePMCProfile exists for the given pmc_id",
            status=404,
        )

    reachable_pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
    if pmc_id not in reachable_pmc_ids:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="You are not authorized to view this PMC's reports",
            status=403,
        )

    balance_sheet = compute_balance_sheet(finance_pmc_profile, as_of_date)

    return prepare_response(
        content={
            "pmc_id": pmc_id,
            "as_of_date": as_of_date.isoformat(),
            "asset_accounts": balance_sheet["asset_accounts"],
            "liability_accounts": balance_sheet["liability_accounts"],
            "equity": balance_sheet["equity"],
            "balanced": balance_sheet["balanced"],
        },
        message="Balance Sheet report generated",
        status=200,
    )

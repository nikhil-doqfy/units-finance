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
from ledger.models import (
    Account,
    BankStatementMatch,
    FinancePMCProfile,
    JournalEntry,
    LeaseRef,
    LeaseTransactionRef,
)
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
from ledger.reconciliation import (
    BankStatementImportError,
    BankStatementMatchError,
    apply_match_decision,
    find_suggested_matches,
    import_bank_statement_csv,
)
from ledger.reports import (
    compute_account_ledger_lines,
    compute_ageing,
    compute_balance_sheet,
    compute_chart_of_accounts,
    compute_profit_loss,
    compute_trial_balance,
)
from ledger.response_envelope import prepare_response

# frontend-prd.md FFR-14/FFR-15's drill-down page size, sibling to Ageing's
# existing AGEING_DEFAULT_PAGE_SIZE/AGEING_MAX_PAGE_SIZE pair (Structural
# Seed precedent) -- a Ledger drill-down is the other naturally-growing list
# in this API, so it paginates the same way.
LEDGER_LINES_DEFAULT_PAGE_SIZE = 25
LEDGER_LINES_MAX_PAGE_SIZE = 100

# Story 3.5's default page size (spec Boundaries & Constraints: "page_size
# (optional, default a fixed constant e.g. 25)").
AGEING_DEFAULT_PAGE_SIZE = 25
# Upper bound on caller-supplied page_size -- prevents a single request from
# forcing the full per-row Ledger-balance computation onto one page (review
# finding; unbounded page_size was not covered by the spec).
AGEING_MAX_PAGE_SIZE = 100


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


@api_view(["GET"])
def ageing_report(request):
    """Story 3.5: `GET /reports/ageing`.

    `pmc_id` is required; `page` (default 1) and `page_size` (default
    `AGEING_DEFAULT_PAGE_SIZE`) are optional -- no date param at all, since
    Ageing is always "as of today" (spec Boundaries & Constraints, FR-13).
    Same auth -> parse/validate -> resolve-profile -> scope-check sequence
    as Stories 3.2-3.4:
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id` (and `page`/`page_size`, if given) -- 400
         on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Aggregate/paginate via `compute_ageing` and respond -- `content` is
         the row list, `pagination` is a sibling top-level key, never
         nested inside `content` (Structural Seed, spec Never).
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")
    raw_page = request.query_params.get("page", "1")
    raw_page_size = request.query_params.get("page_size", str(AGEING_DEFAULT_PAGE_SIZE))

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        page = None

    try:
        page_size = int(raw_page_size)
    except (TypeError, ValueError):
        page_size = None

    if (
        pmc_id is None
        or page is None
        or page_size is None
        or page < 1
        or page_size < 1
        or page_size > AGEING_MAX_PAGE_SIZE
    ):
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "page": raw_page,
                "page_size": raw_page_size,
            },
            message="pmc_id is required (page must be a positive integer; "
            f"page_size must be a positive integer up to {AGEING_MAX_PAGE_SIZE})",
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

    ageing = compute_ageing(finance_pmc_profile, page=page, page_size=page_size)

    return prepare_response(
        content=ageing["content"],
        message="Ageing report generated",
        status=200,
        paginator=ageing["page_obj"],
        total_records=ageing["total_records"],
    )


@api_view(["POST"])
def bank_statement_import(request):
    """Story 4.1: `POST /reconciliation/bank-statement-import` (FR-14).

    `pmc_id` and `file` (multipart/form-data) are required. Same auth ->
    parse/validate -> resolve-profile -> scope-check sequence as
    `trial_balance_report` (spec Code Map), adapted for a file upload instead
    of GET query params:
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id` -- 400 on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Require a `file` in `request.FILES` -- 400 if missing.
      6. Delegate to `ledger.reconciliation.import_bank_statement_csv` --
         whole-file rejection (400, naming the problem) on any row/column
         parse failure, no partial writes (spec Always/Never).
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.data.get("pmc_id")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    if pmc_id is None:
        return prepare_response(
            content={"pmc_id": raw_pmc_id},
            message="pmc_id is required",
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
            message="You are not authorized to import bank statements for this PMC",
            status=403,
        )

    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="file is required",
            status=400,
        )

    try:
        created_count = import_bank_statement_csv(finance_pmc_profile, uploaded_file)
    except BankStatementImportError as exc:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message=str(exc),
            status=400,
        )

    return prepare_response(
        content={"pmc_id": pmc_id, "created": created_count},
        message="Bank statement imported",
        status=201,
    )


@api_view(["GET"])
def suggested_matches(request):
    """Story 4.2: `GET /reconciliation/suggested-matches` (FR-15).

    `pmc_id` is a required query param. Same auth -> parse/validate ->
    resolve-profile -> scope-check sequence as every other Finance endpoint
    (spec Always), then delegates the heuristic itself to
    `find_suggested_matches` (copying the auth/scope/404 skeleton from
    `bank_statement_import`, spec Code Map):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id` -- 400 on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Run the heuristic and respond -- one entry per matchable pair.
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    if pmc_id is None:
        return prepare_response(
            content={"pmc_id": raw_pmc_id},
            message="pmc_id is required",
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
            message="You are not authorized to view matches for this PMC",
            status=403,
        )

    matches = find_suggested_matches(finance_pmc_profile)
    suggestions = [
        {
            "bank_statement_line_id": match["bank_statement_line"].id,
            "journal_entry_id": match["journal_entry"].id,
            "amount": str(match["bank_statement_line"].amount),
            "statement_date": match["bank_statement_line"].statement_date.isoformat(),
            "posted_at": match["journal_entry"].posted_at.isoformat(),
        }
        for match in matches
    ]

    return prepare_response(
        content={"pmc_id": pmc_id, "suggestions": suggestions},
        message="Suggested matches generated",
        status=200,
    )


@api_view(["POST"])
def apply_bank_statement_match(request):
    """Story 4.2 / AD-16: `POST /reconciliation/match` (FR-15/FFR-11).

    `pmc_id`, `bank_statement_line_id`, `journal_entry_id`, `action`
    (`confirm`|`reject`|`unreconcile`) are all required. Same auth ->
    parse/validate -> resolve-profile -> scope-check sequence as every other
    Finance endpoint (spec Always), then delegates the state transition to
    `apply_match_decision` (spec Code Map), translating its
    `BankStatementMatchError.status` into the response:
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id`/`bank_statement_line_id`/
         `journal_entry_id`/`action` -- 400 on failure, before any query
         runs (`action` must be exactly `confirm`, `reject`, or
         `unreconcile`).
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Delegate to `apply_match_decision` -- 404/409 on its own
         validation failures, 200 on success. `unreconcile` only succeeds
         against a currently-`confirmed` pair (409 otherwise), flips the
         `BankStatementMatch` row to `unreconciled` and frees
         `bank_statement_line.reconciled` back to `False`, so both sides
         reappear in the open queue (FFR-11) with no separate endpoint
         needed.
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.data.get("pmc_id")
    raw_bank_statement_line_id = request.data.get("bank_statement_line_id")
    raw_journal_entry_id = request.data.get("journal_entry_id")
    action = request.data.get("action")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    bank_statement_line_id = None
    if raw_bank_statement_line_id is not None:
        try:
            bank_statement_line_id = int(raw_bank_statement_line_id)
        except (TypeError, ValueError):
            bank_statement_line_id = None

    journal_entry_id = None
    if raw_journal_entry_id is not None:
        try:
            journal_entry_id = int(raw_journal_entry_id)
        except (TypeError, ValueError):
            journal_entry_id = None

    action_map = {
        "confirm": BankStatementMatch.CONFIRMED,
        "reject": BankStatementMatch.REJECTED,
        "unreconcile": BankStatementMatch.UNRECONCILED,
    }

    if (
        pmc_id is None
        or bank_statement_line_id is None
        or journal_entry_id is None
        or action not in action_map
    ):
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "bank_statement_line_id": raw_bank_statement_line_id,
                "journal_entry_id": raw_journal_entry_id,
                "action": action,
            },
            message="pmc_id, bank_statement_line_id, journal_entry_id, and "
            "a valid action ('confirm', 'reject', or 'unreconcile') are "
            "required",
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
            message="You are not authorized to modify matches for this PMC",
            status=403,
        )

    try:
        result = apply_match_decision(
            finance_pmc_profile,
            bank_statement_line_id,
            journal_entry_id,
            action_map[action],
        )
    except BankStatementMatchError as exc:
        return prepare_response(
            content={
                "pmc_id": pmc_id,
                "bank_statement_line_id": bank_statement_line_id,
                "journal_entry_id": journal_entry_id,
            },
            message=str(exc),
            status=exc.status,
        )

    return prepare_response(
        content={
            "pmc_id": pmc_id,
            "bank_statement_line_id": bank_statement_line_id,
            "journal_entry_id": journal_entry_id,
            "status": result["status"],
        },
        message="Match decision applied",
        status=200,
    )


@api_view(["GET"])
def chart_of_accounts(request):
    """AD-6 / frontend-prd.md FFR-13: `GET /accounts`.

    The Chart of Accounts view's backend companion, closing the PRD gap
    that no endpoint returns raw `Account` rows on their own (only nested
    inside Trial Balance's response). `pmc_id` is the only required query
    param -- there is no date range: this view always reflects each
    Account's current, since-inception balance (spec Design Notes, matching
    `compute_chart_of_accounts`'s own since-inception convention).

    Same auth -> parse/validate -> resolve-profile -> scope-check sequence
    as every other Finance reporting endpoint (spec Always):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id` -- 400 on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Aggregate via `compute_chart_of_accounts` and respond -- returned
         whole, never paginated (a fixed, small CoA is a snapshot document,
         same Structural Seed rule as Trial Balance/P&L/Balance Sheet).
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    raw_pmc_id = request.query_params.get("pmc_id")

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    if pmc_id is None:
        return prepare_response(
            content={"pmc_id": raw_pmc_id},
            message="pmc_id is required",
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
            message="You are not authorized to view this PMC's Chart of Accounts",
            status=403,
        )

    result = compute_chart_of_accounts(finance_pmc_profile)

    return prepare_response(
        content={"pmc_id": pmc_id, "accounts": result["accounts"]},
        message="Chart of Accounts generated",
        status=200,
    )


@api_view(["GET"])
def account_ledger_lines(request, account_id):
    """AD-6 / frontend-prd.md FFR-14/FFR-15: `GET /accounts/<pk>/ledger-lines`.

    The per-Account Ledger drill-down's backend companion, closing the PRD
    gap that no endpoint exposes raw `LedgerLine` rows -- only report-level
    aggregates. `pmc_id`, `start_date`, `end_date` are required query
    params (same date-range shape as Trial Balance, spec Design Notes:
    this view's whole point is to let a caller decompose exactly the
    figure Trial Balance showed for the same Account/range); `page`/
    `page_size` are optional, following Ageing's existing pagination
    convention (Structural Seed).

    Same auth -> parse/validate -> resolve-profile -> scope-check sequence
    as every other Finance reporting endpoint (spec Always), with one
    additional step particular to this view: resolving `account_id` (the
    URL path segment) to a real `Account` row scoped to the *same*
    `finance_pmc_profile` the `pmc_id` query param resolved -- a 404 if
    `account_id` doesn't exist, or exists but belongs to a different PMC's
    profile (never leaking cross-PMC Account existence via a 403 vs 404
    distinction, spec Never):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id`/`start_date`/`end_date` (and `page`/
         `page_size`, if given) -- 400 on failure, before any query runs.
      3. Resolve `FinancePMCProfile` by `pmc_id` -- 404 if none exists.
      4. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs.
      5. Resolve `account_id` scoped to that same profile -- 404 if no such
         Account exists for this PMC.
      6. Aggregate/paginate via `compute_account_ledger_lines` and respond
         -- `content` is the row list, `pagination` is a sibling top-level
         key, never nested inside `content` (Structural Seed, spec Never).
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
    raw_page = request.query_params.get("page", "1")
    raw_page_size = request.query_params.get(
        "page_size", str(LEDGER_LINES_DEFAULT_PAGE_SIZE)
    )

    pmc_id = None
    if raw_pmc_id is not None:
        try:
            pmc_id = int(raw_pmc_id)
        except (TypeError, ValueError):
            pmc_id = None

    start_date = _parse_iso_date(start_date_str)
    end_date = _parse_iso_date(end_date_str)

    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        page = None

    try:
        page_size = int(raw_page_size)
    except (TypeError, ValueError):
        page_size = None

    if (
        pmc_id is None
        or start_date is None
        or end_date is None
        or page is None
        or page_size is None
        or page < 1
        or page_size < 1
        or page_size > LEDGER_LINES_MAX_PAGE_SIZE
    ):
        return prepare_response(
            content={
                "pmc_id": raw_pmc_id,
                "start_date": start_date_str,
                "end_date": end_date_str,
                "page": raw_page,
                "page_size": raw_page_size,
            },
            message="pmc_id, start_date, and end_date are required "
            "(start_date/end_date must be valid ISO 8601 dates; page must "
            "be a positive integer; page_size must be a positive integer "
            f"up to {LEDGER_LINES_MAX_PAGE_SIZE})",
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
            message="You are not authorized to view this PMC's Ledger",
            status=403,
        )

    account = Account.objects.filter(
        pk=account_id, finance_pmc_profile=finance_pmc_profile
    ).first()
    if account is None:
        return prepare_response(
            content={"account_id": account_id, "pmc_id": pmc_id},
            message="No Account exists for the given account_id and pmc_id",
            status=404,
        )

    result = compute_account_ledger_lines(
        account, start_date, end_date, page=page, page_size=page_size
    )

    return prepare_response(
        content={
            "pmc_id": pmc_id,
            "account_id": account.id,
            "account_name": account.name,
            "account_type": account.account_type,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "account_balance": result["account_balance"],
            "lines": result["content"],
        },
        message="Account Ledger lines retrieved",
        status=200,
        paginator=result["page_obj"],
        total_records=result["total_records"],
    )


NOT_ACTIVATED = "not_activated"
ACTIVATED_EMPTY = "activated_empty"
ACTIVATED = "activated"


@api_view(["GET"])
def finance_pmc_profile_status(request, pmc_id):
    """AD-6 / frontend-prd.md FFR-3: `GET /finance-pmc-profile/<pmc_id>/status`.

    The activation-status signal AD-3's frontend route resolver depends on
    (`route.data['financeActivation']`) -- closes the last open item from
    AD-6's original gap list: distinguishing "no FinancePMCProfile exists
    for this PMC" (`not_activated`) from "profile exists, zero data"
    (`activated_empty`) from "profile exists, has activity"
    (`activated`), a distinction no existing endpoint could make since
    every one of them 404s identically for an unactivated PMC.

    Deliberately always a normal 200 (spec Always/Never, per AD-6): a
    reachable-but-unactivated PMC is not an error condition, so `content`
    carries the 3-state string directly rather than the caller having to
    infer "not activated" from a 404 the way every other Finance endpoint's
    404 actually does mean "this specific id doesn't exist." Auth and PMC
    reachability are still checked before revealing this status (never
    leaking whether a PMC is Finance-activated to a caller who cannot
    reach that PMC at all):
      1. Auth (`authenticate_reporting_request`) -- 401 on any rejection,
         before any query runs.
      2. Parse/validate `pmc_id` (URL path segment) -- 400 on failure.
      3. Scope check (`get_pmc_ids_for_user_profile`) -- 403 if the
         requested `pmc_id` is not in the caller's reachable PMCs. Run
         BEFORE resolving `FinancePMCProfile` (unlike every other Finance
         endpoint, which resolves the profile first) specifically so an
         unreachable pmc_id never distinguishes "PMC doesn't exist" from
         "PMC exists but isn't Finance-activated" via timing/response-shape
         differences -- both cases get the identical 403.
      4. Resolve `FinancePMCProfile` by `pmc_id` -- absence means
         `not_activated`, never a 404, per spec Always.
      5. If a profile exists, `activated_empty` vs `activated` is decided
         by whether any `JournalEntry` has ever been posted for it
         (`JournalEntry.objects.filter(finance_pmc_profile=...).exists()`)
         -- the same "has real Ledger activity" signal every report
         function already treats as the difference between a snapshot with
         real numbers and an all-zeros degenerate result (spec Design
         Notes, matching `compute_balance_sheet`'s own precedent for an
         empty-window result being a valid, non-error degenerate case).
    """
    user_profile_ref, reason = authenticate_reporting_request(request)
    if user_profile_ref is None:
        return prepare_response(
            content={"reason": reason},
            message="Authentication failed",
            status=401,
        )

    reachable_pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
    if pmc_id not in reachable_pmc_ids:
        return prepare_response(
            content={"pmc_id": pmc_id},
            message="You are not authorized to view this PMC's Finance status",
            status=403,
        )

    finance_pmc_profile = FinancePMCProfile.objects.filter(pmc_id=pmc_id).first()
    if finance_pmc_profile is None:
        return prepare_response(
            content={"pmc_id": pmc_id, "status": NOT_ACTIVATED},
            message="Finance is not yet activated for this PMC",
            status=200,
        )

    has_activity = JournalEntry.objects.filter(
        finance_pmc_profile=finance_pmc_profile
    ).exists()
    status_value = ACTIVATED if has_activity else ACTIVATED_EMPTY

    return prepare_response(
        content={"pmc_id": pmc_id, "status": status_value},
        message="Finance activation status resolved",
        status=200,
    )

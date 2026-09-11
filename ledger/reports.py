"""
Story 3.2: Trial Balance report aggregation.

`compute_trial_balance` is kept separate from `views.trial_balance_report`
deliberately (spec Design Notes): the epics require Story 3.3's P&L figure
to be "independently recomputable from Trial Balance output" -- keeping the
aggregation logic in its own reusable function means Story 3.3 can call this
same function and derive Income/Expense subtotals from its return value,
rather than re-deriving the query.

Sums `LedgerLine.debit`/`credit` unconditionally across every `JournalEntry`
whose `posted_at` falls within `[start_date 00:00:00, end_date 23:59:59]`
(inclusive both ends) -- including reversal entries, which are never
special-cased (AD-16): their `LedgerLine`s already carry the additive-only
reversing amounts, so summing every row nets them out correctly.

Every `Account` on the profile is included even with zero activity in the
period (spec Boundaries & Constraints) -- the Trial Balance is a full
Chart-of-Accounts snapshot, not just accounts that moved.
"""
from datetime import datetime, time

from django.core.paginator import Paginator
from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from ledger.models import (
    Account,
    LedgerLine,
    LeaseTransactionRef,
    LeaseRef,
    UnitRef,
    PropertyRef,
)
from ledger.posting import AR_TENANTS_ACCOUNT_NAME


def compute_trial_balance(finance_pmc_profile, start_date, end_date):
    """Return the Trial Balance for `finance_pmc_profile` over
    `[start_date, end_date]` (inclusive both ends, Python `date` objects).

    Returns a dict: `{"accounts": [...], "balanced": bool}`, where each
    account dict has `id`, `name`, `account_type`, `total_debit`,
    `total_credit`.
    """
    period_start = timezone.make_aware(datetime.combine(start_date, time.min))
    period_end = timezone.make_aware(datetime.combine(end_date, time.max))

    ledger_line_filter = Q(
        ledger_lines__journal_entry__finance_pmc_profile=finance_pmc_profile,
        ledger_lines__journal_entry__posted_at__gte=period_start,
        ledger_lines__journal_entry__posted_at__lte=period_end,
    )

    accounts = (
        Account.objects.filter(finance_pmc_profile=finance_pmc_profile)
        .annotate(
            total_debit=Coalesce(
                Sum("ledger_lines__debit", filter=ledger_line_filter),
                0,
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            total_credit=Coalesce(
                Sum("ledger_lines__credit", filter=ledger_line_filter),
                0,
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
        )
        .order_by("id")
    )

    account_rows = [
        {
            "id": account.id,
            "name": account.name,
            "account_type": account.account_type,
            "total_debit": account.total_debit,
            "total_credit": account.total_credit,
        }
        for account in accounts
    ]

    sum_debit = sum((row["total_debit"] for row in account_rows), start=0)
    sum_credit = sum((row["total_credit"] for row in account_rows), start=0)

    return {
        "accounts": account_rows,
        "balanced": sum_debit == sum_credit,
    }


def compute_profit_loss(finance_pmc_profile, start_date, end_date):
    """Story 3.3: Profit & Loss report aggregation.

    Deliberately calls `compute_trial_balance` and filters/transforms its
    `accounts` list -- never re-derives the aggregation query independently
    (spec Boundaries & Constraints, Never) -- so the P&L's net figure is
    "independently recomputable from the Trial Balance output" (FR-11) by
    construction, not just by coincidence (spec Design Notes).

    Per-account contribution follows standard double-entry convention
    (spec Boundaries & Constraints / Design Notes): an `Income` account is
    credit-normal (`total_credit - total_debit`), an `Expense` account is
    debit-normal (`total_debit - total_credit`) -- matching every existing
    posting function's own convention (e.g. `post_rent_ar` credits Rent
    Income, `post_commission_split`/`post_bounce_fee` debit Commission
    Expense/Bank Charges-Fees).

    Returns a dict: `{"income_accounts": [...], "expense_accounts": [...],
    "net_profit_loss": Decimal}`, where each account dict has `id`, `name`,
    `account_type`, `total_debit`, `total_credit`, `contribution`.
    """
    trial_balance = compute_trial_balance(finance_pmc_profile, start_date, end_date)

    income_accounts = []
    expense_accounts = []

    for account in trial_balance["accounts"]:
        if account["account_type"] == Account.INCOME:
            contribution = account["total_credit"] - account["total_debit"]
            income_accounts.append({**account, "contribution": contribution})
        elif account["account_type"] == Account.EXPENSE:
            contribution = account["total_debit"] - account["total_credit"]
            expense_accounts.append({**account, "contribution": contribution})

    total_income = sum(
        (row["contribution"] for row in income_accounts), start=0
    )
    total_expense = sum(
        (row["contribution"] for row in expense_accounts), start=0
    )

    return {
        "income_accounts": income_accounts,
        "expense_accounts": expense_accounts,
        "net_profit_loss": total_income - total_expense,
    }


def compute_balance_sheet(finance_pmc_profile, as_of_date):
    """Story 3.4: Balance Sheet report aggregation.

    A Balance Sheet is a point-in-time snapshot (spec Design Notes), unlike
    Trial Balance/P&L's date-range activity -- so both underlying calls use
    a cumulative window from `finance_pmc_profile.created.date()` (the
    profile's true inception point, never a hardcoded epoch, spec
    Boundaries & Constraints/Never) through `as_of_date`, giving genuine
    running balances for every account, not just recent-period activity.

    If `as_of_date` is before `finance_pmc_profile.created.date()`, the
    window is empty (start > end) -- `compute_trial_balance` handles this
    the same as any other window with no Journal Entries: every account
    comes back zeroed, which is a valid degenerate result, not an error
    (spec I/O matrix).

    Per-account balance sign follows the same double-entry convention as
    Story 3.3 (spec Boundaries & Constraints): `Asset` accounts are
    debit-normal (`total_debit - total_credit`), `Liability` accounts are
    credit-normal (`total_credit - total_debit`).

    Equity is a single derived "Retained Earnings" line -- never a real
    posted `Account` row (spec Never) -- computed fresh on every request as
    `compute_profit_loss`'s cumulative `net_profit_loss` over the same
    since-inception window (spec Design Notes: this is how any real
    accounting system would derive Retained Earnings, without a new
    posting mechanism or touching Epic 2's posting functions).

    Returns a dict: `{"asset_accounts": [...], "liability_accounts": [...],
    "equity": {"name": "Retained Earnings", "balance": Decimal},
    "balanced": bool}`, where each account dict has `id`, `name`,
    `account_type`, `total_debit`, `total_credit`, `balance`.
    """
    since_inception = finance_pmc_profile.created.date()

    trial_balance = compute_trial_balance(finance_pmc_profile, since_inception, as_of_date)
    profit_loss = compute_profit_loss(finance_pmc_profile, since_inception, as_of_date)

    asset_accounts = []
    liability_accounts = []

    for account in trial_balance["accounts"]:
        if account["account_type"] == Account.ASSET:
            balance = account["total_debit"] - account["total_credit"]
            asset_accounts.append({**account, "balance": balance})
        elif account["account_type"] == Account.LIABILITY:
            balance = account["total_credit"] - account["total_debit"]
            liability_accounts.append({**account, "balance": balance})

    total_assets = sum((row["balance"] for row in asset_accounts), start=0)
    total_liabilities = sum((row["balance"] for row in liability_accounts), start=0)
    retained_earnings = profit_loss["net_profit_loss"]

    return {
        "asset_accounts": asset_accounts,
        "liability_accounts": liability_accounts,
        "equity": {
            "name": "Retained Earnings",
            "balance": retained_earnings,
        },
        "balanced": total_assets == total_liabilities + retained_earnings,
    }


# Story 3.5's bucket boundaries (spec Boundaries & Constraints, FR-13):
# inclusive lower bound, so day 31 falls in "31-60", not "1-30". "current"
# (<=0 days) is defined-but-typically-empty in practice, since the candidate
# filter below already excludes cheque_date >= today (Design Notes).
BUCKET_CURRENT = "current"
BUCKET_1_30 = "1-30"
BUCKET_31_60 = "31-60"
BUCKET_61_90 = "61-90"
BUCKET_90_PLUS = "90+"


def _bucket_for_days_overdue(days_overdue):
    """Bucket boundaries are inclusive of the lower bound (FR-13): day 31
    falls in "31-60", not "1-30"."""
    if days_overdue <= 0:
        return BUCKET_CURRENT
    if days_overdue <= 30:
        return BUCKET_1_30
    if days_overdue <= 60:
        return BUCKET_31_60
    if days_overdue <= 90:
        return BUCKET_61_90
    return BUCKET_90_PLUS


def compute_ageing(finance_pmc_profile, page=1, page_size=25):
    """Story 3.5: Ageing report aggregation.

    Resolves candidate `LeaseTransactionRef` rows for `finance_pmc_profile`
    via the same PMC chain-resolution `post_rent_ar` uses
    (`lease_id` -> `LeaseRef.unit_id` -> `UnitRef.parent_property_id` ->
    `PropertyRef.pmc_id`, spec Boundaries & Constraints), filtered to
    `status != "REALIZED"` with a non-null `cheque_date` in the past
    (`cheque_date.date() < today`).

    Each candidate row's outstanding AR balance is Finance's own posted
    Ledger balance -- `sum(LedgerLine.debit - LedgerLine.credit)` on the
    "AR — Tenants" account for `JournalEntry.source_lease_transaction_id ==
    <that row's id>` -- never `LeaseTransactionRef.amount` directly
    (human-confirmed, spec Never). A row whose outstanding balance is `0`
    is excluded entirely -- this correctly drops an already-cleared
    `CREDITED` transaction even though its raw status isn't `REALIZED`
    (spec Design Notes).

    Paginated via Django's `Paginator` (spec Tasks & Acceptance). Returns a
    dict: `{"content": [...], "page_obj": <Page>, "total_records": int}`,
    where each `content` row is `{lease_transaction_id, cheque_date,
    days_overdue, bucket, outstanding_amount}`.
    """
    today = timezone.localdate()

    # Resolve every Property/Unit/Lease that chains to this profile's PMC --
    # walked forward (PMC -> Properties -> Units -> Leases) rather than
    # per-row backward resolution, since Ageing must filter/paginate a whole
    # candidate set up front, unlike the posting engine's single-row
    # resolution (Design Notes: same chain, opposite traversal direction).
    property_ids = list(
        PropertyRef.objects.filter(pmc_id=finance_pmc_profile.pmc_id).values_list(
            "id", flat=True
        )
    )
    unit_ids = list(
        UnitRef.objects.filter(parent_property_id__in=property_ids).values_list(
            "id", flat=True
        )
    )
    lease_ids = list(
        LeaseRef.objects.filter(unit_id__in=unit_ids).values_list("id", flat=True)
    )

    candidate_txns = list(
        LeaseTransactionRef.objects.filter(
            lease_id__in=lease_ids,
            cheque_date__isnull=False,
        )
        .exclude(status="REALIZED")
        # `created` lives only on units-backend's parent `Documents` table
        # (MTI: `LeaseTransaction(Documents)`), not on `lease_leasetransaction`
        # itself -- deferred here since Ageing never reads it, and selecting
        # it would 500 with `column lease_leasetransaction.created does not
        # exist` (confirmed against the live table's actual columns).
        .defer("created")
        .order_by("cheque_date", "id")
    )

    rows = []
    for txn in candidate_txns:
        cheque_date = txn.cheque_date.date()
        if not cheque_date < today:
            # "in the past" filter (spec Boundaries & Constraints) -- a
            # cheque_date of today or in the future is excluded outright,
            # which is why the "current" bucket is typically empty in
            # practice (kept as a defined bucket for completeness).
            continue

        balance_totals = LedgerLine.objects.filter(
            journal_entry__finance_pmc_profile=finance_pmc_profile,
            journal_entry__source_lease_transaction_id=txn.id,
            account__name=AR_TENANTS_ACCOUNT_NAME,
        ).aggregate(
            total_debit=Coalesce(
                Sum("debit"), 0, output_field=DecimalField(max_digits=14, decimal_places=2)
            ),
            total_credit=Coalesce(
                Sum("credit"), 0, output_field=DecimalField(max_digits=14, decimal_places=2)
            ),
        )
        outstanding_amount = (
            balance_totals["total_debit"] - balance_totals["total_credit"]
        )

        if outstanding_amount <= 0:
            # Fully cleared (0) or in credit/overpaid (negative) -- excluded
            # entirely. A negative balance is not overdue rent; it's money
            # owed back to the tenant, so it must never render as an
            # ageing-bucket row (review finding).
            continue

        days_overdue = (today - cheque_date).days

        rows.append(
            {
                "lease_transaction_id": txn.id,
                "cheque_date": cheque_date.isoformat(),
                "days_overdue": days_overdue,
                "bucket": _bucket_for_days_overdue(days_overdue),
                "outstanding_amount": outstanding_amount,
            }
        )

    paginator = Paginator(rows, page_size)
    # Clamp an out-of-range page to the nearest valid page (1 or the last
    # page) rather than raising EmptyPage/PageNotAnInteger -- a growing list
    # naturally has requests racing a shrinking/growing page count, and this
    # keeps the endpoint returning a normal 200 rather than a 500 for a
    # caller-supplied page number that's merely stale, not malformed (the
    # 400 path in the view already rejects a genuinely invalid/non-integer
    # page).
    page = max(1, min(page, paginator.num_pages))
    page_obj = paginator.page(page)

    return {
        "content": list(page_obj.object_list),
        "page_obj": page_obj,
        "total_records": paginator.count,
    }


# Standard double-entry normal-balance sign per Account type, matching the
# convention already established independently in compute_profit_loss
# (Income/Expense) and compute_balance_sheet (Asset/Liability) above --
# Equity is included for completeness/symmetry only: no real Account row is
# ever seeded or posted to with account_type=Equity in Phase 1 (Equity is
# always a derived Retained Earnings figure, never a posted Account, per
# compute_balance_sheet's own docstring), so this branch is expected to be
# unreachable in practice, not a new posting path.
_CREDIT_NORMAL_ACCOUNT_TYPES = {Account.LIABILITY, Account.INCOME, Account.EQUITY}


def compute_chart_of_accounts(finance_pmc_profile):
    """FFR-13 (frontend-prd.md §4.4): the standalone Chart of Accounts view's
    backend companion (AD-6).

    Returns every seeded `Account` for `finance_pmc_profile` (FR-3's fixed
    7+ row set) with its current, since-inception running balance -- a
    cumulative window from `finance_pmc_profile.created.date()` through
    today, following the exact same since-inception convention
    `compute_balance_sheet` already established above (Design Notes
    precedent), rather than a new windowing rule invented for this view.

    Sign convention matches every other report function in this module:
    Asset/Expense accounts are debit-normal (`total_debit - total_credit`),
    Liability/Income/Equity accounts are credit-normal
    (`total_credit - total_debit`) -- reusing `compute_trial_balance`'s raw
    per-account debit/credit sums rather than re-deriving the aggregation
    query (same reuse discipline as `compute_profit_loss`/
    `compute_balance_sheet`).

    Returns a dict: `{"accounts": [...]}`, where each account dict has
    `id`, `name`, `account_type`, `account_subtype`, `balance` --
    deliberately a smaller shape than Trial Balance's row (no separate
    `total_debit`/`total_credit` exposed), since this view's job is "what
    Accounts exist and what do they currently hold," not a period
    debit/credit breakdown (that's Trial Balance's job, and remains
    reachable via the existing endpoint for the same PMC).

    Story 5.3 (FR-18) adds `account_subtype`, read via one lightweight
    `values_list` lookup keyed on `row["id"]` -- `compute_trial_balance`'s
    own row dicts don't carry this field, so this one extra query is scoped
    to this function only, never touching the shared
    trial-balance/profit-loss/balance-sheet aggregation chain (spec Never).
    `None` for any account without a subtype (Income/Expense accounts, or
    any pre-existing row left uncategorized by the Story 5.3 migration).
    """
    since_inception = finance_pmc_profile.created.date()
    today = timezone.localdate()

    trial_balance = compute_trial_balance(finance_pmc_profile, since_inception, today)

    account_subtypes_by_id = dict(
        Account.objects.filter(finance_pmc_profile=finance_pmc_profile).values_list(
            "id", "account_subtype"
        )
    )

    accounts = [
        {
            "id": row["id"],
            "name": row["name"],
            "account_type": row["account_type"],
            "account_subtype": account_subtypes_by_id.get(row["id"]),
            "balance": (
                row["total_credit"] - row["total_debit"]
                if row["account_type"] in _CREDIT_NORMAL_ACCOUNT_TYPES
                else row["total_debit"] - row["total_credit"]
            ),
        }
        for row in trial_balance["accounts"]
    ]

    return {"accounts": accounts}


def compute_account_ledger_lines(account, start_date, end_date, page=1, page_size=25):
    """FFR-14/FFR-15 (frontend-prd.md §4.4): the per-Account Ledger
    drill-down's backend companion (AD-6).

    Returns the individual `LedgerLine` rows posted to `account` within
    `[start_date, end_date]` (inclusive both ends, same window convention
    as `compute_trial_balance` above), ordered oldest-to-newest, each
    annotated with a running balance -- so the page's displayed rows sum to
    exactly the same figure Trial Balance would show for this Account and
    date range (frontend-prd.md FFR-14's decomposition guarantee), computed
    here via the same debit/credit sign convention as
    `compute_chart_of_accounts` above (not re-derived independently).

    Every row also carries `source_lease_transaction_id` (traceability,
    AD-14) and, when the parent `JournalEntry` is a reversal or was itself
    reversed, `reversed_journal_entry_id` / `reversing_entry_ids` -- the
    bidirectional linkage FFR-15 requires. `reversed_journal_entry_id` is
    read directly off the FK already on `JournalEntry` (AD-16); the reverse
    direction (a row's reversal, if any) has no direct FK to read, so it's
    resolved via `JournalEntry.reversing_entries` (the FK's own
    `related_name`, AD-16) -- never a new query pattern invented for this
    story, just the existing relation read from its other side.

    Paginated via Django's `Paginator`, matching Ageing's existing
    pagination convention (Structural Seed precedent) -- a Ledger
    drill-down is a naturally growing list, unlike Trial Balance/P&L/
    Balance Sheet's whole-snapshot shape.

    Returns a dict: `{"content": [...], "page_obj": <Page>,
    "total_records": int, "account_balance": Decimal}`, where
    `account_balance` is the account's total balance for the same window
    (for the page to display against the sum of visible rows) and each
    `content` row is `{ledger_line_id, journal_entry_id, posted_at,
    source_lease_transaction_id, source_status_transition, debit, credit,
    running_balance, reversed_journal_entry_id, reversing_entry_ids}`.
    """
    period_start = timezone.make_aware(datetime.combine(start_date, time.min))
    period_end = timezone.make_aware(datetime.combine(end_date, time.max))

    is_credit_normal = account.account_type in _CREDIT_NORMAL_ACCOUNT_TYPES

    lines = list(
        LedgerLine.objects.filter(
            account=account,
            journal_entry__posted_at__gte=period_start,
            journal_entry__posted_at__lte=period_end,
        )
        .select_related("journal_entry")
        .prefetch_related("journal_entry__reversing_entries")
        .order_by("journal_entry__posted_at", "id")
    )

    rows = []
    running_balance = 0
    for line in lines:
        signed_amount = (
            (line.credit - line.debit) if is_credit_normal else (line.debit - line.credit)
        )
        running_balance += signed_amount

        journal_entry = line.journal_entry
        reversing_entry_ids = [
            entry.id for entry in journal_entry.reversing_entries.all()
        ]

        rows.append(
            {
                "ledger_line_id": line.id,
                "journal_entry_id": journal_entry.id,
                "posted_at": journal_entry.posted_at.isoformat(),
                "source_lease_transaction_id": journal_entry.source_lease_transaction_id,
                "source_status_transition": journal_entry.source_status_transition,
                "debit": line.debit,
                "credit": line.credit,
                "running_balance": running_balance,
                "reversed_journal_entry_id": journal_entry.reversed_journal_entry_id,
                "reversing_entry_ids": reversing_entry_ids,
            }
        )

    paginator = Paginator(rows, page_size)
    # Matches compute_ageing's existing clamp convention exactly (Django's
    # Paginator always reports num_pages >= 1, even for an empty list).
    page = max(1, min(page, paginator.num_pages))
    page_obj = paginator.page(page)

    return {
        "content": list(page_obj.object_list),
        "page_obj": page_obj,
        "total_records": paginator.count,
        "account_balance": running_balance,
    }

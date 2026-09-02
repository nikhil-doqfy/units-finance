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

from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from ledger.models import Account, LedgerLine


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

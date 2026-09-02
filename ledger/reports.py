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

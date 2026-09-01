"""
Story 1.3: standard Chart of Accounts seed.

Defines the fixed, non-configurable set of 7 standard Accounts (8 rows —
see Design Notes in the spec on the "7+" framing) created automatically for
every new `FinancePMCProfile`, and the helper that bulk-creates them.

This is not a CoA editor or a configurable fixture: the list below is the
entire Phase 1 Chart of Accounts, fixed in code (spec Never: no CLI flags,
no settings-based configuration, no per-PMC customization).
"""
from ledger.models import Account

# (name, account_type) pairs, exactly as specified by FR-3 / the architecture
# spine's ER diagram / the spec's Design Notes table. Order is preserved for
# readability only — it carries no behavioral meaning.
STANDARD_CHART_OF_ACCOUNTS = [
    ("Rent Income", Account.INCOME),
    ("Security Deposits Held", Account.LIABILITY),
    ("VAT Payable", Account.LIABILITY),
    ("Bank", Account.ASSET),
    ("AR — Tenants", Account.ASSET),
    ("AP — PMC Commission", Account.LIABILITY),
    ("Commission Expense", Account.EXPENSE),
    ("Bank Charges/Fees", Account.EXPENSE),
]


def seed_standard_chart_of_accounts(profile):
    """Create the standard CoA (8 Accounts) scoped to the given profile.

    Bulk-creates one `Account` row per (name, account_type) pair in
    `STANDARD_CHART_OF_ACCOUNTS`, each FK'd to `profile`. Called exactly
    once per `FinancePMCProfile`, from the `post_save` signal handler in
    `ledger/signals.py` (guarded on `created=True`) — never re-invoked for
    an already-seeded profile.
    """
    Account.objects.bulk_create(
        [
            Account(
                finance_pmc_profile=profile,
                name=name,
                account_type=account_type,
            )
            for name, account_type in STANDARD_CHART_OF_ACCOUNTS
        ]
    )

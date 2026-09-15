"""
Story 1.3: standard Chart of Accounts seed.

Defines the fixed, non-configurable set of 7 standard Accounts (8 rows —
see Design Notes in the spec on the "7+" framing) created automatically for
every new `FinancePMCProfile`, and the helper that bulk-creates them.

This is not a CoA editor or a configurable fixture: the list below is the
entire Phase 1 Chart of Accounts, fixed in code (spec Never: no CLI flags,
no settings-based configuration, no per-PMC customization).

Story 2.4 adds "Bounced Cheques" (Asset type — represents money the tenant
still owes, structurally like AR) to this list. It was never part of Story
1.3's original 8-account seed; existing `FinancePMCProfile`s created before
Story 2.4 shipped will NOT retroactively get it (no seed-backfill mechanism
exists — accepted known gap, see spec Verification).

Story 5.3 (FR-18) adds a third element, `account_subtype`, to every row —
every one of the 9 standard accounts now carries an explicit subtype (or
`None` for Income/Expense accounts, which have nothing to subtype into in
Phase 1). See the spec's Design Notes for the full name -> subtype mapping
reasoning. Existing pre-story `FinancePMCProfile`s are NOT retroactively
backfilled (spec Boundaries & Constraints/Design Notes) — only newly-seeded
profiles get subtypes, mirroring the "Bounced Cheques" precedent above.
"""
from ledger.models import Account

# (name, account_type, account_subtype) triples, exactly as specified by
# FR-3/FR-18 / the architecture spine's ER diagram / the spec's Design Notes
# table. Order is preserved for readability only — it carries no behavioral
# meaning.
STANDARD_CHART_OF_ACCOUNTS = [
    ("Rent Income", Account.INCOME, None),
    ("Security Deposits Held", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
    ("VAT Payable", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
    ("Bank", Account.ASSET, Account.CURRENT_ASSET),
    ("AR — Tenants", Account.ASSET, Account.CURRENT_ASSET),
    ("AP — PMC Commission", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
    ("Commission Expense", Account.EXPENSE, None),
    ("Bank Charges/Fees", Account.EXPENSE, None),
    ("Bounced Cheques", Account.ASSET, Account.CURRENT_ASSET),
]


def seed_standard_chart_of_accounts(profile):
    """Create the standard CoA (9 Accounts) scoped to the given profile.

    Bulk-creates one `Account` row per (name, account_type, account_subtype)
    triple in `STANDARD_CHART_OF_ACCOUNTS`, each FK'd to `profile`. Called
    exactly once per `FinancePMCProfile`, from the `post_save` signal handler
    in `ledger/signals.py` (guarded on `created=True`) — never re-invoked for
    an already-seeded profile.
    """
    Account.objects.bulk_create(
        [
            Account(
                finance_pmc_profile=profile,
                name=name,
                account_type=account_type,
                account_subtype=account_subtype,
            )
            for name, account_type, account_subtype in STANDARD_CHART_OF_ACCOUNTS
        ]
    )

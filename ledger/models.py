"""
Story 1.2: FinancePMCProfile creation.

Adds the first Finance-owned model, `FinancePMCProfile` (accounting settings
for an existing units-backend `PropertyManagmentCompany`, keyed by `pmc_id`),
plus a read-only, unmanaged reference model, `PropertyManagmentCompanyRef`,
that lets Finance validate a `pmc_id` exists in units-backend's
`property_propertymanagmentcompany` table without importing units-backend's
`property` app or code (AD-19).
"""
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class FinancePMCProfile(models.Model):
    """Accounting settings that activate an existing PMC for Finance.

    Not a new legal-entity model — `pmc_id` holds the value of
    units-backend's `PropertyManagmentCompany.id` (a BigAutoField PK), but is
    deliberately a plain `BigIntegerField`, not a cross-database Django
    `ForeignKey`, since Finance and units-backend are separate Django
    projects/migration sets sharing one Postgres instance (AD-19).
    """

    pmc_id = models.BigIntegerField(
        help_text="units-backend PropertyManagmentCompany.id — not a cross-DB FK (AD-19)."
    )
    base_currency = models.CharField(max_length=3, default="AED")
    country = models.CharField(max_length=100)
    fiscal_year_start_month = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(12)]
    )
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"FinancePMCProfile(pmc_id={self.pmc_id}, country={self.country})"


class Account(models.Model):
    """One Chart-of-Accounts line, scoped to a single `FinancePMCProfile`.

    Unlike `FinancePMCProfile.pmc_id` (a plain int referencing a table in a
    different Django project's migration set), `finance_pmc_profile` is a
    real Django `ForeignKey` — both `Account` and `FinancePMCProfile` are
    Finance-owned models living in this same app/database (spec Boundaries
    & Constraints).

    Rows are created exclusively by the standard Chart of Accounts seed
    (`ledger.signals`/`ledger.seed`), triggered automatically via a
    `post_save` signal on `FinancePMCProfile` — there is no CoA editor UI or
    per-PMC customization in Phase 1 (FR-3 Notes).
    """

    ASSET = "Asset"
    LIABILITY = "Liability"
    INCOME = "Income"
    EXPENSE = "Expense"
    EQUITY = "Equity"

    ACCOUNT_TYPE_CHOICES = [
        (ASSET, "Asset"),
        (LIABILITY, "Liability"),
        (INCOME, "Income"),
        (EXPENSE, "Expense"),
        (EQUITY, "Equity"),
    ]

    finance_pmc_profile = models.ForeignKey(
        FinancePMCProfile,
        on_delete=models.CASCADE,
        related_name="accounts",
    )
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)

    def __str__(self):
        return f"Account(name={self.name}, type={self.account_type}, pmc_profile_id={self.finance_pmc_profile_id})"


class PropertyManagmentCompanyRef(models.Model):
    """Read-only reference onto units-backend's PropertyManagmentCompany table.

    `managed = False` so Django never creates/alters/drops this table via a
    Finance migration — Finance only ever reads it, to validate that a
    `pmc_id` passed to `create_finance_pmc_profile` refers to a real PMC
    (the first unmanaged-model pattern in this codebase, per the spec's
    Design Notes). Field list is kept minimal (id + name) — just enough to
    validate existence and produce a friendly error message; it does not
    mirror the full units-backend schema, so drift there won't break
    Finance's own migrations.
    """

    name = models.CharField(max_length=255)

    class Meta:
        managed = False
        db_table = "property_propertymanagmentcompany"

    def __str__(self):
        return self.name


class JournalEntry(models.Model):
    """Finance's own double-entry Ledger header record (FR-4, AD-14, AD-16).

    Story 2.2a is pure data-layer: no posting logic, no business rules, no
    wiring into `sync_lease_transaction` lives here — that's Story 2.2b.

    `source_lease_transaction_id` is deliberately a plain `BigIntegerField`,
    not a Django `ForeignKey`, mirroring `FinancePMCProfile.pmc_id`'s AD-19
    precedent — it references units-backend's `LeaseTransaction` row, which
    lives in a different Django project's migration set despite sharing the
    same Postgres instance (AD-2).
    """

    finance_pmc_profile = models.ForeignKey(
        FinancePMCProfile,
        on_delete=models.CASCADE,
        related_name="journal_entries",
    )
    source_lease_transaction_id = models.BigIntegerField(
        help_text="units-backend LeaseTransaction.id — not a cross-DB FK (AD-19 precedent)."
    )
    source_status_transition = models.CharField(
        max_length=100,
        help_text="Exact FROM_STATUS->TO_STATUS pair; idempotency key with "
        "source_lease_transaction_id (AD-14).",
    )
    reversed_journal_entry = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reversing_entries",
        help_text="FK to the original entry if this is a reversal; additive-only "
        "reversals, first populated by Story 2.4's post_bounce_reversal (AD-16). "
        "Django auto-derives the 'reversed_journal_entry_id' db column from "
        "this field name, matching the ARCHITECTURE-SPINE ER diagram's "
        "column name exactly.",
    )
    posted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return (
            f"JournalEntry(id={self.id}, "
            f"source_lease_transaction_id={self.source_lease_transaction_id}, "
            f"source_status_transition={self.source_status_transition})"
        )


class LedgerLine(models.Model):
    """One double-entry detail line belonging to a `JournalEntry`.

    Both `journal_entry` and `account` are real Django `ForeignKey`s —
    `JournalEntry` and `Account` are both Finance-owned models living in
    this same app/database, unlike the cross-service unmanaged ref models
    below.
    """

    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="ledger_lines",
    )
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    def __str__(self):
        return (
            f"LedgerLine(journal_entry_id={self.journal_entry_id}, "
            f"account_id={self.account_id}, debit={self.debit}, credit={self.credit})"
        )


class LeaseTransactionRef(models.Model):
    """Read-only reference onto units-backend's LeaseTransaction table.

    The chain's starting point (Design Notes) — lets Finance read the
    triggering transaction's own data. Follows `PropertyManagmentCompanyRef`'s
    established unmanaged-model pattern (Story 1.2): `managed = False`, real
    `db_table`, minimal field list — only what a future posting story (2.2b)
    actually needs, not a full schema mirror.
    """

    lease_id = models.BigIntegerField(
        help_text="units-backend Lease.id — not a cross-DB FK (next hop toward the PMC)."
    )
    amount = models.FloatField(null=True, blank=True)
    cheque_type = models.CharField(max_length=20)
    payment_type = models.CharField(max_length=20)
    status = models.CharField(max_length=20)

    class Meta:
        managed = False
        db_table = "lease_leasetransaction"

    def __str__(self):
        return f"LeaseTransactionRef(id={self.id}, lease_id={self.lease_id}, status={self.status})"


class LeaseRef(models.Model):
    """Read-only reference onto units-backend's Lease table.

    One hop toward the PMC from `LeaseTransactionRef.lease_id` (Design Notes).
    """

    unit_id = models.BigIntegerField(
        help_text="units-backend Unit.id — not a cross-DB FK (next hop toward the PMC)."
    )

    class Meta:
        managed = False
        db_table = "lease_lease"

    def __str__(self):
        return f"LeaseRef(id={self.id}, unit_id={self.unit_id})"


class UnitRef(models.Model):
    """Read-only reference onto units-backend's Unit table.

    Next hop toward the PMC from `LeaseRef.unit_id` (Design Notes).
    """

    parent_property_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend Property.id — not a cross-DB FK (next hop toward the PMC).",
    )

    class Meta:
        managed = False
        db_table = "property_unit"

    def __str__(self):
        return f"UnitRef(id={self.id}, parent_property_id={self.parent_property_id})"


class PropertyRef(models.Model):
    """Read-only reference onto units-backend's Property table.

    The chain's end (Design Notes) — resolves to `pmc_id`, the same value
    `FinancePMCProfile.pmc_id` already keys on (AD-19); no further resolution
    mechanism is needed past this hop.
    """

    pmc_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend PropertyManagmentCompany.id — matches FinancePMCProfile.pmc_id (AD-19).",
    )

    class Meta:
        managed = False
        db_table = "property_property"

    def __str__(self):
        return f"PropertyRef(id={self.id}, pmc_id={self.pmc_id})"

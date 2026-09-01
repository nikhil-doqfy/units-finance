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

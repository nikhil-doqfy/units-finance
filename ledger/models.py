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


class PMCChargeType(models.Model):
    """Story 5.2 (FR-17): maps an existing units-backend `Charge` catalog
    row to a Finance `Account`, one row per `(finance_pmc_profile,
    charge_id)` pair.

    Finance-owned -- never a modification to units-backend's `charges` app
    or `Charge` model (spec Always); Finance only ever reads `Charge` via
    the existing unmanaged `ChargeRef` (AD-5 precedent). `charge_id` is
    deliberately a plain `BigIntegerField`, not a Django FK, mirroring every
    other cross-service reference in this codebase (AD-19 precedent) -- it
    references units-backend's `Charge.id`, which lives in a different
    Django project's migration set despite sharing the same Postgres
    instance.

    `account` IS a real Django `ForeignKey` -- both `PMCChargeType` and
    `Account` are Finance-owned models living in this same app/database
    (spec Always).

    An inactive row (`active=False`) never produces a new posting, but
    existing postings from when it was active remain untouched -- no
    retroactive reversal (spec Never).
    """

    finance_pmc_profile = models.ForeignKey(
        FinancePMCProfile,
        on_delete=models.CASCADE,
        related_name="pmc_charge_types",
    )
    charge_id = models.BigIntegerField(
        help_text="units-backend Charge.id — not a cross-DB FK (AD-19 precedent)."
    )
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="pmc_charge_types",
    )
    active = models.BooleanField(default=True)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        # Post-review addition: the ORM's `.filter(...).first()` lookups in
        # `post_other_charge`/`pmc_charge_types` assume exactly one row per
        # pair -- without this, a concurrent-POST race (or a direct
        # `.create()` bypassing the endpoint's `update_or_create`) could
        # produce a duplicate, and `.first()` would silently pick an
        # arbitrary one instead of erroring.
        constraints = [
            models.UniqueConstraint(
                fields=["finance_pmc_profile", "charge_id"],
                name="unique_pmc_charge_type_per_profile_and_charge",
            )
        ]

    def __str__(self):
        return (
            f"PMCChargeType(finance_pmc_profile_id={self.finance_pmc_profile_id}, "
            f"charge_id={self.charge_id}, account_id={self.account_id}, "
            f"active={self.active})"
        )


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

    Story 5.1 (FR-16) makes `source_lease_transaction_id` nullable and adds
    `source_type`, so a manual, free-form entry (no originating
    `LeaseTransaction`) can coexist with posting-engine entries: a manual
    entry always has `source_type=MANUAL` and
    `source_lease_transaction_id=None`, and that field's presence always
    implies `source_type=LEASE_TRANSACTION` (spec Never — mutual exclusivity
    enforced at the model/serializer level, not just convention).
    `source_type` defaults to `LEASE_TRANSACTION` so every existing row
    backfills correctly without a data migration (spec Code Map).
    """

    LEASE_TRANSACTION = "LEASE_TRANSACTION"
    MANUAL = "MANUAL"

    SOURCE_TYPE_CHOICES = [
        (LEASE_TRANSACTION, "Lease Transaction"),
        (MANUAL, "Manual"),
    ]

    finance_pmc_profile = models.ForeignKey(
        FinancePMCProfile,
        on_delete=models.CASCADE,
        related_name="journal_entries",
    )
    source_lease_transaction_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend LeaseTransaction.id — not a cross-DB FK "
        "(AD-19 precedent). Null for source_type=MANUAL entries (FR-16).",
    )
    source_type = models.CharField(
        max_length=20,
        choices=SOURCE_TYPE_CHOICES,
        default=LEASE_TRANSACTION,
        help_text="Discriminates posting-engine entries (LEASE_TRANSACTION, "
        "the default) from operator-entered ones (MANUAL, Story 5.1/FR-16).",
    )
    source_status_transition = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Exact FROM_STATUS->TO_STATUS pair; idempotency key with "
        "source_lease_transaction_id (AD-14). Not applicable to manual "
        "entries (source_type=MANUAL) -- left blank, never fabricated "
        "(spec Always).",
    )
    memo = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Optional operator-entered note for a manual entry "
        "(source_type=MANUAL, Story 5.1/FR-16). Always blank for "
        "posting-engine entries -- no story populates it for those.",
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


class BankStatementLine(models.Model):
    """One row of an uploaded bank statement, scoped to a `FinancePMCProfile`
    (Story 4.1, FR-14).

    There is no PMC-owned bank-account entity anywhere in either service
    (units-backend's `Bank` model is a generic branch directory with no
    PMC/Property/Unit linkage, confirmed by investigation) -- human-confirmed
    to scope statement lines to `FinancePMCProfile` directly, no separate
    bank-account entity in Phase 1 (spec Intent).

    `finance_pmc_profile` is a real Django `ForeignKey` -- both
    `BankStatementLine` and `FinancePMCProfile` are Finance-owned models
    living in this same app/database, following the `Account`/`JournalEntry`
    FK convention (spec Boundaries & Constraints), never a plain
    `BigIntegerField` (that pattern is reserved for units-backend mirrors,
    AD-19).

    `amount` is `Decimal`, 2dp, matching `LedgerLine.debit`/`credit`'s
    convention (spec Boundaries & Constraints). `reconciled` defaults to
    `False` -- added now so Story 4.2's match-state recording doesn't need a
    second migration touching this table (spec Ask First).
    """

    finance_pmc_profile = models.ForeignKey(
        FinancePMCProfile,
        on_delete=models.CASCADE,
        related_name="bank_statement_lines",
    )
    statement_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=255)
    reconciled = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f"BankStatementLine(finance_pmc_profile_id={self.finance_pmc_profile_id}, "
            f"statement_date={self.statement_date}, amount={self.amount}, "
            f"reference={self.reference})"
        )


class BankStatementMatch(models.Model):
    """Join row recording one reconciliation decision between a
    `BankStatementLine` and a Bank Journal `JournalEntry` (Story 4.2, FR-15).

    A real Django model with real FKs on both sides -- both
    `BankStatementLine` and `JournalEntry` are Finance-owned models living in
    this same app/database, following the `Account`/`JournalEntry` FK
    convention (spec Boundaries & Constraints; never a plain
    `BigIntegerField`, that pattern is reserved for units-backend mirrors,
    AD-19).

    Chosen over adding fields directly to `JournalEntry`/`LedgerLine`
    (human-confirmed, spec Never): a rejected suggestion needs somewhere to
    persist without touching either Ledger model, and both sides must stay
    free for a later, different match. `status` models the row's lifecycle
    (`suggested` -> `confirmed`, or `suggested` -> `rejected` -> `confirmed`
    if re-tried) as a mutable field on one row, rather than multiple rows
    per pair (spec Design Notes).
    """

    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNRECONCILED = "unreconciled"

    STATUS_CHOICES = [
        (SUGGESTED, "Suggested"),
        (CONFIRMED, "Confirmed"),
        (REJECTED, "Rejected"),
        (UNRECONCILED, "Unreconciled"),
    ]

    bank_statement_line = models.ForeignKey(
        BankStatementLine,
        on_delete=models.CASCADE,
        related_name="matches",
    )
    journal_entry = models.ForeignKey(
        JournalEntry,
        on_delete=models.CASCADE,
        related_name="statement_matches",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        # Post-review patch: `_confirm_match`/`_reject_match` already assume
        # at most one row exists per (bank_statement_line, journal_entry)
        # pair -- they look one up and update it in place rather than ever
        # creating a second row for the same pair. This constraint enforces
        # that assumption at the database level instead of relying solely on
        # application-level check-then-act logic.
        unique_together = [("bank_statement_line", "journal_entry")]

    def __str__(self):
        return (
            f"BankStatementMatch(bank_statement_line_id={self.bank_statement_line_id}, "
            f"journal_entry_id={self.journal_entry_id}, status={self.status})"
        )


class LeaseTransactionRef(models.Model):
    """Read-only reference onto units-backend's LeaseTransaction table.

    The chain's starting point (Design Notes) — lets Finance read the
    triggering transaction's own data. Follows `PropertyManagmentCompanyRef`'s
    established unmanaged-model pattern (Story 1.2): `managed = False`, real
    `db_table`, minimal field list — only what a future posting story (2.2b)
    actually needs, not a full schema mirror.

    Story 2.5 adds `created` and `charge_id`: `created` (the transaction's
    own creation timestamp) drives the nearest-neighbor bounce-fee pairing
    query, and `charge_id` (the FK to units-backend's `Charge`, populated
    only when `cheque_type == OTHER_CHARGE`) is how Finance detects an
    `OTHER_CHARGE` transaction actually carries a fee to post against (spec
    Code Map).

    Story 3.5 adds `cheque_date`: the real units-backend column
    (`DateTimeField(null=True, blank=True)`, confirmed by reading
    `lease/models.py:144`), used by `compute_ageing` for the days-overdue
    computation (normalized to `.date()` before subtraction, spec Boundaries
    & Constraints) -- following the `created` field's existing precedent
    above, same type, same nullability.

    `id` is declared explicitly, `db_column="documents_ptr_id"`: units-backend's
    `LeaseTransaction(Documents)` is Django multi-table inheritance, so its
    real primary-key column is `documents_ptr_id`, not `id` -- confirmed via
    `information_schema.columns` against the live `lease_leasetransaction`
    table. Without this override Django assumes an implicit `id` AutoField
    that doesn't exist on this table, and every query 500s with
    `psycopg2.errors.UndefinedColumn: column lease_leasetransaction.id does
    not exist` -- this broke every real lease-transaction sync
    (`sync_lease_transaction`), not just a hypothetical case.
    """

    id = models.BigIntegerField(primary_key=True, db_column="documents_ptr_id")
    lease_id = models.BigIntegerField(
        help_text="units-backend Lease.id — not a cross-DB FK (next hop toward the PMC)."
    )
    amount = models.FloatField(null=True, blank=True)
    cheque_type = models.CharField(max_length=20)
    payment_type = models.CharField(max_length=20)
    status = models.CharField(max_length=20)
    created = models.DateTimeField(
        null=True,
        blank=True,
        help_text="units-backend LeaseTransaction.created — drives Story 2.5's "
        "nearest-neighbor OTHER_CHARGE/bounce pairing query.",
    )
    charge_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend Charge.id (LeaseTransaction.charge FK) — set "
        "only when cheque_type == OTHER_CHARGE; not a cross-DB FK (AD-19 "
        "precedent).",
    )
    cheque_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="units-backend LeaseTransaction.cheque_date -- Story 3.5's "
        "Ageing report days-overdue basis; normalize to .date() before "
        "subtracting from today (spec Boundaries & Constraints).",
    )

    class Meta:
        managed = False
        db_table = "lease_leasetransaction"

    def __str__(self):
        return f"LeaseTransactionRef(id={self.id}, lease_id={self.lease_id}, status={self.status})"


class ChargeRef(models.Model):
    """Read-only reference onto units-backend's Charge table (Story 2.5).

    Lets Finance read a bounce fee's `amount`/`vat_amount` without importing
    units-backend's `charges` app or code, following the same
    unmanaged-model pattern as the other `*Ref` models above (Story 1.2's
    precedent). Field list is kept minimal — only what `post_bounce_fee`
    actually needs (spec Code Map).
    """

    amount = models.FloatField()
    vat_amount = models.FloatField(default=0)

    class Meta:
        managed = False
        db_table = "charges_charge"

    def __str__(self):
        return f"ChargeRef(id={self.id}, amount={self.amount}, vat_amount={self.vat_amount})"


class LeaseRef(models.Model):
    """Read-only reference onto units-backend's Lease table.

    One hop toward the PMC from `LeaseTransactionRef.lease_id` (Design Notes).

    Story 2.6 adds `security_deposit` and `lease_status`: this story's
    trigger IS the `Lease` row itself (a new `post_save` signal, not a
    `LeaseTransaction`), so `post_security_deposit` reads the posting
    decision (`lease_status == "ACTIVE"` and `security_deposit` non-null/
    non-zero) directly off this ref model instead of a `LeaseTransactionRef`
    hop.
    """

    unit_id = models.BigIntegerField(
        help_text="units-backend Unit.id — not a cross-DB FK (next hop toward the PMC)."
    )
    security_deposit = models.FloatField(
        null=True,
        blank=True,
        help_text="units-backend Lease.security_deposit -- Story 2.6's posting amount.",
    )
    lease_status = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="units-backend Lease.lease_status -- Story 2.6 posts only when 'ACTIVE'.",
    )

    class Meta:
        managed = False
        db_table = "lease_lease"

    def __str__(self):
        return f"LeaseRef(id={self.id}, unit_id={self.unit_id})"


class UnitRef(models.Model):
    """Read-only reference onto units-backend's Unit table.

    Next hop toward the PMC from `LeaseRef.unit_id` (Design Notes).

    Story 2.7 adds `commission_percent`: the sole commission source for
    `post_commission_split` (spec Intent -- `Lease.commission` is not read).

    Story 3.1 adds `property_block_tower_id`: the alternate Owner-branch
    join path (`Unit.property_block_tower` -> `PropertyBlocks.property_id`),
    alongside the existing `parent_property_id` direct path, matching
    `org_scope.get_pmc_ids_for_user()`'s real Owner branch exactly.
    """

    parent_property_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend Property.id — not a cross-DB FK (next hop toward the PMC).",
    )
    property_block_tower_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend PropertyBlocks.id — not a cross-DB FK; the "
        "alternate Owner-branch join path to a Unit's PMC (Story 3.1).",
    )
    commission_percent = models.DecimalField(
        max_digits=5,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="units-backend Unit.commission_percent -- Story 2.7's sole "
        "commission source (spec Never: Lease.commission is not read).",
    )

    class Meta:
        managed = False
        db_table = "property_unit"

    def __str__(self):
        return f"UnitRef(id={self.id}, parent_property_id={self.parent_property_id})"


class UnitOwnerRef(models.Model):
    """Read-only reference onto units-backend's UnitOwner table (Story 2.7).

    Lets Finance read each owner's `ownership_percent` share for a Unit
    (AD-15/AD-9) without importing units-backend's `property` app or code,
    following the same unmanaged-model pattern as the other `*Ref` models
    above. Field list is kept minimal -- only what
    `post_commission_split`'s sum-to-100 validation actually needs (spec
    Code Map).

    Story 3.1 adds `owner_id`: joins back to `OwnerRef` for the PMC-scoping
    helper's Owner branch (`get_pmc_ids_for_user_profile`) -- Epic 2 only
    needed `unit_id`/`ownership_percent` to validate the sum-to-100
    invariant, never to identify which owner a row belongs to.
    """

    unit_id = models.BigIntegerField(
        help_text="units-backend Unit.id — not a cross-DB FK (AD-19 precedent)."
    )
    owner_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend Owner.id (UnitOwner.owner_id) — not a "
        "cross-DB FK; joins back to OwnerRef for PMC-scoping (Story 3.1).",
    )
    ownership_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="units-backend UnitOwner.ownership_percent -- AD-15's "
        "sum-to-100 invariant, validated (not itself posted) by "
        "post_commission_split.",
    )

    class Meta:
        managed = False
        db_table = "property_unitowner"

    def __str__(self):
        return f"UnitOwnerRef(id={self.id}, unit_id={self.unit_id}, ownership_percent={self.ownership_percent})"


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


class UserProfileRef(models.Model):
    """Read-only reference onto units-backend's UserProfile table (Story 3.1).

    Resolved by the JWT's `email` claim (units-backend's real mechanism,
    human-confirmed -- NOT `user_id`, per `utilities/decorator.py`'s
    `UserProfile.objects.filter(user__email=user_email)`). Also carries
    `token` for the DB-backed single-token revocation check
    (`user_profile.token == token`) and `user_id` (the underlying
    `auth_user.id`, Django's `User` model) so `authenticate_reporting_request`
    can check `auth_user.is_active` via a second, minimal lookup without a
    cross-app Django FK (Finance never imports units-backend's `User`/
    `user_service` code).

    `id` (the implicit PK) is `UserProfile.id` -- the same PK value
    `PropertyManagerRef`/`OwnerRef` share via Django multi-table inheritance
    in the real schema (units-backend's `PropertyManager`/`Owner` are
    `UserProfile` subclasses; their tables' PK IS `UserProfile.id`, exposed as
    `userprofile_ptr_id`). Finance's unmanaged refs are separate models (no
    inheritance across apps), so `get_pmc_ids_for_user_profile` looks up
    `PropertyManagerRef`/`OwnerRef` by `pk=user_profile_ref.id` directly.
    """

    email = models.EmailField(max_length=255, null=True, blank=True)
    token = models.TextField(null=True, blank=True)
    user_id = models.BigIntegerField(
        help_text="units-backend auth_user.id (Django's User model) — the "
        "FK column backing UserProfile.user_id; not a cross-DB FK."
    )

    class Meta:
        managed = False
        db_table = "user_service_userprofile"

    def __str__(self):
        return f"UserProfileRef(id={self.id}, email={self.email})"


class AuthUserRef(models.Model):
    """Read-only reference onto Django's own `auth_user` table (Story 3.1).

    Lets `authenticate_reporting_request` check `is_active` (matching
    units-backend's `user_profile.user.is_active` check) without importing
    units-backend's Django `User`/settings, by reading the same underlying
    table Django's default auth app always creates as `auth_user`, keyed by
    `UserProfileRef.user_id`.

    Post-review fix: also carries `email` -- units-backend's real profile
    lookup resolves via `UserProfile.objects.filter(user__email=...)`, i.e.
    the related `User.email`, NOT `UserProfile.email` (a separate,
    independently-settable field on `UserProfile` itself). Filtering
    directly on `UserProfileRef.email` would silently authenticate the
    wrong profile if the two ever diverge for a given user -- confirmed a
    real bug, not a stylistic difference, by direct comparison against
    `utilities/decorator.py`.
    """

    email = models.EmailField(max_length=255, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "auth_user"

    def __str__(self):
        return f"AuthUserRef(id={self.id}, is_active={self.is_active})"


class PropertyManagerRef(models.Model):
    """Read-only reference onto units-backend's PropertyManager table (Story 3.1).

    Multi-table inheritance in the real schema means this table's PK
    column is `userprofile_ptr_id` (Django's auto-derived parent-link
    column name), holding the same value as the owning `UserProfile.id` --
    so Finance looks this up by `pk=user_profile_ref.id`, matching
    `org_scope.get_pmc_ids_for_user()`'s own
    `PropertyManager.objects.filter(pk=user_profile.pk)` exactly.

    Post-review fix: `pk` is declared explicitly with
    `db_column="userprofile_ptr_id"` -- without this, Django's implicit
    `id` PK would generate SQL against a column that doesn't exist on the
    real table (confirmed: `user_service_propertymanager`'s only PK column
    is `userprofile_ptr_id`, no `id` column exists), causing every query
    to fail with an undefined-column error.
    """

    userprofile_ptr_id = models.BigIntegerField(
        primary_key=True,
        db_column="userprofile_ptr_id",
        help_text="units-backend UserProfile.id, via PropertyManager's "
        "multi-table-inheritance parent link (userprofile_ptr_id) — not a "
        "cross-DB FK.",
    )
    company_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="units-backend PropertyManagmentCompany.id "
        "(PropertyManager.company_id) — the PMCPMMapping fallback "
        "(AD-19 precedent); may be null.",
    )

    class Meta:
        managed = False
        db_table = "user_service_propertymanager"

    def __str__(self):
        return f"PropertyManagerRef(id={self.pk}, company_id={self.company_id})"


class OwnerRef(models.Model):
    """Read-only reference onto units-backend's Owner table (Story 3.1).

    No extra fields needed beyond the pk (spec Code Map) -- its real
    schema PK column is `userprofile_ptr_id` (multi-table inheritance),
    holding the same value as the owning `UserProfile.id`, so Finance
    looks this up by `pk=user_profile_ref.id`, matching
    `org_scope.get_pmc_ids_for_user()`'s own
    `Owner.objects.filter(pk=user_profile.pk)` exactly. Existence alone
    (a matching row) is what the PMC-scoping helper's Owner branch checks.

    Post-review fix: `pk` is declared explicitly with
    `db_column="userprofile_ptr_id"` -- same reasoning as `PropertyManagerRef`
    (the real table has no `id` column, only `userprofile_ptr_id`).
    """

    userprofile_ptr_id = models.BigIntegerField(
        primary_key=True,
        db_column="userprofile_ptr_id",
        help_text="units-backend UserProfile.id, via Owner's "
        "multi-table-inheritance parent link (userprofile_ptr_id) — not a "
        "cross-DB FK.",
    )

    class Meta:
        managed = False
        db_table = "user_service_owner"

    def __str__(self):
        return f"OwnerRef(id={self.pk})"


class PMCPMMappingRef(models.Model):
    """Read-only reference onto units-backend's PMCPMMapping table (Story 3.1).

    Resolves a PropertyManager's mapped pmc_ids -- the PropertyManager
    branch's primary path (before the `company_id` fallback), matching
    `org_scope.get_pmc_ids_for_user()`'s own
    `PMCPMMapping.objects.filter(pm=pm)` exactly.
    """

    pmc_id = models.BigIntegerField(
        help_text="units-backend PropertyManagmentCompany.id (PMCPMMapping.pmc_id) — not a cross-DB FK."
    )
    pm_id = models.BigIntegerField(
        help_text="units-backend PropertyManager.id (PMCPMMapping.pm_id) — not a cross-DB FK."
    )

    class Meta:
        managed = False
        db_table = "property_pmcpmmapping"

    def __str__(self):
        return f"PMCPMMappingRef(id={self.id}, pmc_id={self.pmc_id}, pm_id={self.pm_id})"


class PropertyBlocksRef(models.Model):
    """Read-only reference onto units-backend's PropertyBlocks table (Story 3.1).

    The alternate Owner-branch join path's second hop:
    `UnitRef.property_block_tower_id` -> `PropertyBlocksRef.property_id` ->
    `PropertyRef.pmc_id`, matching
    `org_scope.get_pmc_ids_for_user()`'s own
    `property_blocks__block_towers__unit_owners__owner` traversal.
    """

    property_id = models.BigIntegerField(
        help_text="units-backend Property.id (PropertyBlocks.property_id) — not a cross-DB FK."
    )

    class Meta:
        managed = False
        db_table = "property_propertyblocks"

    def __str__(self):
        return f"PropertyBlocksRef(id={self.id}, property_id={self.property_id})"

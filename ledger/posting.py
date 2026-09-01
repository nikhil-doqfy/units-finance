"""
Story 2.2b: Rent AR posting logic.

`post_rent_ar` is the posting engine for a RENT-type `LeaseTransaction`
(`cheque_type == RENT_CHEQUE`). It resolves the triggering transaction's
`FinancePMCProfile` via the Story 2.2a cross-service ref chain
(`LeaseTransactionRef.lease_id` -> `LeaseRef.unit_id` ->
`UnitRef.parent_property_id` -> `PropertyRef.pmc_id` -> `FinancePMCProfile`),
checks idempotency on `(source_lease_transaction_id, source_status_transition)`,
and -- if resolvable and not already posted -- atomically posts a balanced
Journal Entry debiting "AR — Tenants" and crediting "Rent Income" for
`LeaseTransactionRef.amount`.

Not built here (out of scope for this story, per the spec's Never section):
non-RENT cheque_type postings (bounce fees, deposits, commission splits --
Stories 2.5-2.7), any modification of units-backend code, and any cross-DB
Django ForeignKey in the resolution chain (walked via plain int field
lookups instead, per AD-19 precedent).

Story 2.3: Cheque clearing posting.

`post_cheque_clearing` posts a Bank Journal entry (debit Bank, credit
AR — Tenants) when a `LeaseTransaction.status` is currently `REALIZED` or
`CREDITED` and there is a prior `JournalEntry` for this
`lease_transaction_id` whose transition has not yet been cleared to the
current status. The "from" status is never received from units-backend --
it is inferred entirely from Finance's own `JournalEntry` history (the most
recent entry's "to" side is this posting's "from" side), since
units-backend's signal carries no before/after diff and adding one is out
of scope for this story (Design Notes).

Deliberately NOT gated on `cheque_type` (post-review, Spec Change Log): the
sole gate is the existing prior-`JournalEntry` check. A transaction with no
prior posting at all (e.g. a non-RENT type that never had a Rent AR
posting) correctly and intentionally hits the "no prior posting" failure
path below -- this naturally excludes cheque types with nothing to clear,
without a separate `cheque_type` filter.
"""
import logging

from django.db import transaction

from ledger.models import (
    Account,
    FinancePMCProfile,
    JournalEntry,
    LeaseRef,
    LeaseTransactionRef,
    LedgerLine,
    PropertyRef,
    UnitRef,
)

logger = logging.getLogger(__name__)

RENT_CHEQUE = "RENT_CHEQUE"

# This story only ever infers the CREATE case (Design Notes): Finance has no
# created/updated signal from units-backend, only the id, so "no prior
# JournalEntry for this id" is how Finance infers "first-ever sync."
CREATE_STATUS_TRANSITION = "CREATE-BALANCE"

AR_TENANTS_ACCOUNT_NAME = "AR — Tenants"
RENT_INCOME_ACCOUNT_NAME = "Rent Income"
BANK_ACCOUNT_NAME = "Bank"

# Trigger statuses for Story 2.3's cheque clearing posting -- a cheque is
# "cleared" once it reaches either of these (per the existing units-backend
# status flow; BOUNCED is Story 2.4, not handled here).
CHEQUE_STATUS_CREDITED = "CREDITED"
CHEQUE_STATUS_REALIZED = "REALIZED"
CLEARING_TRIGGER_STATUSES = (CHEQUE_STATUS_CREDITED, CHEQUE_STATUS_REALIZED)


def post_rent_ar(lease_transaction_id, txn=None):
    """Post a balanced Rent AR Journal Entry for a RENT LeaseTransaction.

    `txn` may be passed in by a caller that has already fetched the
    `LeaseTransactionRef` (e.g. `sync_lease_transaction`, which must inspect
    `cheque_type` before deciding to call this function at all) -- avoids a
    second, redundant query for the same row, and avoids the two lookups
    ever disagreeing if the row changed between them. If omitted, this
    function fetches it itself (e.g. for direct/test callers).

    Returns a dict: {"posted": bool, "reason": str} -- "reason" explains a
    skip/failure (duplicate, unresolvable PMC hop, missing transaction,
    missing amount, unconfigured Chart of Accounts, or non-RENT cheque_type)
    and is empty on a successful post. Never raises for an expected failure
    path -- the caller (the sync view) always gets a normal, non-5xx-worthy
    response back from this function; only a genuinely unexpected DB error
    (not modeled above) propagates out of the atomic block.
    """
    if txn is None:
        txn = LeaseTransactionRef.objects.filter(pk=lease_transaction_id).first()
    if txn is None:
        logger.error(
            "post_rent_ar: no LeaseTransaction found for id=%s -- cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "lease_transaction_not_found"}

    if txn.cheque_type != RENT_CHEQUE:
        # Not this story's trigger -- correctly a no-op, not a failure.
        return {"posted": False, "reason": "not_rent_cheque_type"}

    if txn.amount is None:
        logger.error(
            "post_rent_ar: LeaseTransaction id=%s has a null amount -- cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "missing_amount"}

    transition = CREATE_STATUS_TRANSITION

    if JournalEntry.objects.filter(
        source_lease_transaction_id=lease_transaction_id,
        source_status_transition=transition,
    ).exists():
        logger.info(
            "post_rent_ar: JournalEntry already exists for "
            "lease_transaction_id=%s transition=%s -- skipping duplicate post",
            lease_transaction_id,
            transition,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    # Walk the PMC resolution chain via plain int field lookups -- never a
    # cross-DB Django ForeignKey (spec Never). `.filter(pk=...).first()` at
    # each hop yields None on a missing/null id rather than raising, so a
    # broken chain surfaces as the "unresolvable PMC" failure path, not a
    # crash.
    lease = LeaseRef.objects.filter(pk=txn.lease_id).first()
    if lease is None:
        logger.error(
            "post_rent_ar: unresolvable PMC for lease_transaction_id=%s -- "
            "no Lease found for lease_id=%s",
            lease_transaction_id,
            txn.lease_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    unit = UnitRef.objects.filter(pk=lease.unit_id).first()
    if unit is None:
        logger.error(
            "post_rent_ar: unresolvable PMC for lease_transaction_id=%s -- "
            "no Unit found for unit_id=%s",
            lease_transaction_id,
            lease.unit_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    property_ = PropertyRef.objects.filter(pk=unit.parent_property_id).first()
    if property_ is None:
        logger.error(
            "post_rent_ar: unresolvable PMC for lease_transaction_id=%s -- "
            "no Property found for parent_property_id=%s",
            lease_transaction_id,
            unit.parent_property_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    profile = FinancePMCProfile.objects.filter(pmc_id=property_.pmc_id).first()
    if profile is None:
        logger.error(
            "post_rent_ar: unresolvable PMC for lease_transaction_id=%s -- "
            "no FinancePMCProfile found for pmc_id=%s",
            lease_transaction_id,
            property_.pmc_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    amount = txn.amount

    try:
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name=AR_TENANTS_ACCOUNT_NAME
        )
        rent_income_account = Account.objects.get(
            finance_pmc_profile=profile, name=RENT_INCOME_ACCOUNT_NAME
        )
    except Account.DoesNotExist:
        logger.error(
            "post_rent_ar: Chart of Accounts not configured for "
            "FinancePMCProfile pmc_id=%s (lease_transaction_id=%s) -- "
            "expected Accounts named %r and %r",
            profile.pmc_id,
            lease_transaction_id,
            AR_TENANTS_ACCOUNT_NAME,
            RENT_INCOME_ACCOUNT_NAME,
        )
        return {"posted": False, "reason": "chart_of_accounts_not_configured"}

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=lease_transaction_id,
            source_status_transition=transition,
        )

        LedgerLine.objects.create(
            journal_entry=entry,
            account=ar_account,
            debit=amount,
            credit=0,
        )
        LedgerLine.objects.create(
            journal_entry=entry,
            account=rent_income_account,
            debit=0,
            credit=amount,
        )

        debit_total = sum(line.debit for line in entry.lines.all())
        credit_total = sum(line.credit for line in entry.lines.all())
        if debit_total != credit_total or debit_total != amount:
            # A real conditional, not `assert` -- `assert` is stripped
            # entirely under Python's `-O` flag, which some Gunicorn
            # deployments use; this balance invariant must hold even then
            # (NFR-1). Raising here rolls back the whole atomic block.
            raise ValueError(
                f"post_rent_ar: unbalanced entry for lease_transaction_id="
                f"{lease_transaction_id} (debit={debit_total}, "
                f"credit={credit_total}, amount={amount})"
            )

    logger.info(
        "post_rent_ar: posted JournalEntry id=%s for lease_transaction_id=%s "
        "(pmc_id=%s, amount=%s)",
        entry.id,
        lease_transaction_id,
        profile.pmc_id,
        amount,
    )
    return {"posted": True, "reason": ""}


def post_cheque_clearing(lease_transaction_id, txn=None):
    """Post a Bank Journal entry when a cheque's status has cleared.

    `txn` may be passed in by a caller that has already fetched the
    `LeaseTransactionRef` (mirrors `post_rent_ar`'s `txn` param) -- avoids a
    second, redundant query for the same row. If omitted, this function
    fetches it itself.

    Returns a dict: {"posted": bool, "reason": str} -- same shape/contract as
    `post_rent_ar`. Never raises for an expected failure path; only a
    genuinely unexpected DB error propagates out of the atomic block.

    Naming note: the "cheque clearing" name reflects the motivating use case
    (FR-5) and CLEARING_TRIGGER_STATUSES is a real-status list, not a
    cheque-only concept -- since this function has no cheque_type gate, it
    also runs for (and would clear) any other transaction type with a prior
    posting and a status in that set. This is intentional (Spec Change Log),
    not a naming bug to "fix" by adding a gate back.
    """
    if txn is None:
        txn = LeaseTransactionRef.objects.filter(pk=lease_transaction_id).first()
    if txn is None:
        logger.error(
            "post_cheque_clearing: no LeaseTransaction found for id=%s -- "
            "cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "lease_transaction_not_found"}

    current_status = txn.status
    if current_status not in CLEARING_TRIGGER_STATUSES:
        # Not this story's trigger (e.g. BALANCE, or BOUNCED -- Story 2.4) --
        # correctly a no-op, not a failure.
        return {"posted": False, "reason": "not_clearing_trigger_status"}

    if txn.amount is None:
        logger.error(
            "post_cheque_clearing: LeaseTransaction id=%s has a null amount "
            "-- cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "missing_amount"}

    # Infer the "from" status from Finance's own JournalEntry history --
    # never received from units-backend (Design Notes). The most recent
    # prior entry's "to" side (the part after the "-") is the last-known-
    # posted status, i.e. this posting's "from" side.
    prior_entry = (
        JournalEntry.objects.filter(source_lease_transaction_id=lease_transaction_id)
        .order_by("-posted_at", "-id")
        .first()
    )
    if prior_entry is None:
        logger.error(
            "post_cheque_clearing: no prior JournalEntry exists for "
            "lease_transaction_id=%s -- cannot clear against a nonexistent "
            "AR balance",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "no_prior_posting"}

    last_known_status = prior_entry.source_status_transition.split("-")[-1]

    if last_known_status == current_status:
        # Self-loop -- the most recent posting already landed this exact
        # status (e.g. a retried sync call re-delivering the same event).
        # Skip without fabricating a nonsensical "X-X" transition.
        logger.info(
            "post_cheque_clearing: lease_transaction_id=%s already cleared "
            "to status=%s -- skipping duplicate post",
            lease_transaction_id,
            current_status,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    transition = f"{last_known_status}-{current_status}"

    if JournalEntry.objects.filter(
        source_lease_transaction_id=lease_transaction_id,
        source_status_transition=transition,
    ).exists():
        logger.info(
            "post_cheque_clearing: JournalEntry already exists for "
            "lease_transaction_id=%s transition=%s -- skipping duplicate post",
            lease_transaction_id,
            transition,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    # Walk the same PMC resolution chain post_rent_ar uses -- no new
    # resolution mechanism (spec Boundaries & Constraints).
    lease = LeaseRef.objects.filter(pk=txn.lease_id).first()
    if lease is None:
        logger.error(
            "post_cheque_clearing: unresolvable PMC for lease_transaction_id="
            "%s -- no Lease found for lease_id=%s",
            lease_transaction_id,
            txn.lease_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    unit = UnitRef.objects.filter(pk=lease.unit_id).first()
    if unit is None:
        logger.error(
            "post_cheque_clearing: unresolvable PMC for lease_transaction_id="
            "%s -- no Unit found for unit_id=%s",
            lease_transaction_id,
            lease.unit_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    property_ = PropertyRef.objects.filter(pk=unit.parent_property_id).first()
    if property_ is None:
        logger.error(
            "post_cheque_clearing: unresolvable PMC for lease_transaction_id="
            "%s -- no Property found for parent_property_id=%s",
            lease_transaction_id,
            unit.parent_property_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    profile = FinancePMCProfile.objects.filter(pmc_id=property_.pmc_id).first()
    if profile is None:
        logger.error(
            "post_cheque_clearing: unresolvable PMC for lease_transaction_id="
            "%s -- no FinancePMCProfile found for pmc_id=%s",
            lease_transaction_id,
            property_.pmc_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    amount = txn.amount

    try:
        bank_account = Account.objects.get(
            finance_pmc_profile=profile, name=BANK_ACCOUNT_NAME
        )
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name=AR_TENANTS_ACCOUNT_NAME
        )
    except Account.DoesNotExist:
        logger.error(
            "post_cheque_clearing: Chart of Accounts not configured for "
            "FinancePMCProfile pmc_id=%s (lease_transaction_id=%s) -- "
            "expected Accounts named %r and %r",
            profile.pmc_id,
            lease_transaction_id,
            BANK_ACCOUNT_NAME,
            AR_TENANTS_ACCOUNT_NAME,
        )
        return {"posted": False, "reason": "chart_of_accounts_not_configured"}

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=lease_transaction_id,
            source_status_transition=transition,
        )

        LedgerLine.objects.create(
            journal_entry=entry,
            account=bank_account,
            debit=amount,
            credit=0,
        )
        LedgerLine.objects.create(
            journal_entry=entry,
            account=ar_account,
            debit=0,
            credit=amount,
        )

        debit_total = sum(line.debit for line in entry.lines.all())
        credit_total = sum(line.credit for line in entry.lines.all())
        if debit_total != credit_total or debit_total != amount:
            # Same explicit if/raise balance check as post_rent_ar -- never
            # `assert` (NFR-1). Raising here rolls back the whole atomic
            # block.
            raise ValueError(
                f"post_cheque_clearing: unbalanced entry for "
                f"lease_transaction_id={lease_transaction_id} "
                f"(debit={debit_total}, credit={credit_total}, "
                f"amount={amount})"
            )

    logger.info(
        "post_cheque_clearing: posted JournalEntry id=%s for "
        "lease_transaction_id=%s (pmc_id=%s, amount=%s, transition=%s)",
        entry.id,
        lease_transaction_id,
        profile.pmc_id,
        amount,
        transition,
    )
    return {"posted": True, "reason": ""}

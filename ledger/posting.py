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

Story 2.4: Bounce reversal posting.

`post_bounce_reversal` posts a reversing Journal Entry (credit AR —
Tenants, debit Bounced Cheques) when a `LeaseTransaction.status` is
currently `BOUNCED`. The "from" status is inferred the same way Story 2.3
does -- from the most recent prior `JournalEntry`'s "to" side -- but the
`reversed_journal_entry` FK is deliberately NOT set to that most-recent
entry: it always points at the original Rent AR posting (the `CREATE-*`
entry) for this `lease_transaction_id`, found by filtering for
`source_status_transition` starting with `"CREATE-"` (Design Notes; AD-16).
If no prior `JournalEntry` exists at all, or no `CREATE-*` entry exists
among them, this fails loudly -- there's nothing to reverse.

Also NOT gated on `cheque_type` (same Story 2.3 precedent, spec Boundaries
& Constraints): the sole gate is status == BOUNCED plus the existing
prior-posting/idempotency checks.

Story 2.5: Bounce fee posting.

`post_bounce_fee` posts a Journal Entry (debit AR — Tenants, credit Bank
Charges/Fees, optional VAT Payable line) when an `OTHER_CHARGE`-type
`LeaseTransaction` is paired with an unresolved bounced transaction on the
same lease. Unlike every prior posting function in this module, the trigger
is NOT a status transition on the `LeaseTransaction` passed in — `Charge`
rows have no status transition of their own, and an `OTHER_CHARGE`
transaction's own `status` (BALANCE, etc.) is irrelevant to whether its fee
has been posted (spec Design Notes). Instead:

  - The gate is `txn.cheque_type == OTHER_CHARGE` and `txn.charge_id` is not
    null (spec Boundaries & Constraints; Code Map).
  - The pairing is a nearest-neighbor heuristic, not a structural link: among
    `BOUNCED` transactions on the same lease that already have a bounce
    reversal posted (Story 2.4's `BOUNCED`-suffixed JournalEntry) but no
    bounce-fee posting yet (no existing JournalEntry pairing them with any
    `OTHER_CHARGE` transaction), pick the nearest one created before this
    `OTHER_CHARGE` transaction (i.e. the most recent qualifying bounce,
    since a later bounce is the one this newly-created fee charge is most
    likely to be for) that has not itself already been paired with a
    *different* `OTHER_CHARGE` transaction.
  - "Consumed once matched" (Design Notes) is enforced entirely by
    idempotency-key lookups against existing `JournalEntry` rows -- there is
    no persisted "matched" flag on any units-backend row, and none is added
    (spec Never).
  - Idempotency key is `(other_charge_transaction_id, bounced_transaction_id)`
    -- NOT `(lease_transaction_id, from_status, to_status)` (spec Boundaries
    & Constraints, Design Notes) -- stored via `source_lease_transaction_id`
    (the `OTHER_CHARGE` transaction's id) and a `source_status_transition`
    string encoding the paired bounced transaction's id
    (`BOUNCE_FEE-FOR-<bounced_transaction_id>`), so the exact-tuple lookup
    reuses the same `JournalEntry` columns every other posting function
    uses, without repurposing them as an actual status pair.
  - If no unmatched candidate bounce exists, this is a genuine no-op (post
    nothing, wait) -- not a failure (spec Boundaries & Constraints).
"""
import logging

from django.db import transaction

from ledger.models import (
    Account,
    ChargeRef,
    FinancePMCProfile,
    JournalEntry,
    LeaseRef,
    LeaseTransactionRef,
    LedgerLine,
    PropertyRef,
    UnitRef,
)

"""
Story 2.6: Security deposit posting.

`post_security_deposit` posts a Journal Entry (debit Bank, credit Security
Deposits Held) when a `Lease` row's CURRENT `lease_status == "ACTIVE"` and
`security_deposit` is non-null and non-zero. Unlike every other posting
function in this module, the trigger is NOT a `LeaseTransaction` at all --
units-backend's second-ever signal fires a `post_save` on `Lease` itself
(spec Intent), so this function starts directly from `LeaseRef` rather than
walking in from a `LeaseTransactionRef.lease_id` hop (Design Notes: "one hop
shorter").

Idempotency key is the fixed pseudo-transition `(lease_id, "ACTIVATE")` --
deliberately coarser than every other function's inferred from/to status
pair, since `Lease` carries no per-row transition history to infer a "from"
status from (Design Notes). This treats deposit-posting as a one-time-per-
lease accounting fact: once posted, it is never re-posted, even if
`lease_status` later cycles INACTIVE/EXPIRED and back to ACTIVE (spec
Boundaries & Constraints).
"""

SECURITY_DEPOSITS_HELD_ACCOUNT_NAME = "Security Deposits Held"

LEASE_STATUS_ACTIVE = "ACTIVE"

# Fixed pseudo-transition -- not a real from/to status pair (Design Notes).
# One deposit posting per lease, ever, keyed only on lease_id.
DEPOSIT_ACTIVATE_TRANSITION = "ACTIVATE"

logger = logging.getLogger(__name__)

RENT_CHEQUE = "RENT_CHEQUE"

# This story only ever infers the CREATE case (Design Notes): Finance has no
# created/updated signal from units-backend, only the id, so "no prior
# JournalEntry for this id" is how Finance infers "first-ever sync."
CREATE_STATUS_TRANSITION = "CREATE-BALANCE"

AR_TENANTS_ACCOUNT_NAME = "AR — Tenants"
RENT_INCOME_ACCOUNT_NAME = "Rent Income"
BANK_ACCOUNT_NAME = "Bank"
BOUNCED_CHEQUES_ACCOUNT_NAME = "Bounced Cheques"
BANK_CHARGES_FEE_INCOME_ACCOUNT_NAME = "Bank Charges/Fees"
VAT_PAYABLE_ACCOUNT_NAME = "VAT Payable"

# Trigger statuses for Story 2.3's cheque clearing posting -- a cheque is
# "cleared" once it reaches either of these (per the existing units-backend
# status flow; BOUNCED is Story 2.4, not handled here).
CHEQUE_STATUS_CREDITED = "CREDITED"
CHEQUE_STATUS_REALIZED = "REALIZED"
CLEARING_TRIGGER_STATUSES = (CHEQUE_STATUS_CREDITED, CHEQUE_STATUS_REALIZED)

# Trigger status for Story 2.4's bounce reversal posting.
CHEQUE_STATUS_BOUNCED = "BOUNCED"

# Story 2.5's trigger cheque_type -- an ad-hoc fee charge, populated via
# units-backend's generic "Other Charge" flow (spec Intent).
OTHER_CHARGE = "OTHER_CHARGE"

# Prefix identifying a Story 2.4 bounce-reversal JournalEntry among a
# lease_transaction_id's history -- its source_status_transition is always
# "<something>-BOUNCED" (Design Notes: post_bounce_reversal infers the
# "from" side, but always ends in BOUNCED).
BOUNCE_TRANSITION_SUFFIX = "-BOUNCED"

# Story 2.5's idempotency-key encoding (Design Notes): the pairing has no
# status transition of its own, so source_status_transition instead encodes
# which bounced_transaction_id this OTHER_CHARGE transaction (the row
# JournalEntry.source_lease_transaction_id already points at) was paired
# with. The exact-tuple lookup this module's other posting functions already
# do on (source_lease_transaction_id, source_status_transition) becomes,
# for this story, exactly the spec's
# (other_charge_transaction_id, bounced_transaction_id) idempotency key.
BOUNCE_FEE_TRANSITION_PREFIX = "BOUNCE_FEE-FOR-"


def _bounce_fee_transition(bounced_transaction_id):
    return f"{BOUNCE_FEE_TRANSITION_PREFIX}{bounced_transaction_id}"

# Prefix identifying the original Rent AR posting's source_status_transition
# among a lease_transaction_id's JournalEntry history (Design Notes; AD-16) --
# always "CREATE-<something>", exactly one per id since post_rent_ar posts
# once, guarded by its own idempotency check.
CREATE_TRANSITION_PREFIX = "CREATE-"


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


def post_bounce_reversal(lease_transaction_id, txn=None):
    """Post a reversing Journal Entry when a cheque bounces.

    `txn` may be passed in by a caller that has already fetched the
    `LeaseTransactionRef` (mirrors `post_rent_ar`/`post_cheque_clearing`'s
    `txn` param) -- avoids a second, redundant query for the same row. If
    omitted, this function fetches it itself.

    Returns a dict: {"posted": bool, "reason": str} -- same shape/contract as
    `post_rent_ar`/`post_cheque_clearing`. Never raises for an expected
    failure path; only a genuinely unexpected DB error propagates out of the
    atomic block.

    Deliberately NOT gated on `cheque_type` (spec Boundaries & Constraints,
    same Story 2.3 precedent) -- the sole gate is status == BOUNCED plus the
    existing prior-posting/idempotency checks below.
    """
    if txn is None:
        txn = LeaseTransactionRef.objects.filter(pk=lease_transaction_id).first()
    if txn is None:
        logger.error(
            "post_bounce_reversal: no LeaseTransaction found for id=%s -- "
            "cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "lease_transaction_not_found"}

    current_status = txn.status
    if current_status != CHEQUE_STATUS_BOUNCED:
        # Not this story's trigger -- correctly a no-op, not a failure.
        return {"posted": False, "reason": "not_bounced_status"}

    if txn.amount is None:
        logger.error(
            "post_bounce_reversal: LeaseTransaction id=%s has a null amount "
            "-- cannot post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "missing_amount"}

    # Infer the "from" status from Finance's own JournalEntry history --
    # same mechanism as post_cheque_clearing (Design Notes). The most recent
    # prior entry's "to" side is the last-known-posted status, i.e. this
    # posting's "from" side.
    prior_entry = (
        JournalEntry.objects.filter(source_lease_transaction_id=lease_transaction_id)
        .order_by("-posted_at", "-id")
        .first()
    )
    if prior_entry is None:
        logger.error(
            "post_bounce_reversal: no prior JournalEntry exists for "
            "lease_transaction_id=%s -- the RENT AR posting never happened, "
            "cannot reverse",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "no_prior_posting"}

    last_known_status = prior_entry.source_status_transition.split("-")[-1]

    if last_known_status == current_status:
        # Self-loop -- the most recent posting already landed this exact
        # status (e.g. a retried sync call re-delivering the same event).
        # Skip without fabricating a nonsensical "X-X" transition.
        logger.info(
            "post_bounce_reversal: lease_transaction_id=%s already reversed "
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
            "post_bounce_reversal: JournalEntry already exists for "
            "lease_transaction_id=%s transition=%s -- skipping duplicate post",
            lease_transaction_id,
            transition,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    # The reversed_journal_entry FK must point at the ORIGINAL Rent AR
    # posting (the CREATE-* entry), never the most-recently-inferred "from"
    # entry above, which may itself already be a clearing/other reversal
    # (Design Notes; AD-16). Exactly one CREATE-* entry per id, since
    # post_rent_ar posts once, guarded by its own idempotency check.
    original_entry = (
        JournalEntry.objects.filter(
            source_lease_transaction_id=lease_transaction_id,
            source_status_transition__startswith=CREATE_TRANSITION_PREFIX,
        )
        .order_by("-posted_at", "-id")
        .first()
    )
    if original_entry is None:
        logger.error(
            "post_bounce_reversal: no original CREATE-* JournalEntry exists "
            "for lease_transaction_id=%s -- nothing to reverse",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "no_original_posting"}

    # Walk the same PMC resolution chain post_rent_ar/post_cheque_clearing
    # use -- no new resolution mechanism (spec Boundaries & Constraints).
    lease = LeaseRef.objects.filter(pk=txn.lease_id).first()
    if lease is None:
        logger.error(
            "post_bounce_reversal: unresolvable PMC for lease_transaction_id="
            "%s -- no Lease found for lease_id=%s",
            lease_transaction_id,
            txn.lease_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    unit = UnitRef.objects.filter(pk=lease.unit_id).first()
    if unit is None:
        logger.error(
            "post_bounce_reversal: unresolvable PMC for lease_transaction_id="
            "%s -- no Unit found for unit_id=%s",
            lease_transaction_id,
            lease.unit_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    property_ = PropertyRef.objects.filter(pk=unit.parent_property_id).first()
    if property_ is None:
        logger.error(
            "post_bounce_reversal: unresolvable PMC for lease_transaction_id="
            "%s -- no Property found for parent_property_id=%s",
            lease_transaction_id,
            unit.parent_property_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    profile = FinancePMCProfile.objects.filter(pmc_id=property_.pmc_id).first()
    if profile is None:
        logger.error(
            "post_bounce_reversal: unresolvable PMC for lease_transaction_id="
            "%s -- no FinancePMCProfile found for pmc_id=%s",
            lease_transaction_id,
            property_.pmc_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    # Reversal amount always matches the ORIGINAL Rent AR posting's amount
    # for this lease_transaction_id -- read from LeaseTransactionRef.amount
    # (same field, same value, consistent with Stories 2.2b/2.3's
    # convention), not recomputed (spec Boundaries & Constraints).
    amount = txn.amount

    try:
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name=AR_TENANTS_ACCOUNT_NAME
        )
        bounced_cheques_account = Account.objects.get(
            finance_pmc_profile=profile, name=BOUNCED_CHEQUES_ACCOUNT_NAME
        )
    except Account.DoesNotExist:
        logger.error(
            "post_bounce_reversal: Chart of Accounts not configured for "
            "FinancePMCProfile pmc_id=%s (lease_transaction_id=%s) -- "
            "expected Accounts named %r and %r",
            profile.pmc_id,
            lease_transaction_id,
            AR_TENANTS_ACCOUNT_NAME,
            BOUNCED_CHEQUES_ACCOUNT_NAME,
        )
        return {"posted": False, "reason": "chart_of_accounts_not_configured"}

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=lease_transaction_id,
            source_status_transition=transition,
            reversed_journal_entry=original_entry,
        )

        # Credit AR — Tenants, debit Bounced Cheques -- the cheque never
        # cleared the bank, so it never touches Bank (spec Intent).
        LedgerLine.objects.create(
            journal_entry=entry,
            account=bounced_cheques_account,
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
            # Same explicit if/raise balance check as post_rent_ar/
            # post_cheque_clearing -- never `assert` (NFR-1). Raising here
            # rolls back the whole atomic block.
            raise ValueError(
                f"post_bounce_reversal: unbalanced entry for "
                f"lease_transaction_id={lease_transaction_id} "
                f"(debit={debit_total}, credit={credit_total}, "
                f"amount={amount})"
            )

    logger.info(
        "post_bounce_reversal: posted JournalEntry id=%s for "
        "lease_transaction_id=%s (pmc_id=%s, amount=%s, transition=%s, "
        "reversed_journal_entry_id=%s)",
        entry.id,
        lease_transaction_id,
        profile.pmc_id,
        amount,
        transition,
        original_entry.id,
    )
    return {"posted": True, "reason": ""}


def post_bounce_fee(lease_transaction_id, txn=None):
    """Post a Journal Entry for a bounce fee paired to an unresolved bounce.

    `txn` may be passed in by a caller that has already fetched the
    `LeaseTransactionRef` (mirrors the `txn` param on every other posting
    function in this module) -- avoids a second, redundant query for the
    same row. If omitted, this function fetches it itself.

    Returns a dict: {"posted": bool, "reason": str} -- same shape/contract as
    every other posting function here. Never raises for an expected failure
    path; only a genuinely unexpected DB error propagates out of the atomic
    block.

    Trigger: `txn.cheque_type == OTHER_CHARGE` and `txn.charge_id` is not
    null (spec Boundaries & Constraints). Neither `txn.status` nor any
    status transition gates this function -- a Charge/OTHER_CHARGE pairing
    has no status transition of its own (Design Notes).
    """
    if txn is None:
        txn = LeaseTransactionRef.objects.filter(pk=lease_transaction_id).first()
    if txn is None:
        logger.error(
            "post_bounce_fee: no LeaseTransaction found for id=%s -- cannot "
            "post",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "lease_transaction_not_found"}

    if txn.cheque_type != OTHER_CHARGE or not txn.charge_id:
        # Not this story's trigger -- correctly a no-op, not a failure.
        return {"posted": False, "reason": "not_other_charge_type"}

    if txn.created is None:
        # The nearest-neighbor pairing query is created-timestamp-ordered;
        # without one there is no principled way to pick a candidate bounce.
        logger.error(
            "post_bounce_fee: OTHER_CHARGE LeaseTransaction id=%s has no "
            "created timestamp -- cannot run the pairing query",
            lease_transaction_id,
        )
        return {"posted": False, "reason": "missing_created_timestamp"}

    # Candidate bounced transactions: every JournalEntry on this same lease
    # whose transition is a Story 2.4 bounce reversal (ends "-BOUNCED"),
    # i.e. one BOUNCED lease_transaction_id per such entry. Scoped to the
    # same lease via LeaseTransactionRef.lease_id (spec Boundaries &
    # Constraints: "on the same Lease").
    bounced_ids_on_lease = list(
        LeaseTransactionRef.objects.filter(
            lease_id=txn.lease_id, status=CHEQUE_STATUS_BOUNCED
        ).values_list("id", flat=True)
    )
    if not bounced_ids_on_lease:
        # Steady state: no bounce exists yet on this lease at all -- post
        # nothing and wait (spec Boundaries & Constraints), not a failure.
        return {"posted": False, "reason": "no_unresolved_bounce"}

    bounce_reversal_entries = (
        JournalEntry.objects.filter(
            source_lease_transaction_id__in=bounced_ids_on_lease,
            source_status_transition__endswith=BOUNCE_TRANSITION_SUFFIX,
        )
        .exclude(source_status_transition__startswith=BOUNCE_FEE_TRANSITION_PREFIX)
        .order_by("posted_at", "id")
    )

    # "Consumed once matched" (Design Notes): a bounced_transaction_id
    # already paired with a DIFFERENT OTHER_CHARGE transaction (a
    # BOUNCE_FEE-FOR-<id> JournalEntry whose own source_lease_transaction_id
    # is not this OTHER_CHARGE transaction's id) is excluded from candidate
    # matching -- checked via existing JournalEntry history, no persisted
    # "matched" flag anywhere. A pairing already recorded against THIS SAME
    # OTHER_CHARGE transaction is deliberately NOT excluded here -- that
    # case is a duplicate re-sync, not a re-pairing, and must fall through
    # to the idempotency check below instead (spec: "never re-pair ... to a
    # different bounce" -- re-finding the same pairing for the same
    # transaction is not a re-pair).
    already_paired_bounce_ids = set(
        JournalEntry.objects.filter(
            source_status_transition__startswith=BOUNCE_FEE_TRANSITION_PREFIX,
            source_status_transition__in=[
                _bounce_fee_transition(bid) for bid in bounced_ids_on_lease
            ],
        )
        .exclude(source_lease_transaction_id=lease_transaction_id)
        .values_list("source_status_transition", flat=True)
    )
    already_paired_bounce_ids = {
        s[len(BOUNCE_FEE_TRANSITION_PREFIX) :] for s in already_paired_bounce_ids
    }

    # Nearest-neighbor pick: the OLDEST unresolved bounce created before this
    # OTHER_CHARGE transaction, still unmatched (spec Boundaries &
    # Constraints; Design Notes -- confirmed by the "two simultaneous
    # bounces" I/O matrix row: the fee pairs with the OLDER unresolved
    # bounce). `bounce_reversal_entries` is already ordered oldest-first.
    bounced_transaction_id = None
    for entry in bounce_reversal_entries:
        candidate_id = entry.source_lease_transaction_id
        if str(candidate_id) in already_paired_bounce_ids:
            continue
        candidate_txn = LeaseTransactionRef.objects.filter(pk=candidate_id).first()
        if candidate_txn is None:
            continue
        if candidate_txn.created is None or candidate_txn.created >= txn.created:
            # The bounce must have happened before this OTHER_CHARGE
            # transaction was created (spec Boundaries & Constraints: "oldest
            # OTHER_CHARGE transaction created after the bounce event") --
            # symmetrically, from this OTHER_CHARGE transaction's own point
            # of view, only a bounce created before it is eligible. A null
            # `created` on the candidate means we cannot verify the ordering,
            # so it must be excluded rather than accepted by default.
            continue
        bounced_transaction_id = candidate_id
        break

    if bounced_transaction_id is None:
        # Genuine steady state (spec Boundaries & Constraints; I/O matrix
        # "No fee charge yet" row's mirror image): no unmatched bounce
        # exists yet for this fee charge to pair with. Post nothing and
        # wait -- not a failure.
        return {"posted": False, "reason": "no_unresolved_bounce"}

    transition = _bounce_fee_transition(bounced_transaction_id)

    # Idempotency key: (other_charge_transaction_id, bounced_transaction_id)
    # -- encoded as (source_lease_transaction_id, source_status_transition),
    # NOT (lease_transaction_id, from_status, to_status) (spec Boundaries &
    # Constraints, Design Notes).
    if JournalEntry.objects.filter(
        source_lease_transaction_id=lease_transaction_id,
        source_status_transition=transition,
    ).exists():
        logger.info(
            "post_bounce_fee: JournalEntry already exists for "
            "other_charge_transaction_id=%s bounced_transaction_id=%s -- "
            "skipping duplicate post",
            lease_transaction_id,
            bounced_transaction_id,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    charge = ChargeRef.objects.filter(pk=txn.charge_id).first()
    if charge is None:
        logger.error(
            "post_bounce_fee: no Charge found for charge_id=%s "
            "(other_charge_transaction_id=%s) -- cannot post",
            txn.charge_id,
            lease_transaction_id,
        )
        return {"posted": False, "reason": "charge_not_found"}

    # Walk the same PMC resolution chain every other posting function uses --
    # resolved via the OTHER_CHARGE transaction's own lease_id (spec
    # Boundaries & Constraints: "same chain, different starting transaction").
    lease = LeaseRef.objects.filter(pk=txn.lease_id).first()
    if lease is None:
        logger.error(
            "post_bounce_fee: unresolvable PMC for "
            "other_charge_transaction_id=%s -- no Lease found for "
            "lease_id=%s",
            lease_transaction_id,
            txn.lease_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    unit = UnitRef.objects.filter(pk=lease.unit_id).first()
    if unit is None:
        logger.error(
            "post_bounce_fee: unresolvable PMC for "
            "other_charge_transaction_id=%s -- no Unit found for unit_id=%s",
            lease_transaction_id,
            lease.unit_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    property_ = PropertyRef.objects.filter(pk=unit.parent_property_id).first()
    if property_ is None:
        logger.error(
            "post_bounce_fee: unresolvable PMC for "
            "other_charge_transaction_id=%s -- no Property found for "
            "parent_property_id=%s",
            lease_transaction_id,
            unit.parent_property_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    profile = FinancePMCProfile.objects.filter(pmc_id=property_.pmc_id).first()
    if profile is None:
        logger.error(
            "post_bounce_fee: unresolvable PMC for "
            "other_charge_transaction_id=%s -- no FinancePMCProfile found "
            "for pmc_id=%s",
            lease_transaction_id,
            property_.pmc_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    fee_amount = charge.amount
    if fee_amount is None:
        logger.error(
            "post_bounce_fee: Charge charge_id=%s has no amount "
            "(other_charge_transaction_id=%s) -- cannot post",
            txn.charge_id,
            lease_transaction_id,
        )
        return {"posted": False, "reason": "missing_charge_amount"}
    vat_amount = charge.vat_amount or 0

    try:
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name=AR_TENANTS_ACCOUNT_NAME
        )
        fee_income_account = Account.objects.get(
            finance_pmc_profile=profile, name=BANK_CHARGES_FEE_INCOME_ACCOUNT_NAME
        )
        vat_payable_account = None
        if vat_amount:
            vat_payable_account = Account.objects.get(
                finance_pmc_profile=profile, name=VAT_PAYABLE_ACCOUNT_NAME
            )
    except Account.DoesNotExist:
        logger.error(
            "post_bounce_fee: Chart of Accounts not configured for "
            "FinancePMCProfile pmc_id=%s (other_charge_transaction_id=%s) "
            "-- expected Accounts named %r, %r%s",
            profile.pmc_id,
            lease_transaction_id,
            AR_TENANTS_ACCOUNT_NAME,
            BANK_CHARGES_FEE_INCOME_ACCOUNT_NAME,
            f" and {VAT_PAYABLE_ACCOUNT_NAME!r}" if vat_amount else "",
        )
        return {"posted": False, "reason": "chart_of_accounts_not_configured"}

    # AR debit == (Bank Charges/Fee Income credit + VAT Payable credit), per
    # the spec's explicit balance framing -- AR carries the tenant's full
    # amount owed (fee + VAT), Fee Income carries only the fee itself, and
    # VAT Payable carries only the VAT (spec Boundaries & Constraints).
    ar_debit_total = fee_amount + vat_amount

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=lease_transaction_id,
            source_status_transition=transition,
        )

        LedgerLine.objects.create(
            journal_entry=entry,
            account=ar_account,
            debit=ar_debit_total,
            credit=0,
        )
        LedgerLine.objects.create(
            journal_entry=entry,
            account=fee_income_account,
            debit=0,
            credit=fee_amount,
        )
        if vat_amount:
            LedgerLine.objects.create(
                journal_entry=entry,
                account=vat_payable_account,
                debit=0,
                credit=vat_amount,
            )

        debit_total = sum(line.debit for line in entry.lines.all())
        credit_total = sum(line.credit for line in entry.lines.all())
        if debit_total != credit_total or debit_total != ar_debit_total:
            # Same explicit if/raise balance check as every other posting
            # function here -- never `assert` (NFR-1). Raising here rolls
            # back the whole atomic block.
            raise ValueError(
                f"post_bounce_fee: unbalanced entry for "
                f"other_charge_transaction_id={lease_transaction_id} "
                f"(debit={debit_total}, credit={credit_total}, "
                f"fee_amount={fee_amount}, vat_amount={vat_amount})"
            )

    # Heuristic-match logging (spec Boundaries & Constraints): every
    # successful post logs at INFO or above, naming both transaction ids, so
    # a suspected mismatch is traceable and manually correctable -- this is
    # not a silent best-effort guess.
    logger.info(
        "post_bounce_fee: posted JournalEntry id=%s pairing "
        "other_charge_transaction_id=%s with bounced_transaction_id=%s via "
        "nearest-neighbor heuristic match (pmc_id=%s, fee_amount=%s, "
        "vat_amount=%s) -- HEURISTIC MATCH, verify/reconcile manually if a "
        "mismatch is suspected",
        entry.id,
        lease_transaction_id,
        bounced_transaction_id,
        profile.pmc_id,
        fee_amount,
        vat_amount,
    )
    return {"posted": True, "reason": ""}


def post_security_deposit(lease_id, lease=None):
    """Post a balanced security deposit Journal Entry for an ACTIVE Lease.

    `lease` may be passed in by a caller that has already fetched the
    `LeaseRef` (e.g. `sync_lease`, which must inspect `lease_status`/
    `security_deposit` before deciding whether to post at all) -- avoids a
    second, redundant query for the same row. If omitted, this function
    fetches it itself (e.g. for direct/test callers).

    Trigger (spec Boundaries & Constraints): on the CURRENT `Lease` row,
    `lease_status == "ACTIVE"` AND `security_deposit` is not null and not
    zero. Any other state (not yet ACTIVE, or ACTIVE with no deposit) is a
    genuine no-op -- wait, not a failure -- since every `Lease.save()` fires
    this signal, and re-syncs of a still-DRAFT or already-posted lease are
    expected and frequent.

    Idempotency key is the fixed pseudo-transition `(lease_id, "ACTIVATE")`
    -- NOT an inferred from/to status pair (Design Notes: `Lease` carries no
    per-row transition history to infer one from). One deposit posting per
    lease, ever, regardless of `lease_status` cycling back to INACTIVE/
    EXPIRED and returning to ACTIVE later.

    Returns a dict: {"posted": bool, "reason": str} -- same shape/contract as
    every other posting function in this module. Never raises for an
    expected failure path; only a genuinely unexpected DB error propagates
    out of the atomic block.
    """
    if lease is None:
        lease = LeaseRef.objects.filter(pk=lease_id).first()
    if lease is None:
        logger.error(
            "post_security_deposit: no Lease found for id=%s -- cannot post",
            lease_id,
        )
        return {"posted": False, "reason": "lease_not_found"}

    if lease.lease_status != LEASE_STATUS_ACTIVE:
        # Not yet active -- correctly a no-op, not a failure (steady state:
        # every Lease.save() fires this signal, most of which aren't
        # activations at all).
        return {"posted": False, "reason": "not_active"}

    if not lease.security_deposit:
        # ACTIVE but no deposit amount (null or zero) -- correctly a no-op,
        # not a failure (spec Never: never post when security_deposit is
        # null or zero).
        return {"posted": False, "reason": "no_deposit_amount"}

    if JournalEntry.objects.filter(
        source_lease_transaction_id=lease_id,
        source_status_transition=DEPOSIT_ACTIVATE_TRANSITION,
    ).exists():
        logger.info(
            "post_security_deposit: JournalEntry already exists for "
            "lease_id=%s transition=%s -- skipping duplicate post "
            "(already posted once, ever -- includes re-activation cycles)",
            lease_id,
            DEPOSIT_ACTIVATE_TRANSITION,
        )
        return {"posted": False, "reason": "duplicate_skip"}

    # PMC resolution chain starts directly from LeaseRef.unit_id -- one hop
    # shorter than post_rent_ar/post_cheque_clearing/post_bounce_reversal,
    # which all start from a LeaseTransactionRef.lease_id hop (Design
    # Notes). This story's trigger already IS the Lease row.
    unit = UnitRef.objects.filter(pk=lease.unit_id).first()
    if unit is None:
        logger.error(
            "post_security_deposit: unresolvable PMC for lease_id=%s -- "
            "no Unit found for unit_id=%s",
            lease_id,
            lease.unit_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    property_ = PropertyRef.objects.filter(pk=unit.parent_property_id).first()
    if property_ is None:
        logger.error(
            "post_security_deposit: unresolvable PMC for lease_id=%s -- "
            "no Property found for parent_property_id=%s",
            lease_id,
            unit.parent_property_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    profile = FinancePMCProfile.objects.filter(pmc_id=property_.pmc_id).first()
    if profile is None:
        logger.error(
            "post_security_deposit: unresolvable PMC for lease_id=%s -- "
            "no FinancePMCProfile found for pmc_id=%s",
            lease_id,
            property_.pmc_id,
        )
        return {"posted": False, "reason": "unresolvable_pmc"}

    amount = lease.security_deposit

    try:
        bank_account = Account.objects.get(
            finance_pmc_profile=profile, name=BANK_ACCOUNT_NAME
        )
        security_deposits_held_account = Account.objects.get(
            finance_pmc_profile=profile, name=SECURITY_DEPOSITS_HELD_ACCOUNT_NAME
        )
    except Account.DoesNotExist:
        logger.error(
            "post_security_deposit: Chart of Accounts not configured for "
            "FinancePMCProfile pmc_id=%s (lease_id=%s) -- expected Accounts "
            "named %r and %r",
            profile.pmc_id,
            lease_id,
            BANK_ACCOUNT_NAME,
            SECURITY_DEPOSITS_HELD_ACCOUNT_NAME,
        )
        return {"posted": False, "reason": "chart_of_accounts_not_configured"}

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=lease_id,
            source_status_transition=DEPOSIT_ACTIVATE_TRANSITION,
        )

        # Debit Bank, credit Security Deposits Held (a liability) -- never
        # Rent Income, under any circumstance (spec Never).
        LedgerLine.objects.create(
            journal_entry=entry,
            account=bank_account,
            debit=amount,
            credit=0,
        )
        LedgerLine.objects.create(
            journal_entry=entry,
            account=security_deposits_held_account,
            debit=0,
            credit=amount,
        )

        debit_total = sum(line.debit for line in entry.lines.all())
        credit_total = sum(line.credit for line in entry.lines.all())
        if debit_total != credit_total or debit_total != amount:
            # Explicit if/raise balance check -- never `assert` (NFR-1).
            # Raising here rolls back the whole atomic block.
            raise ValueError(
                f"post_security_deposit: unbalanced entry for lease_id="
                f"{lease_id} (debit={debit_total}, credit={credit_total}, "
                f"amount={amount})"
            )

    logger.info(
        "post_security_deposit: posted JournalEntry id=%s for lease_id=%s "
        "(pmc_id=%s, amount=%s)",
        entry.id,
        lease_id,
        profile.pmc_id,
        amount,
    )
    return {"posted": True, "reason": ""}

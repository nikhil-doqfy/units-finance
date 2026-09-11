"""
Story 5.1: Manual journal entries (Ledger -> General -> Cash -> Manual
Entries), FR-16.

`post_manual_entry` is the create-only posting path for an operator-entered
`JournalEntry` that does not originate from a `LeaseTransaction` event --
corrections, cash transactions, and (per Epic 5's dependency flow) Story
5.2's future charge-type postings. It never touches `posting.py`'s existing
posting functions (spec Never) -- this is a new, independent creation path,
following the same atomicity/balance-check style (`transaction.atomic()`,
an explicit if/raise balance check, never `assert`) as `post_rent_ar` et al.

Validation performed here, all before any row is created (spec I/O matrix):
  - At least two lines are required, each a dict with a valid integer
    `account_id`.
  - `debit`/`credit` are never negative, and a single line never has both
    nonzero (post-review addition: neither is valid double-entry
    accounting, spec Never).
  - `sum(debits) == sum(credits)` exactly -- no unbalanced entry is ever
    persisted (NFR-1's atomicity invariant, extended to manual entries).
  - The entry's total is nonzero -- an all-zero-value entry is rejected as
    meaningless (post-review addition, spec Never).
  - Every `account_id` must resolve to an `Account` belonging to the
    requested `finance_pmc_profile` -- no cross-PMC posting (spec Always).

On success, posts one `JournalEntry(source_type=MANUAL,
source_lease_transaction_id=None, source_status_transition="")` plus one
`LedgerLine` per input line, inside a single `transaction.atomic()` block
(spec Always) -- all-or-nothing, matching `posting.py`'s pattern.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ledger.models import Account, JournalEntry, LedgerLine

MIN_LINES = 2


class ManualEntryValidationError(Exception):
    """Raised for any expected validation failure (spec I/O matrix) --
    `message` is a short, caller-facing summary matching the spec's exact
    wording, always translated by the view into a 400 response."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _to_decimal(value, field_name):
    if value is None:
        raise ManualEntryValidationError(
            f"Each line requires a numeric {field_name}"
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ManualEntryValidationError(
            f"Each line requires a numeric {field_name}"
        )


def post_manual_entry(
    finance_pmc_profile, lines, memo="", source_lease_transaction_id=None,
    source_status_transition="",
):
    """Validate and post a balanced manual `JournalEntry`.

    `lines` is a list of dicts, each with `account_id`, `debit`, `credit`
    (matching the spec's example payload shape). Raises
    `ManualEntryValidationError` on any validation failure -- no rows are
    ever created in that case (spec I/O matrix: "No rows created" on every
    error row). Returns the created `JournalEntry` on success.

    `source_lease_transaction_id`/`source_status_transition` (Story 5.2,
    FR-17): let a posting-engine caller (`post_other_charge`) reuse this
    same construct-and-validate path while still carrying a real
    idempotency key back to the triggering `LeaseTransaction` -- unlike an
    operator-entered manual entry (Story 5.1), which always leaves both at
    their default (`None`/`""`). `source_type` is always `MANUAL` either
    way (spec Always: "a balanced manual-entry-style Journal Entry ...
    source_type = MANUAL").
    """
    if not lines or len(lines) < MIN_LINES:
        raise ManualEntryValidationError("At least two lines are required")

    parsed_lines = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    account_ids = []

    for line in lines:
        if not isinstance(line, dict):
            raise ManualEntryValidationError(
                "Each line must be an object with account_id, debit, and credit"
            )

        account_id = line.get("account_id")
        if not isinstance(account_id, int) or isinstance(account_id, bool):
            raise ManualEntryValidationError(
                "Each line requires a valid account_id"
            )
        debit = _to_decimal(line.get("debit", 0), "debit")
        credit = _to_decimal(line.get("credit", 0), "credit")

        if debit < 0 or credit < 0:
            raise ManualEntryValidationError(
                "debit and credit must not be negative"
            )
        if debit > 0 and credit > 0:
            raise ManualEntryValidationError(
                "A line cannot have both debit and credit"
            )

        account_ids.append(account_id)
        total_debit += debit
        total_credit += credit
        parsed_lines.append(
            {"account_id": account_id, "debit": debit, "credit": credit}
        )

    if total_debit != total_credit:
        raise ManualEntryValidationError("Entry does not balance")

    if total_debit == 0:
        raise ManualEntryValidationError("Entry must have a non-zero amount")

    # Every account_id must belong to the requested PMC -- fetched in one
    # query, then matched by id so a missing/foreign account_id is caught
    # without a second per-line query (spec Always: no cross-PMC posting).
    accounts_by_id = {
        account.id: account
        for account in Account.objects.filter(
            finance_pmc_profile=finance_pmc_profile, id__in=set(account_ids)
        )
    }
    if len(accounts_by_id) != len(set(account_ids)):
        raise ManualEntryValidationError(
            "All accounts must belong to the requested PMC"
        )

    with transaction.atomic():
        entry = JournalEntry.objects.create(
            finance_pmc_profile=finance_pmc_profile,
            source_lease_transaction_id=source_lease_transaction_id,
            source_type=JournalEntry.MANUAL,
            source_status_transition=source_status_transition,
            memo=memo,
        )

        for line in parsed_lines:
            LedgerLine.objects.create(
                journal_entry=entry,
                account=accounts_by_id[line["account_id"]],
                debit=line["debit"],
                credit=line["credit"],
            )

        debit_total = sum(l.debit for l in entry.lines.all())
        credit_total = sum(l.credit for l in entry.lines.all())
        if debit_total != credit_total:
            # A real conditional, not `assert` -- matches posting.py's
            # convention (NFR-1: this invariant must hold even under
            # Python's `-O` flag). Raising here rolls back the whole atomic
            # block.
            raise ValueError(
                f"post_manual_entry: unbalanced entry constructed for "
                f"finance_pmc_profile_id={finance_pmc_profile.id} "
                f"(debit={debit_total}, credit={credit_total})"
            )

    return entry

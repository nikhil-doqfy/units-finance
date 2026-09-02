"""
Story 4.1: bank statement CSV import (FR-14).

`import_bank_statement_csv` parses an uploaded CSV (`date`, `amount`,
`reference` columns) and creates one `BankStatementLine` per data row, all
scoped to the given `FinancePMCProfile` via a real Django FK (spec Boundaries
& Constraints).

Column matching is case-insensitive header lookup, matching
`lead_bulk_import`'s established convention of lowercasing/stripping header
keys (`microservices/units-backend/lead/views.py`) -- read-only reference,
not reused directly (Finance never imports units-backend code, AD-1).

Whole-file rejection (spec Design Notes): unlike `lead_bulk_import`'s
partial-skip-with-errors-array style, a single unparsable row rejects the
entire request and creates nothing -- bank statement rows have no natural
"skip and continue" semantics for reconciliation purposes (a partially
imported statement is worse than a rejected one, since Story 4.2's matching
would run against an incomplete picture with no signal that rows are
missing). This is why every row is parsed into an in-memory list of
`BankStatementLine(...)` instances first, and `bulk_create` only runs once
every row across the whole file has parsed cleanly.
"""
import csv
import datetime
import decimal
import io

from django.db import transaction

REQUIRED_COLUMNS = ("date", "amount", "reference")
MAX_REFERENCE_LENGTH = 255
MAX_AMOUNT = decimal.Decimal("999999999999.99")  # max_digits=14, decimal_places=2
AMOUNT_PATTERN = decimal.Decimal("0.01")


class BankStatementImportError(Exception):
    """Raised for any whole-file rejection reason (missing column, or an
    unparsable row) -- the view translates this into a 400 response naming
    the specific problem (spec I/O matrix). No `BankStatementLine` rows are
    created when this is raised (spec Always / Design Notes)."""


class BankStatementMatchError(Exception):
    """Raised by `apply_match_decision` for any 404/409-worthy problem (spec
    Code Map) -- the view translates `.status` into the matching HTTP status
    and `str(exc)` into the response message."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


MATCH_DATE_WINDOW_DAYS = 7


def _normalize_row(row):
    """Lowercase/strip header keys, matching lead_bulk_import's established
    case-insensitive header convention."""
    return {(k or "").strip().lower(): v for k, v in row.items()}


def import_bank_statement_csv(finance_pmc_profile, csv_file):
    """Parse `csv_file` (a Django `UploadedFile`) and create one
    `BankStatementLine` per data row, scoped to `finance_pmc_profile`.

    Returns the number of rows created on success. Raises
    `BankStatementImportError` -- creating nothing -- if a required column is
    missing, or if any row's `date`/`amount` is unparsable (whole-file
    rejection, spec Always/Never).
    """
    # Imported here (not at module load) to avoid a hard import-time
    # dependency cycle between ledger.models and ledger.reconciliation.
    from ledger.models import BankStatementLine

    raw_bytes = csv_file.read()
    try:
        csv_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BankStatementImportError("Invalid file encoding")

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames or []
    normalized_names = [(h or "").strip().lower() for h in fieldnames]
    normalized_headers = set(normalized_names)

    if len(normalized_names) != len(normalized_headers):
        raise BankStatementImportError("CSV has duplicate column headers")

    missing = [c for c in REQUIRED_COLUMNS if c not in normalized_headers]
    if missing:
        raise BankStatementImportError(
            f"CSV missing required column(s): {', '.join(missing)}"
        )

    lines_to_create = []
    for i, raw_row in enumerate(reader, start=1):
        row = _normalize_row(raw_row)

        date_str = (row.get("date") or "").strip()
        amount_str = (row.get("amount") or "").strip()
        reference = (row.get("reference") or "").strip()

        try:
            statement_date = datetime.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            raise BankStatementImportError(
                f"Row {i}: unparsable date '{date_str}'"
            )

        try:
            amount = decimal.Decimal(amount_str)
        except (decimal.InvalidOperation, TypeError, ValueError):
            raise BankStatementImportError(
                f"Row {i}: unparsable amount '{amount_str}'"
            )

        # NaN/Infinity are valid Decimal literals but not valid amounts --
        # reject before the exponent check below (a non-finite Decimal's
        # exponent is a non-integer sentinel like 'n'/'N'/'F', not
        # comparable to an int).
        if not amount.is_finite():
            raise BankStatementImportError(
                f"Row {i}: unparsable amount '{amount_str}'"
            )

        # Reject rather than silently round: a value with more than 2
        # decimal places (e.g. a sub-cent amount) is an unparsable amount
        # for this story's whole-file-rejection contract, not a value to
        # quietly quantize (spec Always/Never -- confirmed by review: the
        # default Decimal.quantize would otherwise round '10.005' to
        # '10.00' with no signal).
        if amount.as_tuple().exponent < -2:
            raise BankStatementImportError(
                f"Row {i}: amount '{amount_str}' has more than 2 decimal places"
            )
        if abs(amount) > MAX_AMOUNT:
            raise BankStatementImportError(
                f"Row {i}: amount '{amount_str}' is out of range"
            )
        amount = amount.quantize(AMOUNT_PATTERN)

        if len(reference) > MAX_REFERENCE_LENGTH:
            raise BankStatementImportError(
                f"Row {i}: reference exceeds {MAX_REFERENCE_LENGTH} characters"
            )

        lines_to_create.append(
            BankStatementLine(
                finance_pmc_profile=finance_pmc_profile,
                statement_date=statement_date,
                amount=amount,
                reference=reference,
            )
        )

    if lines_to_create:
        with transaction.atomic():
            BankStatementLine.objects.bulk_create(lines_to_create)

    return len(lines_to_create)


def _unreconciled_bank_journal_entries(finance_pmc_profile):
    """Bank Journal `JournalEntry` rows for `finance_pmc_profile` that are not
    yet reconciled: a `JournalEntry` whose `lines` include a `LedgerLine` with
    `debit > 0` on the Account named `BANK_ACCOUNT_NAME` (spec Boundaries &
    Constraints, matching `post_cheque_clearing`'s posting shape), excluding
    any entry that already has a `status="confirmed"` `BankStatementMatch`
    row (spec Always: "Unreconciled" definition).

    Imported here (not at module load), same import-cycle-avoidance pattern
    as `import_bank_statement_csv` above.
    """
    from ledger.models import BankStatementMatch, JournalEntry
    from ledger.posting import BANK_ACCOUNT_NAME

    confirmed_journal_entry_ids = BankStatementMatch.objects.filter(
        status=BankStatementMatch.CONFIRMED,
    ).values_list("journal_entry_id", flat=True)

    return (
        JournalEntry.objects.filter(
            finance_pmc_profile=finance_pmc_profile,
            lines__account__name=BANK_ACCOUNT_NAME,
            lines__debit__gt=0,
        )
        .exclude(id__in=confirmed_journal_entry_ids)
        .distinct()
    )


def find_suggested_matches(finance_pmc_profile):
    """Story 4.2: the suggestion heuristic (spec Boundaries & Constraints,
    FR-15 NFR).

    For each unreconciled `BankStatementLine` (`reconciled=False`) of
    `finance_pmc_profile`, finds unreconciled Bank Journal `JournalEntry`
    rows for the same profile where the Bank-side `LedgerLine.debit` equals
    the statement line's `amount` exactly, and
    `abs(statement_date - posted_at.date()) <= MATCH_DATE_WINDOW_DAYS` days.
    No fuzzy/partial-amount matching (spec Never).

    Returns a list of `{"bank_statement_line": BankStatementLine,
    "journal_entry": JournalEntry}` dicts, one per matchable pair -- the view
    is responsible for serializing these into the response shape.
    """
    from ledger.models import BankStatementLine
    from ledger.posting import BANK_ACCOUNT_NAME

    unreconciled_lines = BankStatementLine.objects.filter(
        finance_pmc_profile=finance_pmc_profile, reconciled=False
    )
    candidate_entries = list(
        _unreconciled_bank_journal_entries(finance_pmc_profile).prefetch_related(
            "lines__account"
        )
    )

    suggestions = []
    for line in unreconciled_lines:
        for entry in candidate_entries:
            bank_debit = sum(
                l.debit
                for l in entry.lines.all()
                if l.account.name == BANK_ACCOUNT_NAME and l.debit > 0
            )
            if bank_debit != line.amount:
                continue
            if abs((line.statement_date - entry.posted_at.date()).days) > MATCH_DATE_WINDOW_DAYS:
                continue
            suggestions.append(
                {"bank_statement_line": line, "journal_entry": entry}
            )

    return suggestions


def apply_match_decision(
    finance_pmc_profile, bank_statement_line_id, journal_entry_id, action
):
    """Story 4.2: apply a `confirm`/`reject` decision for one
    `(bank_statement_line_id, journal_entry_id)` pair (spec Boundaries &
    Constraints).

    Raises `BankStatementMatchError` (with `.status` 404/409) for any
    error the view should surface as that HTTP status. Returns a result dict
    describing the outcome on success.
    """
    from ledger.models import BankStatementLine, BankStatementMatch, JournalEntry

    bank_statement_line = BankStatementLine.objects.filter(
        pk=bank_statement_line_id, finance_pmc_profile=finance_pmc_profile
    ).first()
    if bank_statement_line is None:
        raise BankStatementMatchError(
            "No BankStatementLine exists for the given bank_statement_line_id "
            "and pmc_id",
            status=404,
        )

    journal_entry = JournalEntry.objects.filter(
        pk=journal_entry_id, finance_pmc_profile=finance_pmc_profile
    ).first()
    if journal_entry is None:
        raise BankStatementMatchError(
            "No JournalEntry exists for the given journal_entry_id and pmc_id",
            status=404,
        )

    if action == BankStatementMatch.CONFIRMED:
        return _confirm_match(bank_statement_line, journal_entry)
    elif action == BankStatementMatch.REJECTED:
        return _reject_match(bank_statement_line, journal_entry)

    # The view validates `action` in {"confirm", "reject"} before calling
    # this function (spec Always: 400 on invalid action) -- this branch is
    # unreachable in practice, kept only as a defensive guard.
    raise BankStatementMatchError(f"Unsupported action '{action}'", status=400)


def _confirm_match(bank_statement_line, journal_entry):
    from ledger.models import BankStatementMatch

    existing_pair_match = BankStatementMatch.objects.filter(
        bank_statement_line=bank_statement_line, journal_entry=journal_entry
    ).first()

    # Idempotent re-confirm of the exact same already-confirmed pair (spec
    # Always/I-O matrix): 200, no state change.
    if existing_pair_match is not None and existing_pair_match.status == BankStatementMatch.CONFIRMED:
        return {"status": "confirmed", "created": False}

    other_confirmed_for_line = (
        BankStatementMatch.objects.filter(
            bank_statement_line=bank_statement_line,
            status=BankStatementMatch.CONFIRMED,
        )
        .exclude(journal_entry=journal_entry)
        .exists()
    )
    if other_confirmed_for_line:
        raise BankStatementMatchError(
            "bank_statement_line already has a different confirmed match",
            status=409,
        )

    other_confirmed_for_entry = (
        BankStatementMatch.objects.filter(
            journal_entry=journal_entry,
            status=BankStatementMatch.CONFIRMED,
        )
        .exclude(bank_statement_line=bank_statement_line)
        .exists()
    )
    if other_confirmed_for_entry:
        raise BankStatementMatchError(
            "journal_entry already has a confirmed match to a different "
            "bank_statement_line",
            status=409,
        )

    with transaction.atomic():
        if existing_pair_match is not None:
            existing_pair_match.status = BankStatementMatch.CONFIRMED
            existing_pair_match.save(update_fields=["status", "modified"])
        else:
            BankStatementMatch.objects.create(
                bank_statement_line=bank_statement_line,
                journal_entry=journal_entry,
                status=BankStatementMatch.CONFIRMED,
            )
        bank_statement_line.reconciled = True
        bank_statement_line.save(update_fields=["reconciled", "modified"])

    return {"status": "confirmed", "created": True}


def _reject_match(bank_statement_line, journal_entry):
    from ledger.models import BankStatementMatch

    existing_pair_match = BankStatementMatch.objects.filter(
        bank_statement_line=bank_statement_line, journal_entry=journal_entry
    ).first()

    # Post-review patch: rejecting an already-confirmed pair would otherwise
    # silently flip BankStatementMatch back to "rejected" while leaving
    # BankStatementLine.reconciled == True with no confirmed match backing
    # it -- an inconsistent state this story's "confirm/reject" model must
    # never produce. Confirmed pairs can only be superseded by a *different*
    # confirm (spec Never: no un-confirming a previously confirmed match).
    if existing_pair_match is not None and existing_pair_match.status == BankStatementMatch.CONFIRMED:
        raise BankStatementMatchError(
            "This pair is already confirmed; rejecting a confirmed match is "
            "not supported",
            status=409,
        )

    with transaction.atomic():
        if existing_pair_match is not None:
            existing_pair_match.status = BankStatementMatch.REJECTED
            existing_pair_match.save(update_fields=["status", "modified"])
        else:
            BankStatementMatch.objects.create(
                bank_statement_line=bank_statement_line,
                journal_entry=journal_entry,
                status=BankStatementMatch.REJECTED,
            )

    # Neither side's reconciled flag changes (spec Always).
    return {"status": "rejected", "created": existing_pair_match is None}

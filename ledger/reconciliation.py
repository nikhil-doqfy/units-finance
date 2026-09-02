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

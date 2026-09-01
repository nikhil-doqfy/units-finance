"""
Story 1.1 scaffold: intentionally empty.

`ledger` is registered in INSTALLED_APPS with no models yet, so
`python manage.py migrate` runs cleanly with zero conflicting table names
against the shared database (NFR-4). FinancePMCProfile, Account,
JournalEntry, and LedgerLine models arrive in Stories 1.2/1.3+.
"""
from django.db import models  # noqa: F401

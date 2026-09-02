"""finance_service URL Configuration."""
from django.contrib import admin
from django.urls import path

from ledger.views import (
    balance_sheet_report,
    profit_loss_report,
    sync_lease,
    sync_lease_transaction,
    trial_balance_report,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    # Spec's Code Map/I-O matrix both direct the trailing-slash form. Also
    # matched without the trailing slash: units-backend's actual sender
    # (lease/finance_sync.py) posts to ".../sync" with no trailing slash, and
    # a bare APPEND_SLASH redirect would silently turn that POST into a GET
    # on most HTTP clients, breaking the real channel this story exists to
    # receive.
    path(
        'internal/lease-transactions/<int:lease_transaction_id>/sync/',
        sync_lease_transaction,
        name='sync-lease-transaction',
    ),
    path(
        'internal/lease-transactions/<int:lease_transaction_id>/sync',
        sync_lease_transaction,
        name='sync-lease-transaction-no-slash',
    ),
    # Story 2.6: mirrors the lease-transactions naming precedent above --
    # same trailing-slash + no-trailing-slash pair, since units-backend's
    # real sender (lease/finance_sync.py's sync_lease_to_finance) also posts
    # without a trailing slash.
    path(
        'internal/leases/<int:lease_id>/sync/',
        sync_lease,
        name='sync-lease',
    ),
    path(
        'internal/leases/<int:lease_id>/sync',
        sync_lease,
        name='sync-lease-no-slash',
    ),
    # Story 3.2: Trial Balance report -- trailing-slash + no-slash pair,
    # matching the existing /internal/... route-pair precedent above (spec
    # Boundaries & Constraints).
    path(
        'reports/trial-balance/',
        trial_balance_report,
        name='trial-balance-report',
    ),
    path(
        'reports/trial-balance',
        trial_balance_report,
        name='trial-balance-report-no-slash',
    ),
    # Story 3.3: Profit & Loss report -- trailing-slash + no-slash pair,
    # matching the established route-pair precedent above (spec Boundaries
    # & Constraints).
    path(
        'reports/profit-loss/',
        profit_loss_report,
        name='profit-loss-report',
    ),
    path(
        'reports/profit-loss',
        profit_loss_report,
        name='profit-loss-report-no-slash',
    ),
    # Story 3.4: Balance Sheet report -- trailing-slash + no-slash pair,
    # matching the established route-pair precedent above (spec Boundaries
    # & Constraints).
    path(
        'reports/balance-sheet/',
        balance_sheet_report,
        name='balance-sheet-report',
    ),
    path(
        'reports/balance-sheet',
        balance_sheet_report,
        name='balance-sheet-report-no-slash',
    ),
]

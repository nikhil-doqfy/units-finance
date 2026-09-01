"""finance_service URL Configuration."""
from django.contrib import admin
from django.urls import path

from ledger.views import sync_lease, sync_lease_transaction

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
]

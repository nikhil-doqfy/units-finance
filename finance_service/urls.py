"""finance_service URL Configuration."""
from django.contrib import admin
from django.urls import path

from ledger.views import (
    account_ledger_lines,
    ageing_report,
    apply_bank_statement_match,
    balance_sheet_report,
    bank_statement_import,
    chart_of_accounts,
    finance_pmc_profile_status,
    profit_loss_report,
    suggested_matches,
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
    # Story 3.5: Ageing report -- trailing-slash + no-slash pair, matching
    # the established route-pair precedent above (spec Boundaries &
    # Constraints).
    path(
        'reports/ageing/',
        ageing_report,
        name='ageing-report',
    ),
    path(
        'reports/ageing',
        ageing_report,
        name='ageing-report-no-slash',
    ),
    # Story 4.1: Bank statement import -- trailing-slash + no-slash pair,
    # matching the established route-pair precedent above (spec Code Map).
    path(
        'reconciliation/bank-statement-import/',
        bank_statement_import,
        name='bank-statement-import',
    ),
    path(
        'reconciliation/bank-statement-import',
        bank_statement_import,
        name='bank-statement-import-no-slash',
    ),
    # Story 4.2: Match statement lines to Ledger entries -- trailing-slash +
    # no-slash pair, matching the established route-pair convention above
    # (spec Code Map).
    path(
        'reconciliation/suggested-matches/',
        suggested_matches,
        name='suggested-matches',
    ),
    path(
        'reconciliation/suggested-matches',
        suggested_matches,
        name='suggested-matches-no-slash',
    ),
    path(
        'reconciliation/match/',
        apply_bank_statement_match,
        name='apply-bank-statement-match',
    ),
    path(
        'reconciliation/match',
        apply_bank_statement_match,
        name='apply-bank-statement-match-no-slash',
    ),
    # AD-6 / frontend-prd.md FFR-13: Chart of Accounts -- trailing-slash +
    # no-slash pair, matching the established route-pair convention above
    # (spec Code Map).
    path(
        'accounts/',
        chart_of_accounts,
        name='chart-of-accounts',
    ),
    path(
        'accounts',
        chart_of_accounts,
        name='chart-of-accounts-no-slash',
    ),
    # AD-6 / frontend-prd.md FFR-14/FFR-15: per-Account Ledger drill-down --
    # trailing-slash + no-slash pair, matching the established route-pair
    # convention above (spec Code Map).
    path(
        'accounts/<int:account_id>/ledger-lines/',
        account_ledger_lines,
        name='account-ledger-lines',
    ),
    path(
        'accounts/<int:account_id>/ledger-lines',
        account_ledger_lines,
        name='account-ledger-lines-no-slash',
    ),
    # AD-6 / frontend-prd.md FFR-3: Finance-PMC-activation status signal --
    # trailing-slash + no-slash pair, matching the established route-pair
    # convention above (spec Code Map).
    path(
        'finance-pmc-profile/<int:pmc_id>/status/',
        finance_pmc_profile_status,
        name='finance-pmc-profile-status',
    ),
    path(
        'finance-pmc-profile/<int:pmc_id>/status',
        finance_pmc_profile_status,
        name='finance-pmc-profile-status-no-slash',
    ),
]

from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'ledger'

    def ready(self):
        # Import inside ready(), not at module scope, so this module never
        # triggers Django's app registry / model-loading before apps are
        # ready (spec Verification: manual check on this exact pitfall).
        # The import's only purpose is its side effect: registering
        # ledger/signals.py's post_save receiver on FinancePMCProfile.
        import ledger.signals  # noqa: F401

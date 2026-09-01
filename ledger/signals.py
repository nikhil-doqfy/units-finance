"""
Story 1.3: post_save signal that seeds the standard Chart of Accounts.

The first `signals.py` in Finance's own codebase, mirroring the
signal-driven-posting pattern the architecture spine plans for
units-backend's `LeaseTransaction` (AD-4) — here applied to
`FinancePMCProfile` creation instead. Registered in `ledger/apps.py`'s
`ready()` so it's live from Django startup, with no second manual step for
the operator running `create_finance_pmc_profile` (Story 1.2).
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from ledger.models import FinancePMCProfile
from ledger.seed import seed_standard_chart_of_accounts


@receiver(post_save, sender=FinancePMCProfile)
def seed_chart_of_accounts_on_profile_creation(sender, instance, created, **kwargs):
    """Seed the standard CoA the moment a FinancePMCProfile is first created.

    Guarded on `created=True` only — a re-save of an already-seeded profile
    (e.g. `profile.save()` after a field update) does not re-trigger this
    handler at all, so no additional Account-level dedupe check is needed
    (spec Boundaries & Constraints: Ask First, resolved as `created=True`
    guard only).
    """
    if not created:
        return
    seed_standard_chart_of_accounts(instance)

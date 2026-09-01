from django.core.management.base import BaseCommand, CommandError

from ledger.models import FinancePMCProfile, PropertyManagmentCompanyRef

# Phase 1 hard constraint (FR-1): every FinancePMCProfile must be UAE-registered.
# Non-UAE country values are rejected — no non-UAE support logic is built (spine Deferred).
UAE_COUNTRY = "UAE"


class Command(BaseCommand):
    help = (
        "Activate an existing PropertyManagmentCompany for Finance by creating "
        "its FinancePMCProfile (accounting settings: base currency, country, "
        "fiscal year start month)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--pmc-id",
            type=int,
            required=True,
            help="id of the existing PropertyManagmentCompany to activate for Finance.",
        )
        parser.add_argument(
            "--base-currency",
            default="AED",
            help="Base currency for this PMC's Ledger (default: AED).",
        )
        parser.add_argument(
            "--country",
            required=True,
            help="Country of registration. Phase 1 only accepts UAE.",
        )
        parser.add_argument(
            "--fiscal-year-start-month",
            type=int,
            required=True,
            help="Fiscal year start month (1-12).",
        )

    def handle(self, *args, **options):
        pmc_id = options["pmc_id"]
        base_currency = options["base_currency"]
        country = options["country"]
        fiscal_year_start_month = options["fiscal_year_start_month"]

        if country != UAE_COUNTRY:
            raise CommandError(
                f"Rejected: country '{country}' is not supported in Phase 1 "
                f"(only '{UAE_COUNTRY}' is accepted)."
            )

        if not PropertyManagmentCompanyRef.objects.filter(pk=pmc_id).exists():
            raise CommandError(
                f"Rejected: no PropertyManagmentCompany found with id {pmc_id}."
            )

        if not 1 <= fiscal_year_start_month <= 12:
            raise CommandError(
                f"Rejected: fiscal_year_start_month must be between 1 and 12, "
                f"got {fiscal_year_start_month}."
            )

        profile = FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency=base_currency,
            country=country,
            fiscal_year_start_month=fiscal_year_start_month,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created FinancePMCProfile(id={profile.id}, pmc_id={profile.pmc_id}, "
                f"base_currency={profile.base_currency}, country={profile.country}, "
                f"fiscal_year_start_month={profile.fiscal_year_start_month})"
            )
        )

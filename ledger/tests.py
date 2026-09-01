"""
Story 1.2 tests: FinancePMCProfile creation via create_finance_pmc_profile.

Covers all four I/O matrix rows from the spec:
  1. Happy path — valid pmc_id + UAE country creates a FinancePMCProfile.
  2. Non-UAE country is rejected — no profile created.
  3. Nonexistent pmc_id is rejected — no profile created.
  4. Two independent pmc_ids each get their own, independently coexisting
     FinancePMCProfile row.

`PropertyManagmentCompanyRef` is `managed = False` (it points at
units-backend's real `property_propertymanagmentcompany` table), so Django's
test runner will not create/drop that table for us. Since these tests run
against a database where units-backend's migrations may not have been
applied, we create/drop the table ourselves around this test case so the
command's existence check has something real to query, without ever
importing units-backend's code (consistent with the spec's Design Notes).
"""
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from ledger.models import Account, FinancePMCProfile
from ledger.seed import STANDARD_CHART_OF_ACCOUNTS


class CreateFinancePMCProfileCommandTests(TestCase):
    """Exercises the create_finance_pmc_profile management command."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Stand in for units-backend's real property_propertymanagmentcompany
        # table, which PropertyManagmentCompanyRef (managed=False) points at.
        # Minimal shape only (id + name) — matches the unmanaged model's
        # own minimal field list, not units-backend's full schema.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_propertymanagmentcompany (
                    id BIGSERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS property_propertymanagmentcompany")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM property_propertymanagmentcompany")
            cursor.execute(
                "INSERT INTO property_propertymanagmentcompany (id, name) VALUES (%s, %s)",
                [1, "Acme PMC"],
            )
            cursor.execute(
                "INSERT INTO property_propertymanagmentcompany (id, name) VALUES (%s, %s)",
                [2, "Beta PMC"],
            )

    def test_happy_path_creates_profile(self):
        call_command(
            "create_finance_pmc_profile",
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

        profile = FinancePMCProfile.objects.get(pmc_id=1)
        self.assertEqual(profile.base_currency, "AED")
        self.assertEqual(profile.country, "UAE")
        self.assertEqual(profile.fiscal_year_start_month, 1)
        self.assertIsNotNone(profile.pmc_id)

    def test_non_uae_country_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command(
                "create_finance_pmc_profile",
                pmc_id=1,
                base_currency="AED",
                country="USA",
                fiscal_year_start_month=1,
            )

        self.assertFalse(FinancePMCProfile.objects.filter(pmc_id=1).exists())

    def test_nonexistent_pmc_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command(
                "create_finance_pmc_profile",
                pmc_id=999999,
                base_currency="AED",
                country="UAE",
                fiscal_year_start_month=1,
            )

        self.assertFalse(FinancePMCProfile.objects.filter(pmc_id=999999).exists())

    def test_fiscal_year_start_month_out_of_range_is_rejected(self):
        with self.assertRaises(CommandError):
            call_command(
                "create_finance_pmc_profile",
                pmc_id=1,
                base_currency="AED",
                country="UAE",
                fiscal_year_start_month=13,
            )
        self.assertFalse(FinancePMCProfile.objects.filter(pmc_id=1).exists())

        with self.assertRaises(CommandError):
            call_command(
                "create_finance_pmc_profile",
                pmc_id=1,
                base_currency="AED",
                country="UAE",
                fiscal_year_start_month=0,
            )
        self.assertFalse(FinancePMCProfile.objects.filter(pmc_id=1).exists())

    def test_two_independent_pmc_profiles_coexist(self):
        call_command(
            "create_finance_pmc_profile",
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        call_command(
            "create_finance_pmc_profile",
            pmc_id=2,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=4,
        )

        profile_1 = FinancePMCProfile.objects.get(pmc_id=1)
        profile_2 = FinancePMCProfile.objects.get(pmc_id=2)

        self.assertNotEqual(profile_1.id, profile_2.id)
        self.assertEqual(profile_1.fiscal_year_start_month, 1)
        self.assertEqual(profile_2.fiscal_year_start_month, 4)
        self.assertEqual(FinancePMCProfile.objects.count(), 2)


class SeedStandardChartOfAccountsTests(TestCase):
    """Story 1.3 tests: the post_save-triggered standard CoA seed.

    Covers all three I/O matrix rows from the spec:
      1. PMC activation seeds exactly the 7 standard Accounts (8 rows, see
         Design Notes) with the correct account_type each.
      2. Two separate FinancePMCProfiles each get their own independent set
         — no Account row's FK points to the wrong profile.
      3. Re-saving an already-seeded profile does not create duplicates —
         guarded by the post_save signal's `created=True` check.
    """

    def test_profile_creation_seeds_standard_chart_of_accounts(self):
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

        accounts = Account.objects.filter(finance_pmc_profile=profile)
        self.assertEqual(accounts.count(), len(STANDARD_CHART_OF_ACCOUNTS))

        actual_pairs = {(a.name, a.account_type) for a in accounts}
        expected_pairs = set(STANDARD_CHART_OF_ACCOUNTS)
        self.assertEqual(actual_pairs, expected_pairs)

        # Independent of STANDARD_CHART_OF_ACCOUNTS — asserts the literal FR-3
        # account list so a typo/misclassification introduced in seed.py
        # itself would fail this test, not just self-agree with it.
        self.assertEqual(
            actual_pairs,
            {
                ("Rent Income", Account.INCOME),
                ("Security Deposits Held", Account.LIABILITY),
                ("VAT Payable", Account.LIABILITY),
                ("Bank", Account.ASSET),
                ("AR — Tenants", Account.ASSET),
                ("AP — PMC Commission", Account.LIABILITY),
                ("Commission Expense", Account.EXPENSE),
                ("Bank Charges/Fees", Account.EXPENSE),
            },
        )

    def test_seed_helper_called_twice_directly_creates_duplicates(self):
        """Documents the seed helper's actual (non-)idempotency contract.

        `seed_standard_chart_of_accounts` has no internal dedupe guard — only
        the `post_save` signal's `created=True` check prevents re-seeding.
        Calling the helper directly a second time (bypassing the signal, as a
        future backfill/repair tool might) creates a second full set. This is
        current, intentional behavior for Phase 1, not a bug being silently
        introduced — documented here so a future direct caller can see the
        contract rather than assume idempotency.
        """
        from ledger.seed import seed_standard_chart_of_accounts

        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        self.assertEqual(
            Account.objects.filter(finance_pmc_profile=profile).count(),
            len(STANDARD_CHART_OF_ACCOUNTS),
        )

        seed_standard_chart_of_accounts(profile)

        self.assertEqual(
            Account.objects.filter(finance_pmc_profile=profile).count(),
            2 * len(STANDARD_CHART_OF_ACCOUNTS),
        )

    def test_two_profiles_get_independent_account_sets(self):
        profile_1 = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        profile_2 = FinancePMCProfile.objects.create(
            pmc_id=2,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=4,
        )

        accounts_1 = Account.objects.filter(finance_pmc_profile=profile_1)
        accounts_2 = Account.objects.filter(finance_pmc_profile=profile_2)

        self.assertEqual(accounts_1.count(), len(STANDARD_CHART_OF_ACCOUNTS))
        self.assertEqual(accounts_2.count(), len(STANDARD_CHART_OF_ACCOUNTS))
        self.assertEqual(Account.objects.count(), 2 * len(STANDARD_CHART_OF_ACCOUNTS))

        # No Account row's FK points to the wrong profile.
        self.assertFalse(
            Account.objects.filter(finance_pmc_profile=profile_1).filter(
                id__in=accounts_2.values_list("id", flat=True)
            ).exists()
        )

    def test_resaving_existing_profile_does_not_duplicate_accounts(self):
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        self.assertEqual(
            Account.objects.filter(finance_pmc_profile=profile).count(),
            len(STANDARD_CHART_OF_ACCOUNTS),
        )

        profile.fiscal_year_start_month = 2
        profile.save()

        self.assertEqual(
            Account.objects.filter(finance_pmc_profile=profile).count(),
            len(STANDARD_CHART_OF_ACCOUNTS),
        )

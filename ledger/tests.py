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

from ledger.models import FinancePMCProfile


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

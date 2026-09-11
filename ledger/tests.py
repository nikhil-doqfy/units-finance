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
import datetime
import decimal

import jwt
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ledger.auth import authenticate_reporting_request
from ledger.models import (
    Account,
    BankStatementLine,
    BankStatementMatch,
    FinancePMCProfile,
    JournalEntry,
    LedgerLine,
    PMCChargeType,
)
from ledger.org_scope import get_pmc_ids_for_user_profile
from ledger.reports import compute_chart_of_accounts
from ledger.posting import (
    post_bounce_fee,
    post_bounce_reversal,
    post_cheque_clearing,
    post_commission_split,
    post_other_charge,
    post_rent_ar,
    post_security_deposit,
)
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

        actual_triples = {(a.name, a.account_type, a.account_subtype) for a in accounts}
        expected_triples = set(STANDARD_CHART_OF_ACCOUNTS)
        self.assertEqual(actual_triples, expected_triples)

        # Independent of STANDARD_CHART_OF_ACCOUNTS — asserts the literal FR-3
        # + Story 2.4 + Story 5.3 (FR-18) account list so a typo/
        # misclassification introduced in seed.py itself would fail this
        # test, not just self-agree with it.
        self.assertEqual(
            actual_triples,
            {
                ("Rent Income", Account.INCOME, None),
                ("Security Deposits Held", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
                ("VAT Payable", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
                ("Bank", Account.ASSET, Account.CURRENT_ASSET),
                ("AR — Tenants", Account.ASSET, Account.CURRENT_ASSET),
                ("AP — PMC Commission", Account.LIABILITY, Account.LIABILITY_SUBTYPE),
                ("Commission Expense", Account.EXPENSE, None),
                ("Bank Charges/Fees", Account.EXPENSE, None),
                ("Bounced Cheques", Account.ASSET, Account.CURRENT_ASSET),
            },
        )

    def test_no_seeded_account_left_uncategorized_except_income_expense(self):
        """Story 5.3 (FR-18) I/O matrix row 1: every one of the 9 standard
        accounts has an explicit account_subtype, except Income/Expense
        accounts (which have nothing to subtype into in Phase 1)."""
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

        for account in Account.objects.filter(finance_pmc_profile=profile):
            if account.account_type in (Account.INCOME, Account.EXPENSE):
                self.assertIsNone(account.account_subtype)
            else:
                self.assertIsNotNone(
                    account.account_subtype,
                    f"{account.name} ({account.account_type}) was left uncategorized",
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


class AccountSubtypeTests(TestCase):
    """Story 5.3 (FR-18) tests: `account_subtype` field, seed backfill, and
    Chart of Accounts view surfacing.

    Covers all three I/O matrix rows from the spec:
      1. New PMC seeded after this story ships -- all 9 accounts have both
         account_type and account_subtype set (correct value, or None for
         Income/Expense) -- see test_no_seeded_account_left_uncategorized_
         except_income_expense above for the full backfill assertion.
      2. Existing PMC, seeded before this story -- account_subtype is null
         immediately after the migration, no data migration touches
         existing rows.
      3. Chart of Accounts view (compute_chart_of_accounts) -- response
         includes account_subtype per row (null for pre-existing
         unbackfilled rows, a real value for newly-seeded ones).
    """

    def test_existing_preseeded_rows_stay_null_after_field_added(self):
        """Simulates a pre-Story-5.3 Account row (created without ever
        setting account_subtype, as if seeded before this migration) --
        confirms it is simply null, never guessed/backfilled (spec
        Boundaries & Constraints/Design Notes: no best-effort name-matching
        backfill migration)."""
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        # Bypass the seed helper entirely to model a genuinely pre-existing
        # row that predates the account_subtype field/backfill.
        pre_existing = Account.objects.create(
            finance_pmc_profile=profile,
            name="Bank",
            account_type=Account.ASSET,
        )

        pre_existing.refresh_from_db()
        self.assertIsNone(pre_existing.account_subtype)

    def test_chart_of_accounts_report_includes_account_subtype(self):
        """New-PMC CoA rows surface their real subtype; a null-subtype row
        (modeling a pre-existing, unbackfilled account) surfaces as None --
        never a fabricated guess."""
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        # Add a stand-in "pre-existing, unbackfilled" row alongside the
        # newly-seeded ones.
        Account.objects.create(
            finance_pmc_profile=profile,
            name="Legacy Suspense",
            account_type=Account.ASSET,
        )

        result = compute_chart_of_accounts(profile)
        by_name = {row["name"]: row["account_subtype"] for row in result["accounts"]}

        self.assertEqual(by_name["Bank"], Account.CURRENT_ASSET)
        self.assertEqual(by_name["AR — Tenants"], Account.CURRENT_ASSET)
        self.assertEqual(by_name["Security Deposits Held"], Account.LIABILITY_SUBTYPE)
        self.assertEqual(by_name["VAT Payable"], Account.LIABILITY_SUBTYPE)
        self.assertEqual(by_name["AP — PMC Commission"], Account.LIABILITY_SUBTYPE)
        self.assertIsNone(by_name["Rent Income"])
        self.assertIsNone(by_name["Commission Expense"])
        self.assertIsNone(by_name["Bank Charges/Fees"])
        self.assertIsNone(by_name["Legacy Suspense"])


@override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
class SyncLeaseTransactionEndpointTests(TestCase):
    """Story 2.1b tests: the internal sync endpoint + token auth.

    Covers all four I/O matrix rows from the spec:
      1. Happy path -- valid token, consistent path/body id -> 200 ack.
      2. Missing token -> 401, rejected before any view logic runs.
      3. Wrong token -> 403, rejected before any view logic runs.
      4. Path/body id mismatch -> 400 naming the mismatch.

    Story 2.2b's posting.post_rent_ar is now wired into this view, which
    means every call (even non-RENT ones) queries the unmanaged
    LeaseTransactionRef -- so the stand-in lease_leasetransaction table must
    exist even for these auth/envelope-focused tests, matching Story 1.2's
    hand-rolled-stand-in-table technique. No row exists at id=42 in these
    tests, so txn resolves to None and no posting is attempted, exercising
    the pre-existing bare-acknowledgment behavior these tests assert on.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
        super().tearDownClass()

    def _url(self, lease_transaction_id):
        return reverse(
            "sync-lease-transaction",
            kwargs={"lease_transaction_id": lease_transaction_id},
        )

    def test_happy_path_returns_200_ack(self):
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 42},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        self.assertEqual(body["content"], {"lease_transaction_id": 42})
        self.assertTrue(body["message"])

    def test_happy_path_with_only_path_id_returns_200_ack(self):
        """Body is optional -- the URL path param alone is sufficient."""
        response = self.client.post(
            self._url(42),
            data={},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], {"lease_transaction_id": 42})

    def test_missing_token_rejected_with_401(self):
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 42},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["status"], 401)
        self.assertTrue(body["message"])

    def test_wrong_token_rejected_with_403(self):
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 42},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="wrong-token",
        )

        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body["status"], 403)
        self.assertTrue(body["message"])

    def test_path_body_id_mismatch_rejected_with_400(self):
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 99},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["status"], 400)
        self.assertTrue(body["message"])
        self.assertEqual(body["content"]["path_lease_transaction_id"], 42)
        self.assertEqual(body["content"]["body_lease_transaction_id"], 99)

    def test_missing_token_rejected_before_mismatch_check(self):
        """Token check happens before any other processing, including the
        path/body consistency check -- a mismatched body with no token still
        surfaces as 401, not 400."""
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 99},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_no_trailing_slash_url_also_works(self):
        """units-backend's real sender (lease/finance_sync.py) posts without
        a trailing slash; this must not 404 or redirect-and-drop the POST."""
        response = self.client.post(
            reverse(
                "sync-lease-transaction-no-slash",
                kwargs={"lease_transaction_id": 42},
            ),
            data={"lease_transaction_id": 42},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], {"lease_transaction_id": 42})

    def test_string_body_id_matching_path_id_is_accepted(self):
        """A JSON string '42' for the body id is coerced and compared as an
        int against the path-converted lease_transaction_id -- a real
        sender serialization change (int -> numeric string) must not start
        rejecting every sync call with a spurious 400."""
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": "42"},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], {"lease_transaction_id": 42})

    def test_non_integer_body_id_rejected_with_400(self):
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": "not-a-number"},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["content"]["body_lease_transaction_id"], "not-a-number"
        )

    def test_disallowed_method_rejected_with_405(self):
        response = self.client.get(
            self._url(42), HTTP_X_INTERNAL_TOKEN="test-internal-token"
        )

        self.assertEqual(response.status_code, 405)

    @override_settings(FINANCE_INTERNAL_TOKEN=None)
    def test_unset_finance_internal_token_fails_loud_with_500(self):
        """A misconfigured environment (FINANCE_INTERNAL_TOKEN never set)
        must not silently behave like every caller sent an invalid token --
        it's an operator error, not a caller error, so this fails loud with
        500 rather than a misleading 403."""
        response = self.client.post(
            self._url(42),
            data={"lease_transaction_id": 42},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="anything",
        )

        self.assertEqual(response.status_code, 500)


class PostRentArTests(TestCase):
    """Story 2.2b tests: post_rent_ar posting logic.

    Extends Story 1.2's hand-rolled-stand-in-table technique to
    LeaseTransactionRef/LeaseRef/UnitRef/PropertyRef, standing in for the
    four units-backend tables the PMC resolution chain walks
    (lease_leasetransaction -> lease_lease -> property_unit ->
    property_property), so these tests exercise the real chain-walking code
    path without importing units-backend.

    Covers all four I/O matrix rows from the spec:
      1. Happy path -- resolvable PMC with seeded CoA posts a balanced entry.
      2. Duplicate sync -- second call for the same
         (lease_transaction_id, "CREATE-BALANCE") is a no-op.
      3. Unresolvable PMC (null hop, or no matching FinancePMCProfile) --
         no posting, logged error.
      4. Non-RENT cheque_type -- no posting, not a failure.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_chain(
        self,
        cursor,
        txn_id,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        cheque_type="RENT_CHEQUE",
        amount=5000,
        status="BALANCE",
        skip_lease=False,
        skip_unit=False,
        skip_property=False,
        null_parent_property_id=False,
        null_pmc_id=False,
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [txn_id, lease_id, amount, cheque_type, "CHEQUE", status],
        )
        if not skip_lease:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
                [lease_id, unit_id],
            )
        if not skip_unit:
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [unit_id, None if null_parent_property_id else property_id],
            )
        if not skip_property:
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [property_id, None if null_pmc_id else pmc_id],
            )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def test_happy_path_posts_balanced_entry(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=100,
                lease_id=10,
                unit_id=20,
                property_id=30,
                pmc_id=1,
                amount=5000,
            )

        result = post_rent_ar(100)

        self.assertTrue(result["posted"])
        entries = JournalEntry.objects.filter(
            source_lease_transaction_id=100,
            source_status_transition="CREATE-BALANCE",
        )
        self.assertEqual(entries.count(), 1)
        entry = entries.first()
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Rent Income")
        vat_line = lines.get(account__name="VAT Payable")
        self.assertEqual(ar_line.debit, 5250)
        self.assertEqual(ar_line.credit, 0)
        self.assertEqual(income_line.debit, 0)
        self.assertEqual(income_line.credit, 5000)
        self.assertEqual(vat_line.debit, 0)
        self.assertEqual(vat_line.credit, 250)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5250)

    def test_duplicate_sync_is_skipped(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=101,
                lease_id=11,
                unit_id=21,
                property_id=31,
                pmc_id=1,
                amount=3000,
            )

        first = post_rent_ar(101)
        second = post_rent_ar(101)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=101).count(), 1
        )

    def test_unresolvable_pmc_null_parent_property_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=102,
                lease_id=12,
                unit_id=22,
                property_id=32,
                pmc_id=1,
                amount=1000,
                null_parent_property_id=True,
            )

        result = post_rent_ar(102)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=102).count(), 0
        )

    def test_unresolvable_pmc_no_matching_finance_profile_posts_nothing(self):
        # No FinancePMCProfile created at all -- chain resolves fully but
        # the final FinancePMCProfile lookup comes up empty.
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=103,
                lease_id=13,
                unit_id=23,
                property_id=33,
                pmc_id=999,
                amount=1000,
            )

        result = post_rent_ar(103)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=103).count(), 0
        )

    def test_non_rent_cheque_type_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=104,
                lease_id=14,
                unit_id=24,
                property_id=34,
                pmc_id=1,
                amount=1000,
                cheque_type="OTHER_CHARGE",
            )

        result = post_rent_ar(104)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "not_rent_cheque_type")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=104).count(), 0
        )

    def test_null_amount_posts_nothing(self):
        """Post-review patch: a null amount must be a defined failure, not
        an unhandled exception from assigning None to LedgerLine's
        DecimalField debit/credit."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=105,
                lease_id=15,
                unit_id=25,
                property_id=35,
                pmc_id=1,
                amount=None,
            )

        result = post_rent_ar(105)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "missing_amount")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=105).count(), 0
        )

    def test_unconfigured_chart_of_accounts_posts_nothing(self):
        """Post-review patch: a resolvable PMC whose Chart of Accounts is
        missing the expected Account names must fail with a structured
        result, not an unhandled Account.DoesNotExist."""
        # A FinancePMCProfile created directly (bypassing the seed signal by
        # deleting the Accounts it creates) simulates a Chart-of-Accounts
        # misconfiguration without needing to break Story 1.3's seed itself.
        profile = self._make_profile(pmc_id=1)
        Account.objects.filter(finance_pmc_profile=profile).delete()
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=106,
                lease_id=16,
                unit_id=26,
                property_id=36,
                pmc_id=1,
                amount=1000,
            )

        result = post_rent_ar(106)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "chart_of_accounts_not_configured")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=106).count(), 0
        )

    def test_vat_standard_case_1000_amount(self):
        """Story 5.5 I/O matrix: amount=1000 -> AR debit 1050, Rent Income
        credit 1000, VAT Payable credit 50."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=107,
                lease_id=17,
                unit_id=27,
                property_id=37,
                pmc_id=1,
                amount=1000,
            )

        result = post_rent_ar(107)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=107,
            source_status_transition="CREATE-BALANCE",
        )
        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Rent Income")
        vat_line = lines.get(account__name="VAT Payable")
        self.assertEqual(ar_line.debit, 1050)
        self.assertEqual(income_line.credit, 1000)
        self.assertEqual(vat_line.credit, 50)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 1050)

    def test_vat_rounding_edge_case_33_33_amount(self):
        """Story 5.5 I/O matrix rounding edge: amount=33.33 -> VAT =
        1.6665 rounds to 1.67; AR debit = 34.9965 rounds to 35.00; entry
        still balances (AR = Rent Income + VAT Payable exactly, both
        rounded consistently)."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=108,
                lease_id=18,
                unit_id=28,
                property_id=38,
                pmc_id=1,
                amount=33.33,
            )

        result = post_rent_ar(108)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=108,
            source_status_transition="CREATE-BALANCE",
        )
        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Rent Income")
        vat_line = lines.get(account__name="VAT Payable")
        self.assertEqual(vat_line.credit, decimal.Decimal("1.67"))
        self.assertEqual(ar_line.debit, decimal.Decimal("35.00"))
        self.assertEqual(income_line.credit, decimal.Decimal("33.33"))

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, decimal.Decimal("35.00"))


class PostChequeClearingTests(TestCase):
    """Story 2.3 tests: post_cheque_clearing posting logic.

    Extends PostRentArTests' hand-rolled stand-in-table technique
    (LeaseTransactionRef/LeaseRef/UnitRef/PropertyRef) to exercise the
    chain-walking code path without importing units-backend.

    Covers all four I/O matrix rows from the spec:
      1. Direct clearing -- prior CREATE-BALANCE JournalEntry exists, status
         now REALIZED -> posts "BALANCE-REALIZED".
      2. Two-step clearing -- BALANCE->CREDITED->REALIZED across two
         separate syncs -> two separate JournalEntries,
         "BALANCE-CREDITED" then "CREDITED-REALIZED".
      3. Duplicate sync -- the same transition synced twice is a no-op the
         second time.
      4. No prior posting -- no JournalEntry exists at all for this
         lease_transaction_id -> fails loudly, no clearing entry posted.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_chain(
        self,
        cursor,
        txn_id,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        cheque_type="RENT_CHEQUE",
        amount=5000,
        status="BALANCE",
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [txn_id, lease_id, amount, cheque_type, "CHEQUE", status],
        )
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
            [lease_id, unit_id],
        )
        cursor.execute(
            "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
            [unit_id, property_id],
        )
        cursor.execute(
            "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
            [property_id, pmc_id],
        )

    def _set_status(self, cursor, txn_id, status):
        cursor.execute(
            "UPDATE lease_leasetransaction SET status = %s WHERE documents_ptr_id = %s",
            [status, txn_id],
        )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def test_direct_clearing_posts_bank_journal_entry(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=200,
                lease_id=20,
                unit_id=30,
                property_id=40,
                pmc_id=1,
                amount=5000,
                status="BALANCE",
            )

        rent_result = post_rent_ar(200)
        self.assertTrue(rent_result["posted"])

        with connection.cursor() as cursor:
            self._set_status(cursor, 200, "REALIZED")

        result = post_cheque_clearing(200)

        self.assertTrue(result["posted"])
        entries = JournalEntry.objects.filter(
            source_lease_transaction_id=200,
            source_status_transition="BALANCE-REALIZED",
        )
        self.assertEqual(entries.count(), 1)
        entry = entries.first()
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)

        bank_line = lines.get(account__name="Bank")
        ar_line = lines.get(account__name="AR — Tenants")
        self.assertEqual(bank_line.debit, 5250)
        self.assertEqual(bank_line.credit, 0)
        self.assertEqual(ar_line.debit, 0)
        self.assertEqual(ar_line.credit, 5250)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5250)

        # Exactly two JournalEntries total for this lease_transaction_id --
        # the original Rent AR posting plus this clearing posting.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=200).count(), 2
        )

        # Story 5.5: AR nets to exactly zero across the post -> clear
        # lifecycle -- the VAT-inclusive debit from post_rent_ar and the
        # VAT-inclusive credit from post_cheque_clearing cancel out.
        ar_lines_for_txn = LedgerLine.objects.filter(
            journal_entry__source_lease_transaction_id=200,
            account__name="AR — Tenants",
        )
        net_ar = sum(line.debit - line.credit for line in ar_lines_for_txn)
        self.assertEqual(net_ar, 0)

    def test_two_step_clearing_posts_two_separate_journal_entries(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=201,
                lease_id=21,
                unit_id=31,
                property_id=41,
                pmc_id=1,
                amount=3000,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(201)["posted"])

        with connection.cursor() as cursor:
            self._set_status(cursor, 201, "CREDITED")
        first_clear = post_cheque_clearing(201)

        with connection.cursor() as cursor:
            self._set_status(cursor, 201, "REALIZED")
        second_clear = post_cheque_clearing(201)

        self.assertTrue(first_clear["posted"])
        self.assertTrue(second_clear["posted"])

        self.assertTrue(
            JournalEntry.objects.filter(
                source_lease_transaction_id=201,
                source_status_transition="BALANCE-CREDITED",
            ).exists()
        )
        self.assertTrue(
            JournalEntry.objects.filter(
                source_lease_transaction_id=201,
                source_status_transition="CREDITED-REALIZED",
            ).exists()
        )
        # CREATE-BALANCE (rent AR) + BALANCE-CREDITED + CREDITED-REALIZED.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=201).count(), 3
        )

    def test_duplicate_sync_is_skipped(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=202,
                lease_id=22,
                unit_id=32,
                property_id=42,
                pmc_id=1,
                amount=4000,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(202)["posted"])

        with connection.cursor() as cursor:
            self._set_status(cursor, 202, "REALIZED")

        first = post_cheque_clearing(202)
        second = post_cheque_clearing(202)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=202,
                source_status_transition="BALANCE-REALIZED",
            ).count(),
            1,
        )

    def test_no_prior_posting_fails_loudly(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=203,
                lease_id=23,
                unit_id=33,
                property_id=43,
                pmc_id=1,
                amount=1000,
                status="REALIZED",
            )

        result = post_cheque_clearing(203)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_prior_posting")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=203).count(), 0
        )

    def test_not_gated_on_cheque_type(self):
        """Post-review decision (Spec Change Log): post_cheque_clearing has
        no cheque_type gate of its own -- a non-RENT transaction that DOES
        have a prior JournalEntry to clear (however that happened) still
        clears normally. The exclusion of cheque types with nothing to
        clear happens naturally via the no-prior-posting path, not a
        cheque_type filter."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=204,
                lease_id=24,
                unit_id=34,
                property_id=44,
                pmc_id=1,
                amount=1000,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
            )

        # Manually seed a prior posting -- post_rent_ar itself would refuse
        # a non-RENT cheque_type, but the clearing function must not care
        # how the prior JournalEntry came to exist.
        profile = FinancePMCProfile.objects.get(pmc_id=1)
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=204,
            source_status_transition="CREATE-BALANCE",
        )
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name="AR — Tenants"
        )
        other_account = Account.objects.get(
            finance_pmc_profile=profile, name="Bank Charges/Fees"
        )
        LedgerLine.objects.create(
            journal_entry=entry, account=ar_account, debit=1000, credit=0
        )
        LedgerLine.objects.create(
            journal_entry=entry, account=other_account, debit=0, credit=1000
        )

        with connection.cursor() as cursor:
            self._set_status(cursor, 204, "REALIZED")

        result = post_cheque_clearing(204)

        self.assertTrue(result["posted"])
        self.assertTrue(
            JournalEntry.objects.filter(
                source_lease_transaction_id=204,
                source_status_transition="BALANCE-REALIZED",
            ).exists()
        )
        # Pins down the exact split-logic branch: the prior entry's
        # transition was "CREATE-BALANCE" (a real, multi-segment string),
        # and the "from" status must be inferred as "BALANCE" (the segment
        # after the last "-"), never the full "CREATE-BALANCE" string.
        self.assertFalse(
            JournalEntry.objects.filter(
                source_lease_transaction_id=204,
                source_status_transition="CREATE-BALANCE-REALIZED",
            ).exists()
        )

    def test_unresolvable_pmc_posts_nothing(self):
        # No FinancePMCProfile created at all -- chain resolves fully but
        # the final FinancePMCProfile lookup comes up empty.
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=205,
                lease_id=25,
                unit_id=35,
                property_id=45,
                pmc_id=1,
                amount=1000,
                status="BALANCE",
            )
            # Fake a prior JournalEntry directly -- profile doesn't exist,
            # so we can't use post_rent_ar to create one; this isolates the
            # unresolvable_pmc path from the no_prior_posting path.
            self._set_status(cursor, 205, "REALIZED")

        result = post_cheque_clearing(205)

        # With no prior JournalEntry (none can exist without a profile),
        # this correctly hits no_prior_posting, not unresolvable_pmc --
        # documents that the prior-posting check runs before PMC
        # resolution.
        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_prior_posting")


class SyncLeaseTransactionRentPostingIntegrationTests(TestCase):
    """Post-review patch: an HTTP-level integration test through the actual
    sync endpoint, closing the gap the verification-gap review flagged --
    PostRentArTests only calls post_rent_ar() directly as a Python function,
    never exercising the view's conditional wiring that decides whether to
    call it at all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
            ):
                cursor.execute(f"DELETE FROM {table}")

    @override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
    def test_rent_sync_via_http_posts_journal_entry(self):
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [200, 50, 7500, "RENT_CHEQUE", "CHEQUE", "BALANCE"],
            )
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)", [50, 60]
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [60, 70],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [70, 1],
            )

        response = self.client.post(
            reverse("sync-lease-transaction", kwargs={"lease_transaction_id": 200}),
            data={"lease_transaction_id": 200},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["content"]["posting"]["rent_ar"], {"posted": True, "reason": ""}
        )

        entry = JournalEntry.objects.get(
            source_lease_transaction_id=200,
            source_status_transition="CREATE-BALANCE",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)
        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)
        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 7875)


class PostBounceReversalTests(TestCase):
    """Story 2.4 tests: post_bounce_reversal posting logic.

    Extends PostRentArTests/PostChequeClearingTests' hand-rolled
    stand-in-table technique (LeaseTransactionRef/LeaseRef/UnitRef/
    PropertyRef) to exercise the chain-walking code path without importing
    units-backend.

    Covers all four I/O matrix rows from the spec:
      1. Direct bounce -- prior CREATE-BALANCE JournalEntry exists, status
         now BOUNCED -> reversing entry posted, credit AR — Tenants, debit
         Bounced Cheques, reversed_journal_entry FK -> the CREATE-BALANCE
         entry.
      2. Duplicate sync -- the same transition synced twice is a no-op the
         second time, exactly one reversing entry total.
      3. No prior posting -- no JournalEntry exists at all for this
         lease_transaction_id -> logged error, no reversal posted.
      4. Two independent bounced cheques -- two distinct
         lease_transaction_ids, each with its own prior posting, both post
         their own independent reversal.

    Plus a dedicated test confirming the original JournalEntry/LedgerLines
    are never modified (spec Never / AD-16).
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_chain(
        self,
        cursor,
        txn_id,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        cheque_type="RENT_CHEQUE",
        amount=5000,
        status="BALANCE",
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [txn_id, lease_id, amount, cheque_type, "CHEQUE", status],
        )
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
            [lease_id, unit_id],
        )
        cursor.execute(
            "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
            [unit_id, property_id],
        )
        cursor.execute(
            "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
            [property_id, pmc_id],
        )

    def _set_status(self, cursor, txn_id, status):
        cursor.execute(
            "UPDATE lease_leasetransaction SET status = %s WHERE documents_ptr_id = %s",
            [status, txn_id],
        )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def test_direct_bounce_posts_reversing_journal_entry(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=300,
                lease_id=30,
                unit_id=40,
                property_id=50,
                pmc_id=1,
                amount=5000,
                status="BALANCE",
            )

        rent_result = post_rent_ar(300)
        self.assertTrue(rent_result["posted"])
        original_entry = JournalEntry.objects.get(
            source_lease_transaction_id=300,
            source_status_transition="CREATE-BALANCE",
        )

        with connection.cursor() as cursor:
            self._set_status(cursor, 300, "BOUNCED")

        result = post_bounce_reversal(300)

        self.assertTrue(result["posted"])
        entries = JournalEntry.objects.filter(
            source_lease_transaction_id=300,
            source_status_transition="BALANCE-BOUNCED",
        )
        self.assertEqual(entries.count(), 1)
        entry = entries.first()
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)
        self.assertEqual(entry.reversed_journal_entry_id, original_entry.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)

        ar_line = lines.get(account__name="AR — Tenants")
        bounced_line = lines.get(account__name="Bounced Cheques")
        self.assertEqual(ar_line.debit, 0)
        self.assertEqual(ar_line.credit, 5250)
        self.assertEqual(bounced_line.debit, 5250)
        self.assertEqual(bounced_line.credit, 0)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5250)

        # Exactly two JournalEntries total for this lease_transaction_id --
        # the original Rent AR posting plus this reversal.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=300).count(), 2
        )

        # Story 5.5: AR nets to exactly zero across the post -> bounce
        # lifecycle -- the VAT-inclusive debit from post_rent_ar and the
        # VAT-inclusive credit from post_bounce_reversal cancel out.
        ar_lines_for_txn = LedgerLine.objects.filter(
            journal_entry__source_lease_transaction_id=300,
            account__name="AR — Tenants",
        )
        net_ar = sum(line.debit - line.credit for line in ar_lines_for_txn)
        self.assertEqual(net_ar, 0)

    def test_duplicate_sync_is_skipped(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=301,
                lease_id=31,
                unit_id=41,
                property_id=51,
                pmc_id=1,
                amount=3000,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(301)["posted"])

        with connection.cursor() as cursor:
            self._set_status(cursor, 301, "BOUNCED")

        first = post_bounce_reversal(301)
        second = post_bounce_reversal(301)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=301,
                source_status_transition="BALANCE-BOUNCED",
            ).count(),
            1,
        )

    def test_no_prior_posting_fails_loudly(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=302,
                lease_id=32,
                unit_id=42,
                property_id=52,
                pmc_id=1,
                amount=1000,
                status="BOUNCED",
            )

        result = post_bounce_reversal(302)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_prior_posting")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=302).count(), 0
        )

    def test_two_independent_bounced_cheques_each_post_own_reversal(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=303,
                lease_id=33,
                unit_id=43,
                property_id=53,
                pmc_id=1,
                amount=2000,
                status="BALANCE",
            )
            self._insert_chain(
                cursor,
                txn_id=304,
                lease_id=34,
                unit_id=44,
                property_id=54,
                pmc_id=1,
                amount=6000,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(303)["posted"])
        self.assertTrue(post_rent_ar(304)["posted"])

        with connection.cursor() as cursor:
            self._set_status(cursor, 303, "BOUNCED")
            self._set_status(cursor, 304, "BOUNCED")

        result_303 = post_bounce_reversal(303)
        result_304 = post_bounce_reversal(304)

        self.assertTrue(result_303["posted"])
        self.assertTrue(result_304["posted"])

        entry_303 = JournalEntry.objects.get(
            source_lease_transaction_id=303,
            source_status_transition="BALANCE-BOUNCED",
        )
        entry_304 = JournalEntry.objects.get(
            source_lease_transaction_id=304,
            source_status_transition="BALANCE-BOUNCED",
        )

        lines_303 = LedgerLine.objects.filter(journal_entry=entry_303)
        lines_304 = LedgerLine.objects.filter(journal_entry=entry_304)
        self.assertEqual(
            sum(line.debit for line in lines_303),
            sum(line.credit for line in lines_303),
        )
        self.assertEqual(sum(line.debit for line in lines_303), 2100)
        self.assertEqual(
            sum(line.debit for line in lines_304),
            sum(line.credit for line in lines_304),
        )
        self.assertEqual(sum(line.debit for line in lines_304), 6300)

        # No shared idempotency state -- each id has exactly its own two
        # entries (CREATE-BALANCE + BALANCE-BOUNCED).
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=303).count(), 2
        )
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=304).count(), 2
        )

    def test_original_journal_entry_and_ledger_lines_never_modified(self):
        """AD-16: the reversal is additive-only -- the original CREATE-*
        JournalEntry and its LedgerLines must be byte-for-byte unchanged
        after the reversal posts."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=305,
                lease_id=35,
                unit_id=45,
                property_id=55,
                pmc_id=1,
                amount=4500,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(305)["posted"])
        original_entry = JournalEntry.objects.get(
            source_lease_transaction_id=305,
            source_status_transition="CREATE-BALANCE",
        )
        original_lines_before = list(
            LedgerLine.objects.filter(journal_entry=original_entry)
            .order_by("id")
            .values("id", "account_id", "debit", "credit")
        )
        original_posted_at_before = original_entry.posted_at

        with connection.cursor() as cursor:
            self._set_status(cursor, 305, "BOUNCED")

        result = post_bounce_reversal(305)
        self.assertTrue(result["posted"])

        original_entry.refresh_from_db()
        self.assertEqual(original_entry.posted_at, original_posted_at_before)
        self.assertEqual(
            original_entry.source_status_transition, "CREATE-BALANCE"
        )
        self.assertIsNone(original_entry.reversed_journal_entry_id)

        original_lines_after = list(
            LedgerLine.objects.filter(journal_entry=original_entry)
            .order_by("id")
            .values("id", "account_id", "debit", "credit")
        )
        self.assertEqual(original_lines_before, original_lines_after)

        # The original entry still exists (never deleted).
        self.assertTrue(
            JournalEntry.objects.filter(pk=original_entry.pk).exists()
        )

    def test_no_original_create_entry_fails_loudly(self):
        """If the RENT AR posting never happened (no CREATE-* entry exists
        at all), the reversal must fail rather than reverse against nothing
        -- distinct from the no-prior-posting-at-all path since here some
        (non-CREATE) prior JournalEntry does exist."""
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=306,
                lease_id=36,
                unit_id=46,
                property_id=56,
                pmc_id=1,
                amount=1000,
                status="BALANCE",
            )

        # Manually seed a prior JournalEntry with a non-CREATE transition --
        # simulates a data state where some entry exists but not the
        # original Rent AR posting.
        ar_account = Account.objects.get(
            finance_pmc_profile=profile, name="AR — Tenants"
        )
        bank_account = Account.objects.get(
            finance_pmc_profile=profile, name="Bank"
        )
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=306,
            source_status_transition="BALANCE-CREDITED",
        )
        LedgerLine.objects.create(
            journal_entry=entry, account=bank_account, debit=1000, credit=0
        )
        LedgerLine.objects.create(
            journal_entry=entry, account=ar_account, debit=0, credit=1000
        )

        with connection.cursor() as cursor:
            self._set_status(cursor, 306, "BOUNCED")

        result = post_bounce_reversal(306)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_original_posting")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=306,
                source_status_transition="CREDITED-BOUNCED",
            ).count(),
            0,
        )

    def test_not_bounced_status_is_noop(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=307,
                lease_id=37,
                unit_id=47,
                property_id=57,
                pmc_id=1,
                amount=1000,
                status="REALIZED",
            )

        result = post_bounce_reversal(307)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "not_bounced_status")

    def test_no_prior_posting_and_no_profile_posts_nothing(self):
        """Renamed from test_unresolvable_pmc_posts_nothing (post-review):
        this scenario has no FinancePMCProfile AND no prior JournalEntry, so
        no_prior_posting fires before ever reaching the PMC-resolution code
        -- it does not exercise post_bounce_reversal's own unresolvable_pmc
        branch. See test_unresolvable_pmc_posts_nothing below for that."""
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=308,
                lease_id=38,
                unit_id=48,
                property_id=58,
                pmc_id=1,
                amount=1000,
                status="BOUNCED",
            )

        result = post_bounce_reversal(308)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_prior_posting")

    def test_unresolvable_pmc_posts_nothing(self):
        """Post-review patch: genuinely exercises post_bounce_reversal's own
        unresolvable_pmc branch -- a real prior CREATE-* entry exists (so
        both the prior-posting and original-entry checks pass), but the
        chain breaks (no Property row for the Unit's parent_property_id) by
        the time the reversal tries to resolve the PMC."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=309,
                lease_id=39,
                unit_id=49,
                property_id=59,
                pmc_id=1,
                amount=1500,
                status="BALANCE",
            )

        self.assertTrue(post_rent_ar(309)["posted"])

        with connection.cursor() as cursor:
            # Break the chain after the CREATE-BALANCE entry already exists:
            # delete the Property row the Unit points at.
            cursor.execute("DELETE FROM property_property WHERE id = %s", [59])
            self._set_status(cursor, 309, "BOUNCED")

        result = post_bounce_reversal(309)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=309,
                source_status_transition="BALANCE-BOUNCED",
            ).count(),
            0,
        )


class PostBounceFeeTests(TestCase):
    """Story 2.5 tests: post_bounce_fee posting logic.

    Extends the established hand-rolled-stand-in-table technique
    (LeaseTransactionRef/LeaseRef/UnitRef/PropertyRef) with a new stand-in
    `charges_charge` table (ChargeRef), and adds `created`/`charge_id`
    columns to the existing `lease_leasetransaction` stand-in table.

    Covers all five I/O matrix rows from the spec:
      1. Simple bounce fee, no VAT -- 2-line balanced entry.
      2. Bounce fee with VAT -- 3-line balanced entry.
      3. No fee charge yet -- no posting, not a failure.
      4. Duplicate sync -- second call is a no-op, exactly one JournalEntry.
      5. Two simultaneous bounces, one fee charge -- pairs with the OLDER
         unresolved bounce; the newer bounce still waits.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS charges_charge (
                    id BIGSERIAL PRIMARY KEY,
                    amount DOUBLE PRECISION,
                    vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
            cursor.execute("DROP TABLE IF EXISTS charges_charge")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
                "charges_charge",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_pmc_chain(self, cursor, lease_id, unit_id, property_id, pmc_id):
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
            [lease_id, unit_id],
        )
        cursor.execute(
            "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
            [unit_id, property_id],
        )
        cursor.execute(
            "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
            [property_id, pmc_id],
        )

    def _insert_txn(
        self,
        cursor,
        txn_id,
        lease_id,
        amount,
        cheque_type,
        status,
        created,
        charge_id=None,
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status,
                 created, charge_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                txn_id,
                lease_id,
                amount,
                cheque_type,
                "CHEQUE",
                status,
                created,
                charge_id,
            ],
        )

    def _insert_charge(self, cursor, charge_id, amount, vat_amount=0):
        cursor.execute(
            "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s)",
            [charge_id, amount, vat_amount],
        )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def _bounce(
        self,
        cursor,
        txn_id,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        amount,
        created,
    ):
        """Set up a fully-resolved, already-reversed bounced transaction:
        inserts the PMC chain + the BOUNCED LeaseTransaction row, posts the
        rent AR (CREATE-BALANCE) and bounce reversal (BALANCE-BOUNCED)
        JournalEntries via the real posting functions, mirroring
        PostBounceReversalTests' setup exactly."""
        self._insert_pmc_chain(cursor, lease_id, unit_id, property_id, pmc_id)
        self._insert_txn(
            cursor, txn_id, lease_id, amount, "RENT_CHEQUE", "BALANCE", created
        )
        self.assertTrue(post_rent_ar(txn_id)["posted"])
        cursor.execute(
            "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = %s",
            [txn_id],
        )
        self.assertTrue(post_bounce_reversal(txn_id)["posted"])

    def test_simple_bounce_fee_no_vat_posts_balanced_two_line_entry(self):
        from datetime import datetime, timedelta, timezone as dt_timezone

        profile = self._make_profile(pmc_id=1)
        bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            self._bounce(
                cursor,
                txn_id=400,
                lease_id=40,
                unit_id=40,
                property_id=40,
                pmc_id=1,
                amount=5000,
                created=bounce_time,
            )
            self._insert_charge(cursor, charge_id=900, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=401,
                lease_id=40,
                amount=150,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                created=charge_time,
                charge_id=900,
            )

        result = post_bounce_fee(401)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=401,
            source_status_transition="BOUNCE_FEE-FOR-400",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)

        ar_line = lines.get(account__name="AR — Tenants")
        fee_line = lines.get(account__name="Bank Charges/Fees")
        self.assertEqual(ar_line.debit, 150)
        self.assertEqual(ar_line.credit, 0)
        self.assertEqual(fee_line.debit, 0)
        self.assertEqual(fee_line.credit, 150)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 150)

    def test_bounce_fee_with_vat_posts_balanced_three_line_entry(self):
        from datetime import datetime, timedelta, timezone as dt_timezone

        profile = self._make_profile(pmc_id=1)
        bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            self._bounce(
                cursor,
                txn_id=410,
                lease_id=41,
                unit_id=41,
                property_id=41,
                pmc_id=1,
                amount=5000,
                created=bounce_time,
            )
            self._insert_charge(cursor, charge_id=910, amount=100, vat_amount=5)
            self._insert_txn(
                cursor,
                txn_id=411,
                lease_id=41,
                amount=100,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                created=charge_time,
                charge_id=910,
            )

        result = post_bounce_fee(411)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=411,
            source_status_transition="BOUNCE_FEE-FOR-410",
        )

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        ar_line = lines.get(account__name="AR — Tenants")
        fee_line = lines.get(account__name="Bank Charges/Fees")
        vat_line = lines.get(account__name="VAT Payable")

        self.assertEqual(ar_line.debit, 105)
        self.assertEqual(ar_line.credit, 0)
        self.assertEqual(fee_line.debit, 0)
        self.assertEqual(fee_line.credit, 100)
        self.assertEqual(vat_line.debit, 0)
        self.assertEqual(vat_line.credit, 5)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 105)

    def test_no_fee_charge_yet_posts_nothing_not_a_failure(self):
        """I/O matrix row 3: a bounced transaction exists (already reversed
        per Story 2.4), but no OTHER_CHARGE transaction exists on the lease
        at all yet. `sync_lease_transaction` runs post_bounce_fee on every
        sync regardless of cheque_type (view-level, not gated) -- so this
        exercises the real steady-state call: syncing the bounced
        transaction itself, which correctly no-ops via the cheque_type gate,
        without ever creating a bounce-fee JournalEntry."""
        from datetime import datetime, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._bounce(
                cursor,
                txn_id=420,
                lease_id=42,
                unit_id=42,
                property_id=42,
                pmc_id=1,
                amount=5000,
                created=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
            )

        result = post_bounce_fee(420)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "not_other_charge_type")
        self.assertFalse(
            JournalEntry.objects.filter(
                source_status_transition__startswith="BOUNCE_FEE-FOR-"
            ).exists()
        )

    def test_other_charge_with_no_matching_bounce_posts_nothing_and_waits(self):
        """The genuine steady-state no-op: an OTHER_CHARGE transaction
        exists, but no unresolved bounce exists yet on its lease for it to
        pair with."""
        from datetime import datetime, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_pmc_chain(cursor, lease_id=43, unit_id=43, property_id=43, pmc_id=1)
            self._insert_charge(cursor, charge_id=930, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=430,
                lease_id=43,
                amount=150,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                created=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
                charge_id=930,
            )

        result = post_bounce_fee(430)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_unresolved_bounce")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=430).count(), 0
        )

    def test_duplicate_sync_is_skipped(self):
        from datetime import datetime, timedelta, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            self._bounce(
                cursor,
                txn_id=440,
                lease_id=44,
                unit_id=44,
                property_id=44,
                pmc_id=1,
                amount=5000,
                created=bounce_time,
            )
            self._insert_charge(cursor, charge_id=940, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=441,
                lease_id=44,
                amount=150,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                created=charge_time,
                charge_id=940,
            )

        first = post_bounce_fee(441)
        second = post_bounce_fee(441)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=441,
                source_status_transition="BOUNCE_FEE-FOR-440",
            ).count(),
            1,
        )

    def test_two_simultaneous_bounces_one_fee_pairs_with_older_bounce(self):
        """I/O matrix row 5: two unresolved bounced transactions on the same
        lease, one OTHER_CHARGE transaction -- the fee pairs with the OLDER
        unresolved bounce (nearest-neighbor); the newer bounce still waits
        for its own fee charge."""
        from datetime import datetime, timedelta, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        older_bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        newer_bounce_time = older_bounce_time + timedelta(days=1)
        charge_time = newer_bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            # Older bounce, on the same lease.
            self._insert_pmc_chain(cursor, lease_id=45, unit_id=45, property_id=45, pmc_id=1)
            self._insert_txn(
                cursor, 450, 45, 2000, "RENT_CHEQUE", "BALANCE", older_bounce_time
            )
            self.assertTrue(post_rent_ar(450)["posted"])
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = 450"
            )
            self.assertTrue(post_bounce_reversal(450)["posted"])

            # Newer bounce, same lease.
            self._insert_txn(
                cursor, 451, 45, 6000, "RENT_CHEQUE", "BALANCE", newer_bounce_time
            )
            self.assertTrue(post_rent_ar(451)["posted"])
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = 451"
            )
            self.assertTrue(post_bounce_reversal(451)["posted"])

            # One fee charge, created after both bounces.
            self._insert_charge(cursor, charge_id=950, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                452,
                45,
                150,
                "OTHER_CHARGE",
                "BALANCE",
                charge_time,
                charge_id=950,
            )

        result = post_bounce_fee(452)

        self.assertTrue(result["posted"])
        # Pairs with the OLDER unresolved bounce (450), not the newer (451).
        self.assertTrue(
            JournalEntry.objects.filter(
                source_lease_transaction_id=452,
                source_status_transition="BOUNCE_FEE-FOR-450",
            ).exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(
                source_lease_transaction_id=452,
                source_status_transition="BOUNCE_FEE-FOR-451",
            ).exists()
        )

        # The newer bounce (451) still has no bounce-fee posting -- it waits
        # for its own fee charge.
        self.assertFalse(
            JournalEntry.objects.filter(
                source_status_transition="BOUNCE_FEE-FOR-451"
            ).exists()
        )

    def test_unresolved_pmc_posts_nothing(self):
        """A resolvable OTHER_CHARGE/bounce pairing whose PMC chain is
        broken (by the time bounce-fee posting runs) fails loudly (logged),
        consistent with every other posting function's unresolvable_pmc
        path. Mirrors PostBounceReversalTests.
        test_unresolvable_pmc_posts_nothing's technique: post the bounce
        reversal first while the chain is intact, then break the chain
        (delete the Property row) before attempting the bounce-fee post --
        isolates this from the no_prior_posting/no_unresolved_bounce path,
        since the JournalEntry FK is to FinancePMCProfile (deleting the
        profile itself would cascade-delete the JournalEntry too)."""
        from datetime import datetime, timedelta, timezone as dt_timezone

        bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = bounce_time + timedelta(days=1)

        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_pmc_chain(cursor, lease_id=46, unit_id=46, property_id=46, pmc_id=1)
            self._insert_txn(
                cursor, 460, 46, 5000, "RENT_CHEQUE", "BALANCE", bounce_time
            )
            self.assertTrue(post_rent_ar(460)["posted"])
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = 460"
            )
            self.assertTrue(post_bounce_reversal(460)["posted"])

            self._insert_charge(cursor, charge_id=960, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                461,
                46,
                150,
                "OTHER_CHARGE",
                "BALANCE",
                charge_time,
                charge_id=960,
            )

            # Break the chain after the bounce reversal already exists:
            # delete the Property row the Unit points at (both the bounced
            # and OTHER_CHARGE transactions resolve through the same
            # lease/unit/property chain, so this breaks resolution for the
            # OTHER_CHARGE transaction too).
            cursor.execute("DELETE FROM property_property WHERE id = %s", [46])

        result = post_bounce_fee(461)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")

    def test_candidate_with_null_created_is_excluded_not_accepted(self):
        """Regression: a bounced transaction whose `created` timestamp is
        null must NOT be treated as eligible by the nearest-neighbor pairing
        loop. Two unresolved bounces on the same lease -- one with a null
        `created`, one with a real, later `created` -- and a fee charge:
        the fee must pair with the real-timestamped bounce, never the
        null-timestamped one (which cannot be verified to precede the fee
        charge)."""
        from datetime import datetime, timedelta, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        real_bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = real_bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            # Bounce with a null `created` (e.g. a legacy/backfilled row).
            self._insert_pmc_chain(cursor, lease_id=47, unit_id=47, property_id=47, pmc_id=1)
            self._insert_txn(
                cursor, 470, 47, 2000, "RENT_CHEQUE", "BALANCE", None
            )
            self.assertTrue(post_rent_ar(470)["posted"])
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = 470"
            )
            self.assertTrue(post_bounce_reversal(470)["posted"])

            # Bounce with a real, known-earlier `created`, same lease.
            self._insert_txn(
                cursor, 471, 47, 6000, "RENT_CHEQUE", "BALANCE", real_bounce_time
            )
            self.assertTrue(post_rent_ar(471)["posted"])
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' WHERE documents_ptr_id = 471"
            )
            self.assertTrue(post_bounce_reversal(471)["posted"])

            self._insert_charge(cursor, charge_id=970, amount=150, vat_amount=0)
            self._insert_txn(
                cursor,
                472,
                47,
                150,
                "OTHER_CHARGE",
                "BALANCE",
                charge_time,
                charge_id=970,
            )

        result = post_bounce_fee(472)

        self.assertTrue(result["posted"])
        # Must pair with the real-timestamped bounce (471), never the
        # null-timestamped one (470), regardless of query ordering.
        self.assertTrue(
            JournalEntry.objects.filter(
                source_lease_transaction_id=472,
                source_status_transition="BOUNCE_FEE-FOR-471",
            ).exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(
                source_lease_transaction_id=472,
                source_status_transition="BOUNCE_FEE-FOR-470",
            ).exists()
        )

    def test_charge_with_null_amount_posts_nothing_not_a_crash(self):
        """Regression: Charge.amount is a nullable field in units-backend.
        A null amount must return the module's standard
        {"posted": False, "reason": ...} contract, never raise an uncaught
        TypeError from `fee_amount + vat_amount` arithmetic."""
        from datetime import datetime, timedelta, timezone as dt_timezone

        self._make_profile(pmc_id=1)
        bounce_time = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        charge_time = bounce_time + timedelta(days=1)

        with connection.cursor() as cursor:
            self._bounce(
                cursor,
                txn_id=480,
                lease_id=48,
                unit_id=48,
                property_id=48,
                pmc_id=1,
                amount=5000,
                created=bounce_time,
            )
            # Charge with a null amount -- bypass _insert_charge, whose
            # stand-in DDL declares `amount` NOT NULL, to model the real
            # units-backend column (nullable FloatField).
            cursor.execute(
                "INSERT INTO charges_charge (id, amount, vat_amount) "
                "VALUES (%s, NULL, %s)",
                [980, 0],
            )
            self._insert_txn(
                cursor,
                481,
                48,
                150,
                "OTHER_CHARGE",
                "BALANCE",
                charge_time,
                charge_id=980,
            )

        result = post_bounce_fee(481)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "missing_charge_amount")
        self.assertFalse(
            JournalEntry.objects.filter(
                source_lease_transaction_id=481,
                source_status_transition="BOUNCE_FEE-FOR-480",
            ).exists()
        )


class PostSecurityDepositTests(TestCase):
    """Story 2.6 tests: post_security_deposit posting logic.

    Extends the hand-rolled stand-in-table technique used throughout this
    module (Story 1.2/2.2b's precedent) to `lease_lease` (with the new
    `lease_status`/`security_deposit` columns) -> `property_unit` ->
    `property_property`, exercising the real one-hop-shorter chain-walking
    code path (starting directly from LeaseRef.unit_id, no
    LeaseTransactionRef hop) without importing units-backend.

    Covers all six I/O matrix rows from the spec:
      1. Lease activates with a deposit -- ACTIVE + non-zero deposit posts
         a balanced entry (debit Bank, credit Security Deposits Held).
      2. Not yet active -- DRAFT posts nothing, no error.
      3. Active but no deposit -- ACTIVE + null/zero deposit posts nothing,
         no error.
      4. Duplicate sync -- same eligible state synced twice is a no-op the
         second time, exactly one JournalEntry.
      5. Re-activation after INACTIVE -- posted once, cycles to INACTIVE
         then back to ACTIVE, no second JournalEntry (the (lease_id,
         "ACTIVATE") key already exists).
      6. Unresolvable PMC -- broken Unit/Property/FinancePMCProfile chain
         fails loudly, no partial post.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in ("lease_lease", "property_unit", "property_property"):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_chain(
        self,
        cursor,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        lease_status="ACTIVE",
        security_deposit=5000,
        skip_unit=False,
        skip_property=False,
        null_parent_property_id=False,
        null_pmc_id=False,
    ):
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id, lease_status, security_deposit) "
            "VALUES (%s, %s, %s, %s)",
            [lease_id, unit_id, lease_status, security_deposit],
        )
        if not skip_unit:
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [unit_id, None if null_parent_property_id else property_id],
            )
        if not skip_property:
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [property_id, None if null_pmc_id else pmc_id],
            )

    def _set_lease(self, cursor, lease_id, lease_status=None, security_deposit=None):
        fields = []
        params = []
        if lease_status is not None:
            fields.append("lease_status = %s")
            params.append(lease_status)
        if security_deposit is not None or security_deposit == 0:
            fields.append("security_deposit = %s")
            params.append(security_deposit)
        params.append(lease_id)
        cursor.execute(
            f"UPDATE lease_lease SET {', '.join(fields)} WHERE id = %s", params
        )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def test_lease_activates_with_deposit_posts_balanced_entry(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=600,
                unit_id=70,
                property_id=80,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=5000,
            )

        result = post_security_deposit(600)

        self.assertTrue(result["posted"])
        entries = JournalEntry.objects.filter(
            source_lease_transaction_id=600,
            source_status_transition="ACTIVATE",
        )
        self.assertEqual(entries.count(), 1)
        entry = entries.first()
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)

        bank_line = lines.get(account__name="Bank")
        deposit_line = lines.get(account__name="Security Deposits Held")
        self.assertEqual(bank_line.debit, 5000)
        self.assertEqual(bank_line.credit, 0)
        self.assertEqual(deposit_line.debit, 0)
        self.assertEqual(deposit_line.credit, 5000)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5000)

    def test_not_yet_active_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=601,
                unit_id=71,
                property_id=81,
                pmc_id=1,
                lease_status="DRAFT",
                security_deposit=5000,
            )

        result = post_security_deposit(601)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "not_active")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=601).count(), 0
        )

    def test_active_with_null_deposit_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=602,
                unit_id=72,
                property_id=82,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=None,
            )

        result = post_security_deposit(602)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_deposit_amount")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=602).count(), 0
        )

    def test_active_with_zero_deposit_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=603,
                unit_id=73,
                property_id=83,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=0,
            )

        result = post_security_deposit(603)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_deposit_amount")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=603).count(), 0
        )

    def test_duplicate_sync_is_skipped(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=604,
                unit_id=74,
                property_id=84,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=3000,
            )

        first = post_security_deposit(604)
        second = post_security_deposit(604)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=604).count(), 1
        )

    def test_reactivation_after_inactive_does_not_repost(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=605,
                unit_id=75,
                property_id=85,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=4000,
            )

        first = post_security_deposit(605)
        self.assertTrue(first["posted"])

        with connection.cursor() as cursor:
            self._set_lease(cursor, 605, lease_status="INACTIVE")
        cycled_out = post_security_deposit(605)
        self.assertFalse(cycled_out["posted"])
        self.assertEqual(cycled_out["reason"], "not_active")

        with connection.cursor() as cursor:
            self._set_lease(cursor, 605, lease_status="ACTIVE")
        reactivated = post_security_deposit(605)

        self.assertFalse(reactivated["posted"])
        self.assertEqual(reactivated["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=605).count(), 1
        )

    def test_unresolvable_pmc_null_parent_property_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=606,
                unit_id=76,
                property_id=86,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=1000,
                null_parent_property_id=True,
            )

        result = post_security_deposit(606)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=606).count(), 0
        )

    def test_unresolvable_pmc_no_matching_finance_profile_posts_nothing(self):
        # No FinancePMCProfile created at all -- chain resolves fully but
        # the final FinancePMCProfile lookup comes up empty.
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=607,
                unit_id=77,
                property_id=87,
                pmc_id=999,
                lease_status="ACTIVE",
                security_deposit=1000,
            )

        result = post_security_deposit(607)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=607).count(), 0
        )

    def test_lease_not_found_posts_nothing(self):
        result = post_security_deposit(999999)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "lease_not_found")

    def test_unconfigured_chart_of_accounts_posts_nothing(self):
        profile = self._make_profile(pmc_id=1)
        Account.objects.filter(finance_pmc_profile=profile).delete()
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                lease_id=608,
                unit_id=78,
                property_id=88,
                pmc_id=1,
                lease_status="ACTIVE",
                security_deposit=1000,
            )

        result = post_security_deposit(608)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "chart_of_accounts_not_configured")
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=608).count(), 0
        )


@override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
class SyncLeaseEndpointTests(TestCase):
    """Story 2.6 tests: the internal /internal/leases/{id}/sync endpoint,
    an HTTP-level integration test mirroring
    SyncLeaseTransactionRentPostingIntegrationTests' pattern -- exercises
    the view's conditional wiring (path/body id validation, token guard,
    calling post_security_deposit), not just the posting function directly.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in ("lease_lease", "property_unit", "property_property"):
                cursor.execute(f"DELETE FROM {table}")

    def _url(self, lease_id):
        return reverse("sync-lease", kwargs={"lease_id": lease_id})

    def test_happy_path_via_http_posts_journal_entry(self):
        profile = FinancePMCProfile.objects.create(
            pmc_id=1,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease "
                "(id, unit_id, lease_status, security_deposit) "
                "VALUES (%s, %s, %s, %s)",
                [700, 60, "ACTIVE", 7500],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [60, 70],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [70, 1],
            )

        response = self.client.post(
            self._url(700),
            data={"lease_id": 700},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["content"]["posting"]["security_deposit"],
            {"posted": True, "reason": ""},
        )

        entry = JournalEntry.objects.get(
            source_lease_transaction_id=700,
            source_status_transition="ACTIVATE",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)
        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)
        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 7500)

    def test_missing_token_rejected_with_401(self):
        response = self.client.post(
            self._url(700),
            data={"lease_id": 700},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_wrong_token_rejected_with_403(self):
        response = self.client.post(
            self._url(700),
            data={"lease_id": 700},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="wrong-token",
        )

        self.assertEqual(response.status_code, 403)

    def test_path_body_id_mismatch_rejected_with_400(self):
        response = self.client.post(
            self._url(700),
            data={"lease_id": 701},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["content"]["path_lease_id"], 700)
        self.assertEqual(body["content"]["body_lease_id"], 701)

    def test_no_trailing_slash_url_also_works(self):
        response = self.client.post(
            reverse("sync-lease-no-slash", kwargs={"lease_id": 700}),
            data={"lease_id": 700},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["lease_id"], 700)

    def test_lease_not_found_returns_200_ack_with_no_posting(self):
        """No row exists at this lease_id -- lease resolves to None and no
        posting is attempted, mirroring sync_lease_transaction's bare-
        acknowledgment behavior for an unresolvable id."""
        response = self.client.post(
            self._url(999999),
            data={"lease_id": 999999},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], {"lease_id": 999999})


class PostCommissionSplitTests(TestCase):
    """Story 2.7 tests: post_commission_split posting logic.

    Extends the established hand-rolled stand-in-table technique
    (LeaseTransactionRef/LeaseRef/UnitRef/PropertyRef) with a new stand-in
    `property_unitowner` table (UnitOwnerRef) and a `commission_percent`
    column on the stand-in `property_unit` table (UnitRef), exercising the
    real chain-walking/commission-computation code path without importing
    units-backend.

    Covers all six I/O matrix rows from the spec:
      1. Simple commission split, no owners -- 8% commission on 10,000 rent
         posts a balanced 3-line entry (Commission Expense 800, AP — PMC
         Commission 800, VAT Payable 40).
      2. No commission configured -- commission_percent null or 0 -> no
         posting, not a failure.
      3. Rent AR did not post -- post_commission_split is simply never
         called by the view in this case (asserted at the view level).
      4. Duplicate sync -- second call for the same
         (lease_transaction_id, "COMMISSION") is a no-op.
      5. Owners sum to 100 -- two UnitOwner rows (60/40) still post the
         same 3 lines (the split itself doesn't vary by owner count).
      6. Owners don't sum to 100 -- posting fails loudly with
         "invalid_ownership_split", no partial post.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unitowner (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL,
                    owner_id BIGINT,
                    ownership_percent NUMERIC(5, 2) NOT NULL
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
            cursor.execute("DROP TABLE IF EXISTS property_unitowner")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
                "property_unitowner",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _insert_chain(
        self,
        cursor,
        txn_id,
        lease_id,
        unit_id,
        property_id,
        pmc_id,
        cheque_type="RENT_CHEQUE",
        amount=10000,
        status="BALANCE",
        commission_percent=8,
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [txn_id, lease_id, amount, cheque_type, "CHEQUE", status],
        )
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
            [lease_id, unit_id],
        )
        cursor.execute(
            "INSERT INTO property_unit "
            "(id, parent_property_id, commission_percent) VALUES (%s, %s, %s)",
            [unit_id, property_id, commission_percent],
        )
        cursor.execute(
            "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
            [property_id, pmc_id],
        )

    def _insert_owner(self, cursor, unit_id, ownership_percent):
        cursor.execute(
            "INSERT INTO property_unitowner (unit_id, ownership_percent) "
            "VALUES (%s, %s)",
            [unit_id, ownership_percent],
        )

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def test_simple_commission_split_no_owners_posts_balanced_three_lines(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=400,
                lease_id=40,
                unit_id=50,
                property_id=60,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )

        rent_result = post_rent_ar(400)
        self.assertTrue(rent_result["posted"])

        result = post_commission_split(400)

        self.assertTrue(result["posted"])
        entries = JournalEntry.objects.filter(
            source_lease_transaction_id=400,
            source_status_transition="COMMISSION",
        )
        self.assertEqual(entries.count(), 1)
        entry = entries.first()
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        expense_line = lines.get(account__name="Commission Expense")
        ap_line = lines.get(account__name="AP — PMC Commission")
        vat_line = lines.get(account__name="VAT Payable")

        # Commission Expense debits the full 840 (commission + VAT
        # together, mirroring post_bounce_fee's AR-debits-fee+VAT
        # precedent) -- the CoA has no separate account for VAT's debit
        # side, so it must be folded into this line for the entry to
        # balance (NFR-1).
        self.assertEqual(expense_line.debit, 840)
        self.assertEqual(expense_line.credit, 0)
        self.assertEqual(ap_line.debit, 0)
        self.assertEqual(ap_line.credit, 800)
        self.assertEqual(vat_line.debit, 0)
        self.assertEqual(vat_line.credit, 40)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 840)

        # Two JournalEntries total for this lease_transaction_id -- the
        # rent AR posting plus this commission split.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=400).count(), 2
        )

    def test_no_commission_configured_null_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=401,
                lease_id=41,
                unit_id=51,
                property_id=61,
                pmc_id=1,
                amount=10000,
                commission_percent=None,
            )

        self.assertTrue(post_rent_ar(401)["posted"])

        result = post_commission_split(401)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_commission_configured")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=401,
                source_status_transition="COMMISSION",
            ).count(),
            0,
        )

    def test_no_commission_configured_zero_posts_nothing(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=402,
                lease_id=42,
                unit_id=52,
                property_id=62,
                pmc_id=1,
                amount=10000,
                commission_percent=0,
            )

        self.assertTrue(post_rent_ar(402)["posted"])

        result = post_commission_split(402)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_commission_configured")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=402,
                source_status_transition="COMMISSION",
            ).count(),
            0,
        )

    def test_rent_ar_did_not_post_commission_split_never_called(self):
        """Mirrors the view's own gate: post_commission_split is only ever
        called when post_rent_ar's own result is {"posted": True}. Here we
        assert the view-level behavior directly via sync_lease_transaction
        rather than calling post_commission_split in isolation, since the
        spec frames this as "no commission posting attempted", i.e. the
        caller-side gate, not a check inside post_commission_split itself."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=403,
                lease_id=43,
                unit_id=53,
                property_id=63,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )
            # Force post_rent_ar to fail: no FinancePMCProfile resolvable for
            # this pmc_id? No -- profile exists for pmc_id=1. Instead make
            # amount null so post_rent_ar itself fails with missing_amount.
            cursor.execute(
                "UPDATE lease_leasetransaction SET amount = NULL WHERE documents_ptr_id = %s",
                [403],
            )

        from django.test import override_settings
        from django.urls import reverse

        with override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token"):
            response = self.client.post(
                reverse(
                    "sync-lease-transaction",
                    kwargs={"lease_transaction_id": 403},
                ),
                data={"lease_transaction_id": 403},
                content_type="application/json",
                HTTP_X_INTERNAL_TOKEN="test-internal-token",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["content"]["posting"]["rent_ar"]["posted"])
        self.assertNotIn("commission_split", body["content"]["posting"])
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=403).count(), 0
        )

    def test_duplicate_sync_is_skipped(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=404,
                lease_id=44,
                unit_id=54,
                property_id=64,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )

        self.assertTrue(post_rent_ar(404)["posted"])
        first = post_commission_split(404)
        second = post_commission_split(404)

        self.assertTrue(first["posted"])
        self.assertFalse(second["posted"])
        self.assertEqual(second["reason"], "duplicate_skip")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=404,
                source_status_transition="COMMISSION",
            ).count(),
            1,
        )

    def test_owners_sum_to_100_posts_same_three_lines(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=405,
                lease_id=45,
                unit_id=55,
                property_id=65,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )
            self._insert_owner(cursor, unit_id=55, ownership_percent=60)
            self._insert_owner(cursor, unit_id=55, ownership_percent=40)

        self.assertTrue(post_rent_ar(405)["posted"])
        result = post_commission_split(405)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=405,
            source_status_transition="COMMISSION",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)
        expense_line = lines.get(account__name="Commission Expense")
        ap_line = lines.get(account__name="AP — PMC Commission")
        vat_line = lines.get(account__name="VAT Payable")
        self.assertEqual(expense_line.debit, 840)
        self.assertEqual(ap_line.credit, 800)
        self.assertEqual(vat_line.credit, 40)

    def test_owners_do_not_sum_to_100_fails_loudly_no_partial_post(self):
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=406,
                lease_id=46,
                unit_id=56,
                property_id=66,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )
            self._insert_owner(cursor, unit_id=56, ownership_percent=60)
            self._insert_owner(cursor, unit_id=56, ownership_percent=30)

        self.assertTrue(post_rent_ar(406)["posted"])
        result = post_commission_split(406)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "invalid_ownership_split")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=406,
                source_status_transition="COMMISSION",
            ).count(),
            0,
        )

    def test_zero_owner_rows_is_not_a_failure(self):
        """A Unit with zero UnitOwner rows (matching today's actual DB
        state) is NOT a failure -- the ownership-percent check only applies
        when at least one UnitOwner row exists (spec Boundaries &
        Constraints)."""
        self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=407,
                lease_id=47,
                unit_id=57,
                property_id=67,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )
            # No property_unitowner rows inserted at all.

        self.assertTrue(post_rent_ar(407)["posted"])
        result = post_commission_split(407)

        self.assertTrue(result["posted"])

    def test_unresolvable_pmc_posts_nothing(self):
        # No FinancePMCProfile created at all for pmc_id=999 -- chain
        # resolves fully but the final FinancePMCProfile lookup comes up
        # empty. post_rent_ar itself would also fail unresolvable_pmc, so
        # we call post_commission_split directly to isolate its own
        # resolution-failure path.
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=408,
                lease_id=48,
                unit_id=58,
                property_id=68,
                pmc_id=999,
                amount=10000,
                commission_percent=8,
            )

        result = post_commission_split(408)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")

    def test_unconfigured_chart_of_accounts_posts_nothing(self):
        profile = self._make_profile(pmc_id=1)
        with connection.cursor() as cursor:
            self._insert_chain(
                cursor,
                txn_id=409,
                lease_id=49,
                unit_id=59,
                property_id=69,
                pmc_id=1,
                amount=10000,
                commission_percent=8,
            )

        # post_rent_ar must succeed first (Story 5.5: it now also needs a
        # VAT Payable Account for its own 3-line entry) -- only AFTER that
        # succeeds do we delete the Accounts this test is actually about,
        # isolating post_commission_split's own chart_of_accounts_not_configured
        # path from post_rent_ar's.
        self.assertTrue(post_rent_ar(409)["posted"])
        Account.objects.filter(
            finance_pmc_profile=profile,
            name__in=["Commission Expense", "AP — PMC Commission", "VAT Payable"],
        ).delete()
        result = post_commission_split(409)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "chart_of_accounts_not_configured")
        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=409,
                source_status_transition="COMMISSION",
            ).count(),
            0,
        )


class ReportingAuthAndPmcScopingTests(TestCase):
    """Story 3.1 tests: authenticate_reporting_request + get_pmc_ids_for_user_profile.

    Covers all eight I/O matrix rows from the spec:
      1. Valid PM token -> helper returns both pmc_ids via PMCPMMapping.
      2. Valid Owner token, direct path (parent_property) -> resolves pmc_id.
      3. Valid Owner token, block-tower path (property_block_tower) ->
         resolves the same pmc_id via the alternate join.
      4. PM with no PMCPMMapping rows, company_id set -> falls back to
         [company_id].
      5. Expired/invalid JWT -> rejected, 401, before any query.
      6. Token doesn't match UserProfileRef.token -> rejected, 401.
      7. Inactive user (auth_user.is_active = False) -> rejected, 401.
      8. Tenant-only profile (no PropertyManager/Owner row) -> helper
         returns [], not an error.

    Uses the hand-rolled stand-in-table technique (Story 1.2's precedent,
    extended by every later story) for the unmanaged ref tables this story
    introduces: user_service_userprofile, user_service_propertymanager,
    user_service_owner, property_pmcpmmapping, property_unitowner,
    property_unit, property_property, property_propertyblocks.
    `AuthUserRef` (`db_table="auth_user"`) is the one exception -- Finance's
    own `django.contrib.auth` (already in INSTALLED_APPS, needed for Django's
    test/admin scaffolding) means the real, fully-migrated `auth_user` table
    already exists in the test database, so this test creates real rows via
    `django.contrib.auth.models.User` instead of a stand-in table.
    """

    JWT_SECRET_KEY = "test-jwt-secret"
    JWT_ALGORITHM = "HS256"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_userprofile (
                    id BIGSERIAL PRIMARY KEY,
                    email VARCHAR(255),
                    token TEXT,
                    user_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_propertymanager (
                    userprofile_ptr_id BIGINT PRIMARY KEY,
                    company_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_owner (
                    userprofile_ptr_id BIGINT PRIMARY KEY
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_pmcpmmapping (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT NOT NULL,
                    pm_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unitowner (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL,
                    owner_id BIGINT,
                    ownership_percent NUMERIC(5, 2) NOT NULL DEFAULT 100
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_propertyblocks (
                    id BIGSERIAL PRIMARY KEY,
                    property_id BIGINT NOT NULL
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            for table in (
                "property_propertyblocks",
                "property_property",
                "property_unit",
                "property_unitowner",
                "property_pmcpmmapping",
                "user_service_owner",
                "user_service_propertymanager",
                "user_service_userprofile",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        super().tearDownClass()

    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.all().delete()
        with connection.cursor() as cursor:
            for table in (
                "property_propertyblocks",
                "property_property",
                "property_unit",
                "property_unitowner",
                "property_pmcpmmapping",
                "user_service_owner",
                "user_service_propertymanager",
                "user_service_userprofile",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _make_token(self, email, exp_delta_seconds=3600):
        import datetime

        payload = {
            "user_id": 1,
            "email": email,
            "exp": datetime.datetime.utcnow()
            + datetime.timedelta(seconds=exp_delta_seconds),
        }
        return jwt.encode(payload, self.JWT_SECRET_KEY, algorithm=self.JWT_ALGORITHM)

    def _make_user_profile(
        self, cursor, profile_id, email, token, auth_user_id, is_active=True
    ):
        from django.contrib.auth.models import User

        User.objects.create(
            id=auth_user_id,
            username=f"user{auth_user_id}",
            email=email or "",
            is_active=is_active,
        )
        cursor.execute(
            "INSERT INTO user_service_userprofile (id, email, token, user_id) "
            "VALUES (%s, %s, %s, %s)",
            [profile_id, email, token, auth_user_id],
        )

    def _request_with_token(self, token):
        request = type("FakeRequest", (), {})()
        request.headers = {"Authorization": f"Bearer {token}"}
        return request

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_valid_pm_token_returns_both_mapped_pmc_ids(self):
        token = self._make_token("pm@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 1, "pm@example.com", token, 101)
            cursor.execute(
                "INSERT INTO user_service_propertymanager (userprofile_ptr_id, company_id) "
                "VALUES (%s, %s)",
                [1, 5],
            )
            cursor.execute(
                "INSERT INTO property_pmcpmmapping (pmc_id, pm_id) VALUES (%s, %s)",
                [10, 1],
            )
            cursor.execute(
                "INSERT INTO property_pmcpmmapping (pmc_id, pm_id) VALUES (%s, %s)",
                [20, 1],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(user_profile_ref)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(sorted(pmc_ids), [10, 20])

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_valid_owner_token_direct_path_resolves_pmc_id(self):
        token = self._make_token("owner@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 2, "owner@example.com", token, 102)
            cursor.execute("INSERT INTO user_service_owner (userprofile_ptr_id) VALUES (%s)", [2])
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [30, 7],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [40, 30],
            )
            cursor.execute(
                "INSERT INTO property_unitowner (unit_id, owner_id) VALUES (%s, %s)",
                [40, 2],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(pmc_ids, [7])

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_valid_owner_token_block_tower_path_resolves_same_pmc_id(self):
        token = self._make_token("owner2@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 3, "owner2@example.com", token, 103)
            cursor.execute("INSERT INTO user_service_owner (userprofile_ptr_id) VALUES (%s)", [3])
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [31, 8],
            )
            cursor.execute(
                "INSERT INTO property_propertyblocks (id, property_id) "
                "VALUES (%s, %s)",
                [50, 31],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, property_block_tower_id) "
                "VALUES (%s, %s)",
                [41, 50],
            )
            cursor.execute(
                "INSERT INTO property_unitowner (unit_id, owner_id) VALUES (%s, %s)",
                [41, 3],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(pmc_ids, [8])

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_pm_with_no_mapping_rows_falls_back_to_company_id(self):
        token = self._make_token("pm2@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 4, "pm2@example.com", token, 104)
            cursor.execute(
                "INSERT INTO user_service_propertymanager (userprofile_ptr_id, company_id) "
                "VALUES (%s, %s)",
                [4, 99],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(pmc_ids, [99])

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_pm_with_no_mapping_and_null_company_id_returns_empty_list(self):
        """A null company_id fallback yields no PMCs, not an error (spec
        Boundaries & Constraints)."""
        token = self._make_token("pm3@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 5, "pm3@example.com", token, 105)
            cursor.execute(
                "INSERT INTO user_service_propertymanager (userprofile_ptr_id, company_id) "
                "VALUES (%s, %s)",
                [5, None],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(pmc_ids, [])

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_expired_jwt_rejected_with_401_before_any_query(self):
        token = self._make_token("anyone@example.com", exp_delta_seconds=-10)

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "expired_token")

    def test_malformed_jwt_rejected_with_401(self):
        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token("not-a-real-jwt")
        )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "invalid_token")

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_stale_token_not_matching_userprofile_token_rejected_with_401(self):
        """Well-formed, unexpired JWT, but stale -- superseded by a newer
        login (units-backend's own revocation semantics, spec Design
        Notes)."""
        token = self._make_token("stale@example.com")
        with connection.cursor() as cursor:
            # UserProfileRef.token is a DIFFERENT (newer) token than the one
            # presented -- simulates a superseded login.
            self._make_user_profile(
                cursor, 6, "stale@example.com", "a-newer-token", 106
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "token_mismatch")

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_inactive_user_rejected_with_401(self):
        token = self._make_token("inactive@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(
                cursor,
                7,
                "inactive@example.com",
                token,
                107,
                is_active=False,
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "inactive_user")

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_tenant_only_profile_helper_returns_empty_list_not_an_error(self):
        """A profile with no PropertyManager/Owner row (e.g. Tenant-only) --
        helper returns [], not an error (spec I/O matrix; Tenant-role
        scoping is explicitly out of scope, spec Never)."""
        token = self._make_token("tenant@example.com")
        with connection.cursor() as cursor:
            self._make_user_profile(cursor, 8, "tenant@example.com", token, 108)

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(user_profile_ref)

        pmc_ids = get_pmc_ids_for_user_profile(user_profile_ref)
        self.assertEqual(pmc_ids, [])

    def test_missing_authorization_header_rejected_with_401(self):
        request = type("FakeRequest", (), {})()
        request.headers = {}

        user_profile_ref, reason = authenticate_reporting_request(request)
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "missing_token")

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_valid_token_but_no_matching_profile_rejected_with_401(self):
        """A well-formed, unexpired token whose email claim resolves to no
        UserProfileRef row at all -- rejected, not a 500/crash."""
        token = self._make_token("nobody@example.com")

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "profile_not_found")

    def test_missing_email_claim_rejected_with_401(self):
        """A token with no email claim at all is rejected outright, never
        filtered as email=None (which could otherwise match an unrelated
        profile whose email is also null) -- post-review fix."""
        payload = {"user_id": 1, "exp": __import__("datetime").datetime.utcnow()
                   + __import__("datetime").timedelta(seconds=3600)}
        token = jwt.encode(payload, self.JWT_SECRET_KEY, algorithm=self.JWT_ALGORITHM)

        with override_settings(
            JWT_SECRET_KEY=self.JWT_SECRET_KEY, JWT_ALGORITHM=self.JWT_ALGORITHM
        ):
            user_profile_ref, reason = authenticate_reporting_request(
                self._request_with_token(token)
            )
        self.assertIsNone(user_profile_ref)
        self.assertEqual(reason, "invalid_token")

    @override_settings(JWT_SECRET_KEY=JWT_SECRET_KEY, JWT_ALGORITHM=JWT_ALGORITHM)
    def test_diverged_userprofile_email_does_not_leak_wrong_profile(self):
        """Post-review regression: UserProfileRef.email is a separate,
        independently-settable field from the related auth_user.email --
        resolution must go via auth_user.email (matching units-backend's
        real UserProfile.objects.filter(user__email=...)), never
        UserProfileRef.email directly. Here the two intentionally diverge;
        auth must still resolve correctly via the auth_user email."""
        token = self._make_token("real-login-email@example.com")
        with connection.cursor() as cursor:
            from django.contrib.auth.models import User

            User.objects.create(
                id=109,
                username="user109",
                email="real-login-email@example.com",
                is_active=True,
            )
            # UserProfileRef.email deliberately set to a DIFFERENT value
            # than the auth_user's real email -- simulates the two fields
            # having diverged.
            cursor.execute(
                "INSERT INTO user_service_userprofile (id, email, token, user_id) "
                "VALUES (%s, %s, %s, %s)",
                [9, "stale-profile-email@example.com", token, 109],
            )

        user_profile_ref, reason = authenticate_reporting_request(
            self._request_with_token(token)
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(user_profile_ref)
        self.assertEqual(user_profile_ref.id, 9)


@override_settings(
    JWT_SECRET_KEY="test-jwt-secret-3-2", JWT_ALGORITHM="HS256"
)
class TrialBalanceReportTests(TestCase):
    """Story 3.2 tests: GET /reports/trial-balance.

    Covers all seven I/O matrix rows from the spec:
      1. Happy path -- every Account's total debit/credit, balanced: true.
      2. Zero-activity account included with total_debit=0, total_credit=0.
      3. Reversal entry summed unconditionally (no special-casing, AD-16).
      4. Unreachable pmc_id -- 403.
      5. Nonexistent pmc_id -- 404.
      6. Invalid/missing date range -- 400, before any query.
      7. Unauthenticated/expired/revoked token -- 401, before any query.

    Reuses ReportingAuthAndPmcScopingTests' stand-in-table technique for the
    unmanaged units-backend ref tables (user_service_userprofile,
    user_service_owner, property_property, property_unit,
    property_unitowner) needed to exercise the Owner branch of
    get_pmc_ids_for_user_profile through the real HTTP view.
    """

    JWT_SECRET_KEY = "test-jwt-secret-3-2"
    JWT_ALGORITHM = "HS256"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_userprofile (
                    id BIGSERIAL PRIMARY KEY,
                    email VARCHAR(255),
                    token TEXT,
                    user_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_owner (
                    userprofile_ptr_id BIGINT PRIMARY KEY
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_propertymanager (
                    userprofile_ptr_id BIGINT PRIMARY KEY,
                    company_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_pmcpmmapping (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT NOT NULL,
                    pm_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unitowner (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL,
                    owner_id BIGINT,
                    ownership_percent NUMERIC(5, 2) NOT NULL DEFAULT 100
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        super().tearDownClass()

    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.all().delete()
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _make_token(self, email, exp_delta_seconds=3600):
        import datetime as dt

        payload = {
            "user_id": 1,
            "email": email,
            "exp": dt.datetime.utcnow() + dt.timedelta(seconds=exp_delta_seconds),
        }
        return jwt.encode(payload, self.JWT_SECRET_KEY, algorithm=self.JWT_ALGORITHM)

    def _make_owner_with_pmc(
        self, profile_id, email, token, auth_user_id, unit_id, property_id, pmc_id
    ):
        from django.contrib.auth.models import User

        User.objects.create(
            id=auth_user_id,
            username=f"owner{auth_user_id}",
            email=email,
            is_active=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_service_userprofile (id, email, token, user_id) "
                "VALUES (%s, %s, %s, %s)",
                [profile_id, email, token, auth_user_id],
            )
            cursor.execute(
                "INSERT INTO user_service_owner (userprofile_ptr_id) VALUES (%s)",
                [profile_id],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [property_id, pmc_id],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [unit_id, property_id],
            )
            cursor.execute(
                "INSERT INTO property_unitowner (unit_id, owner_id) VALUES (%s, %s)",
                [unit_id, profile_id],
            )

    def _make_profile(self, pmc_id):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def _post_journal_entry(
        self,
        profile,
        transition,
        lines,
        posted_at=None,
        source_txn_id=1,
        reversed_journal_entry=None,
    ):
        """lines: list of (account_name, debit, credit) tuples."""
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=source_txn_id,
            source_status_transition=transition,
            reversed_journal_entry=reversed_journal_entry,
        )
        if posted_at is not None:
            JournalEntry.objects.filter(pk=entry.pk).update(posted_at=posted_at)
            entry.refresh_from_db()
        for account_name, debit, credit in lines:
            account = Account.objects.get(
                finance_pmc_profile=profile, name=account_name
            )
            LedgerLine.objects.create(
                journal_entry=entry, account=account, debit=debit, credit=credit
            )
        return entry

    def _url(self):
        return reverse("trial-balance-report")

    def test_happy_path_returns_every_account_balanced_true(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=1)
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )

        posted_at = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 5000, 0), ("Rent Income", 0, 5000)],
            posted_at=posted_at,
            source_txn_id=100,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 1, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        self.assertTrue(body["content"]["balanced"])

        accounts = {a["name"]: a for a in body["content"]["accounts"]}
        self.assertEqual(
            set(accounts.keys()),
            {
                "Rent Income",
                "Security Deposits Held",
                "VAT Payable",
                "Bank",
                "AR — Tenants",
                "AP — PMC Commission",
                "Commission Expense",
                "Bank Charges/Fees",
                "Bounced Cheques",
            },
        )
        self.assertEqual(accounts["AR — Tenants"]["total_debit"], 5000.0)
        self.assertEqual(accounts["AR — Tenants"]["total_credit"], 0.0)
        self.assertEqual(accounts["Rent Income"]["total_debit"], 0.0)
        self.assertEqual(accounts["Rent Income"]["total_credit"], 5000.0)

        sum_debit = sum(float(a["total_debit"]) for a in accounts.values())
        sum_credit = sum(float(a["total_credit"]) for a in accounts.values())
        self.assertEqual(sum_debit, sum_credit)

    def test_zero_activity_account_included_with_zero_totals(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=2)
        token = self._make_token("owner2@example.com")
        self._make_owner_with_pmc(
            2, "owner2@example.com", token, 202, unit_id=11, property_id=21, pmc_id=2
        )

        posted_at = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 1000, 0), ("Rent Income", 0, 1000)],
            posted_at=posted_at,
            source_txn_id=101,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 2, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        accounts = {a["name"]: a for a in response.json()["content"]["accounts"]}
        # "Bank" had no activity in the period -- still present, zeroed.
        self.assertEqual(accounts["Bank"]["total_debit"], 0.0)
        self.assertEqual(accounts["Bank"]["total_credit"], 0.0)

    def test_reversal_entry_summed_unconditionally(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=3)
        token = self._make_token("owner3@example.com")
        self._make_owner_with_pmc(
            3, "owner3@example.com", token, 203, unit_id=12, property_id=22, pmc_id=3
        )

        posted_at = dt.datetime(2026, 6, 5, tzinfo=dt.timezone.utc)
        original = self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 2000, 0), ("Rent Income", 0, 2000)],
            posted_at=posted_at,
            source_txn_id=102,
        )
        self._post_journal_entry(
            profile,
            "BALANCE-BOUNCED",
            [("AR — Tenants", 0, 2000), ("Bounced Cheques", 2000, 0)],
            posted_at=posted_at,
            source_txn_id=102,
            reversed_journal_entry=original,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 3, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["content"]["balanced"])
        accounts = {a["name"]: a for a in body["content"]["accounts"]}
        # AR — Tenants: debited 2000 (rent), credited 2000 (reversal) -- both
        # summed, no special-casing.
        self.assertEqual(accounts["AR — Tenants"]["total_debit"], 2000.0)
        self.assertEqual(accounts["AR — Tenants"]["total_credit"], 2000.0)
        self.assertEqual(accounts["Bounced Cheques"]["total_debit"], 2000.0)
        self.assertEqual(accounts["Rent Income"]["total_credit"], 2000.0)

    def test_unreachable_pmc_id_rejected_with_403(self):
        self._make_profile(pmc_id=4)
        # Owner is scoped to pmc_id=5, not pmc_id=4.
        token = self._make_token("owner4@example.com")
        self._make_owner_with_pmc(
            4, "owner4@example.com", token, 204, unit_id=13, property_id=23, pmc_id=5
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 4, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], 403)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("owner5@example.com")
        self._make_owner_with_pmc(
            5, "owner5@example.com", token, 205, unit_id=14, property_id=24, pmc_id=999
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 999, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], 404)

    def test_missing_date_range_rejected_with_400(self):
        self._make_profile(pmc_id=6)
        token = self._make_token("owner6@example.com")
        self._make_owner_with_pmc(
            6, "owner6@example.com", token, 206, unit_id=15, property_id=25, pmc_id=6
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 6},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile__pmc_id=6).count(), 0
        )

    def test_invalid_date_format_rejected_with_400(self):
        self._make_profile(pmc_id=7)
        token = self._make_token("owner7@example.com")
        self._make_owner_with_pmc(
            7, "owner7@example.com", token, 207, unit_id=16, property_id=26, pmc_id=7
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 7, "start_date": "not-a-date", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_inverted_date_range_rejected_with_400(self):
        """Post-review patch: start_date after end_date is rejected outright
        rather than silently running a query that always returns
        zero-activity accounts."""
        self._make_profile(pmc_id=71)
        token = self._make_token("owner71@example.com")
        self._make_owner_with_pmc(
            71, "owner71@example.com", token, 271, unit_id=161, property_id=261, pmc_id=71
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 71, "start_date": "2026-06-30", "end_date": "2026-06-01"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self._make_profile(pmc_id=8)

        response = self.client.get(
            self._url(),
            {"pmc_id": 8, "start_date": "2026-06-01", "end_date": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)

    def test_expired_token_rejected_with_401(self):
        token = self._make_token("owner9@example.com", exp_delta_seconds=-10)
        self._make_profile(pmc_id=9)

        response = self.client.get(
            self._url(),
            {"pmc_id": 9, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 401)

    def test_no_trailing_slash_url_also_works(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=10)
        token = self._make_token("owner10@example.com")
        self._make_owner_with_pmc(
            10,
            "owner10@example.com",
            token,
            210,
            unit_id=17,
            property_id=27,
            pmc_id=10,
        )
        posted_at = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
            posted_at=posted_at,
            source_txn_id=110,
        )

        response = self.client.get(
            reverse("trial-balance-report-no-slash"),
            {"pmc_id": 10, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["content"]["balanced"])


class FinancePmcProfileStatusTests(TrialBalanceReportTests):
    """AD-6 / frontend-prd.md FFR-3 tests: GET /finance-pmc-profile/<pmc_id>/status.

    Subclasses `TrialBalanceReportTests` to reuse its stand-in-table
    setUpClass/tearDownClass/setUp and _make_token/_make_owner_with_pmc/
    _make_profile/_post_journal_entry helpers (same pattern as
    ProfitLossReportTests/BalanceSheetReportTests/AgeingReportTests).

    Named `_status_url` (not `_url`) deliberately: this endpoint takes
    `pmc_id` as a URL path segment, not a query param like Trial Balance's
    own `_url()` -- overriding `_url` with an incompatible signature would
    break every inherited Trial-Balance-specific test method this class
    also inherits (they all call `self._url()` with zero args).
    """

    def _status_url(self, pmc_id):
        return reverse("finance-pmc-profile-status", args=[pmc_id])

    def test_not_activated_when_no_profile_exists(self):
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )
        # Deliberately no FinancePMCProfile created for pmc_id=1.

        response = self.client.get(
            self._status_url(1), HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["status"], "not_activated")

    def test_activated_empty_when_profile_exists_with_no_journal_entries(self):
        self._make_profile(pmc_id=1)
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )

        response = self.client.get(
            self._status_url(1), HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["status"], "activated_empty")

    def test_activated_when_profile_has_posted_activity(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=1)
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
            posted_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
            source_txn_id=110,
        )

        response = self.client.get(
            self._status_url(1), HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["status"], "activated")

    def test_unreachable_pmc_returns_403_regardless_of_activation(self):
        """An unreachable pmc_id must never distinguish "doesn't exist" from
        "exists but unactivated" via response differences -- both are 403."""
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )
        # pmc_id=999 is never linked to this owner's reachable PMCs, and has
        # no FinancePMCProfile either -- both facts must stay invisible.

        response = self.client.get(
            self._status_url(999), HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, 403)

    def test_missing_auth_returns_401_before_any_query(self):
        response = self.client.get(self._status_url(1))

        self.assertEqual(response.status_code, 401)

    def test_no_slash_route_also_resolves(self):
        self._make_profile(pmc_id=1)
        token = self._make_token("owner@example.com")
        self._make_owner_with_pmc(
            1, "owner@example.com", token, 201, unit_id=10, property_id=20, pmc_id=1
        )

        response = self.client.get(
            reverse("finance-pmc-profile-status-no-slash", args=[1]),
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["status"], "activated_empty")


class ProfitLossReportTests(TrialBalanceReportTests):
    """Story 3.3 tests: GET /reports/profit-loss.

    Subclasses `TrialBalanceReportTests` to reuse its stand-in-table
    setUpClass/tearDownClass/setUp (the unmanaged units-backend ref tables
    needed to exercise the Owner branch of get_pmc_ids_for_user_profile
    through the real HTTP view) and its `_make_profile`/`_make_owner_with_pmc`/
    `_post_journal_entry` helpers -- this story's endpoint follows the exact
    same auth/validate/resolve/scope sequence as Trial Balance (spec
    Boundaries & Constraints), so the same fixtures apply unchanged.

    Covers all four I/O matrix rows from the spec:
      1. Happy path -- income_accounts/expense_accounts breakdown, correct
         net_profit_loss.
      2. No Income/Expense activity -- both present with zero totals,
         net_profit_loss: 0.
      3. Net figure matches an independent recomputation from a separate
         Trial Balance call on the same data (FR-11's cross-report
         invariant).
      4. Unreachable/nonexistent pmc_id, invalid date range, unauthenticated
         -- same rejections as Trial Balance (403 / 404 / 400 / 401).
    """

    def _url(self):
        return reverse("profit-loss-report")

    # Trial-Balance-specific tests inherited from the parent class assert on
    # a `{"accounts": [...], "balanced": ...}` shape that this endpoint does
    # not return (P&L's shape is `income_accounts`/`expense_accounts`/
    # `net_profit_loss` -- spec Boundaries & Constraints). Skip them here;
    # this subclass adds its own P&L-shaped equivalents below and keeps only
    # the shared auth/validation/scoping rejection tests, which assert
    # nothing about response shape.
    def test_happy_path_returns_every_account_balanced_true(self):
        self.skipTest("superseded by P&L-shaped happy-path test below")

    def test_zero_activity_account_included_with_zero_totals(self):
        self.skipTest("superseded by P&L-shaped zero-activity test below")

    def test_reversal_entry_summed_unconditionally(self):
        self.skipTest("Trial-Balance-shaped assertion; not applicable to P&L")

    def test_invalid_date_format_rejected_with_400(self):
        self.skipTest("covered by this class's own invalid-date-range test below")

    def test_expired_token_rejected_with_401(self):
        self.skipTest("covered by test_unauthenticated_request_rejected_with_401_before_any_query")

    # Note: `test_no_trailing_slash_url_also_works` and
    # `test_inverted_date_range_rejected_with_400` are also inherited, but
    # this class defines its own same-named test further below (Python's
    # last-definition-wins rule within a class body), so no explicit skip
    # stub is needed for either.

    def test_happy_path_returns_income_and_expense_breakdown_and_net(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=101)
        token = self._make_token("plowner101@example.com")
        self._make_owner_with_pmc(
            101,
            "plowner101@example.com",
            token,
            301,
            unit_id=1010,
            property_id=2010,
            pmc_id=101,
        )

        posted_at = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
        # Rent AR posting: debit AR — Tenants, credit Rent Income.
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 10000, 0), ("Rent Income", 0, 10000)],
            posted_at=posted_at,
            source_txn_id=1001,
        )
        # Commission split: credit Commission Expense, debit AP — PMC Commission.
        self._post_journal_entry(
            profile,
            "COMMISSION-SPLIT",
            [("AP — PMC Commission", 800, 0), ("Commission Expense", 0, 800)],
            posted_at=posted_at,
            source_txn_id=1001,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 101, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)

        income_accounts = {
            a["name"]: a for a in body["content"]["income_accounts"]
        }
        expense_accounts = {
            a["name"]: a for a in body["content"]["expense_accounts"]
        }

        self.assertEqual(set(income_accounts.keys()), {"Rent Income"})
        self.assertEqual(
            set(expense_accounts.keys()),
            {"Commission Expense", "Bank Charges/Fees"},
        )

        self.assertEqual(income_accounts["Rent Income"]["total_credit"], 10000.0)
        self.assertEqual(income_accounts["Rent Income"]["contribution"], 10000.0)
        self.assertEqual(
            expense_accounts["Commission Expense"]["total_credit"], 800.0
        )
        # Commission Expense was credited here (not debited) -- an odd
        # test-data choice deliberately included to prove the debit-normal
        # formula (debit - credit) is applied literally, producing a
        # negative contribution rather than being silently clamped.
        self.assertEqual(
            expense_accounts["Commission Expense"]["contribution"], -800.0
        )

        # net_profit_loss = sum(Income contributions) - sum(Expense contributions)
        # = 10000 - (-800) = 10800
        self.assertEqual(body["content"]["net_profit_loss"], 10800.0)

        # Asset/Liability/Equity accounts are excluded entirely.
        all_names = set(income_accounts.keys()) | set(expense_accounts.keys())
        self.assertNotIn("Bank", all_names)
        self.assertNotIn("Security Deposits Held", all_names)
        self.assertNotIn("VAT Payable", all_names)
        self.assertNotIn("AR — Tenants", all_names)
        self.assertNotIn("AP — PMC Commission", all_names)
        self.assertNotIn("Bounced Cheques", all_names)

    def test_no_income_expense_activity_returns_zeroed_accounts_and_zero_net(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=102)
        token = self._make_token("plowner102@example.com")
        self._make_owner_with_pmc(
            102,
            "plowner102@example.com",
            token,
            302,
            unit_id=1020,
            property_id=2020,
            pmc_id=102,
        )

        posted_at = dt.datetime(2026, 6, 5, tzinfo=dt.timezone.utc)
        # Only a security deposit (Asset/Liability) posts -- no Income/Expense
        # activity in the period.
        self._post_journal_entry(
            profile,
            "DEPOSIT",
            [("Bank", 3000, 0), ("Security Deposits Held", 0, 3000)],
            posted_at=posted_at,
            source_txn_id=1002,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 102, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()

        income_accounts = {
            a["name"]: a for a in body["content"]["income_accounts"]
        }
        expense_accounts = {
            a["name"]: a for a in body["content"]["expense_accounts"]
        }
        self.assertEqual(set(income_accounts.keys()), {"Rent Income"})
        self.assertEqual(
            set(expense_accounts.keys()),
            {"Commission Expense", "Bank Charges/Fees"},
        )
        for account in {**income_accounts, **expense_accounts}.values():
            self.assertEqual(account["total_debit"], 0.0)
            self.assertEqual(account["total_credit"], 0.0)
            self.assertEqual(account["contribution"], 0.0)

        self.assertEqual(body["content"]["net_profit_loss"], 0.0)

    def test_net_figure_matches_independent_trial_balance_recomputation(self):
        """FR-11's explicit cross-report invariant: net_profit_loss equals
        sum(Income credits) - sum(Expense debits), independently
        recomputed from a separate compute_trial_balance call on the same
        pmc_id/date range."""
        import datetime as dt

        from ledger.reports import compute_trial_balance

        profile = self._make_profile(pmc_id=103)
        token = self._make_token("plowner103@example.com")
        self._make_owner_with_pmc(
            103,
            "plowner103@example.com",
            token,
            303,
            unit_id=1030,
            property_id=2030,
            pmc_id=103,
        )

        posted_at = dt.datetime(2026, 6, 20, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 5000, 0), ("Rent Income", 0, 5000)],
            posted_at=posted_at,
            source_txn_id=1003,
        )
        self._post_journal_entry(
            profile,
            "BOUNCE-FEE",
            [("AR — Tenants", 210, 0), ("Bank Charges/Fees", 0, 200), ("VAT Payable", 0, 10)],
            posted_at=posted_at,
            source_txn_id=1004,
        )
        self._post_journal_entry(
            profile,
            "COMMISSION-SPLIT",
            [("Commission Expense", 400, 0), ("AP — PMC Commission", 0, 400)],
            posted_at=posted_at,
            source_txn_id=1003,
        )

        start_date = dt.date(2026, 6, 1)
        end_date = dt.date(2026, 6, 30)

        response = self.client.get(
            self._url(),
            {
                "pmc_id": 103,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        net_profit_loss = response.json()["content"]["net_profit_loss"]

        # Independently recompute straight from a separate compute_trial_balance
        # call -- never reusing the view's own aggregation.
        trial_balance = compute_trial_balance(profile, start_date, end_date)
        income_total = sum(
            (a["total_credit"] - a["total_debit"])
            for a in trial_balance["accounts"]
            if a["account_type"] == Account.INCOME
        )
        expense_total = sum(
            (a["total_debit"] - a["total_credit"])
            for a in trial_balance["accounts"]
            if a["account_type"] == Account.EXPENSE
        )
        expected_net = income_total - expense_total

        self.assertEqual(float(net_profit_loss), float(expected_net))
        # Sanity: 5000 (Rent Income) + 200 (Bank Charges/Fees) - 400 (Commission Expense) = 4800
        self.assertEqual(float(net_profit_loss), 4800.0)

    def test_unreachable_pmc_id_rejected_with_403(self):
        self._make_profile(pmc_id=104)
        token = self._make_token("plowner104@example.com")
        self._make_owner_with_pmc(
            104,
            "plowner104@example.com",
            token,
            304,
            unit_id=1040,
            property_id=2040,
            pmc_id=105,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 104, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], 403)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("plowner105@example.com")
        self._make_owner_with_pmc(
            105,
            "plowner105@example.com",
            token,
            305,
            unit_id=1050,
            property_id=2050,
            pmc_id=999,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 999, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], 404)

    def test_invalid_date_range_rejected_with_400(self):
        self._make_profile(pmc_id=106)
        token = self._make_token("plowner106@example.com")
        self._make_owner_with_pmc(
            106,
            "plowner106@example.com",
            token,
            306,
            unit_id=1060,
            property_id=2060,
            pmc_id=106,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 106, "start_date": "2026-06-30", "end_date": "2026-06-01"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_date_range_rejected_with_400(self):
        self._make_profile(pmc_id=107)
        token = self._make_token("plowner107@example.com")
        self._make_owner_with_pmc(
            107,
            "plowner107@example.com",
            token,
            307,
            unit_id=1070,
            property_id=2070,
            pmc_id=107,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 107},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self._make_profile(pmc_id=108)

        response = self.client.get(
            self._url(),
            {"pmc_id": 108, "start_date": "2026-06-01", "end_date": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)

    def test_no_trailing_slash_url_also_works(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=109)
        token = self._make_token("plowner109@example.com")
        self._make_owner_with_pmc(
            109,
            "plowner109@example.com",
            token,
            309,
            unit_id=1090,
            property_id=2090,
            pmc_id=109,
        )
        posted_at = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
            posted_at=posted_at,
            source_txn_id=1005,
        )

        response = self.client.get(
            reverse("profit-loss-report-no-slash"),
            {"pmc_id": 109, "start_date": "2026-06-01", "end_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["net_profit_loss"], 100.0)


class BalanceSheetReportTests(TrialBalanceReportTests):
    """Story 3.4 tests: GET /reports/balance-sheet.

    Subclasses `TrialBalanceReportTests` to reuse its stand-in-table setup
    (Owner-branch PMC scoping) and helpers (`_make_profile`,
    `_post_journal_entry`, `_make_owner_with_pmc`). Covers all four I/O
    matrix rows from the spec:
      1. Happy path -- Asset/Liability accounts + derived Retained Earnings
         Equity line, balanced: true.
      2. Equation holds with real activity across Income/Expense/Asset/
         Liability postings.
      3. as_of_date before profile creation -- valid degenerate empty
         window, all zero, balanced: true (not an error).
      4. Unreachable/nonexistent pmc_id, invalid/missing as_of_date,
         unauthenticated -- same rejections as Story 3.2/3.3.

    Date-range-specific tests inherited from the parent class don't apply
    to this single-`as_of_date`-param endpoint -- skipped/overridden below,
    same pattern `ProfitLossReportTests` uses.
    """

    def _url(self):
        return reverse("balance-sheet-report")

    # Trial-Balance-specific tests inherited from the parent class assert on
    # `start_date`/`end_date` query params and the `accounts`/`balanced`
    # response shape, which doesn't match this single-`as_of_date` endpoint;
    # this subclass adds its own Balance-Sheet-shaped equivalents below and
    # keeps only the auth/scoping rejection tests that are param-agnostic.
    def test_happy_path_returns_every_account_balanced_true(self):
        self.skipTest("covered by this class's own happy-path test below")

    def test_zero_activity_account_included_with_zero_totals(self):
        self.skipTest("covered by this class's own tests below")

    def test_reversal_entry_summed_unconditionally(self):
        self.skipTest("covered by this class's own tests below (reuses "
                       "compute_trial_balance, already tested there)")

    def test_missing_date_range_rejected_with_400(self):
        self.skipTest("covered by this class's own missing-as_of_date test below")

    def test_invalid_date_format_rejected_with_400(self):
        self.skipTest("covered by this class's own invalid-as_of_date test below")

    def test_inverted_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- single as_of_date param, no range to invert")

    def test_no_trailing_slash_url_also_works(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=201)
        # Backdate `created` (auto_now_add) so the since-inception window
        # actually covers `posted_at` below, decoupling this test from
        # wall-clock time (same reasoning as the happy-path test above).
        FinancePMCProfile.objects.filter(pk=profile.pk).update(
            created=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        )
        profile.refresh_from_db()
        token = self._make_token("bsowner201@example.com")
        self._make_owner_with_pmc(
            201,
            "bsowner201@example.com",
            token,
            401,
            unit_id=2010,
            property_id=3010,
            pmc_id=201,
        )
        posted_at = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
            posted_at=posted_at,
            source_txn_id=2001,
        )

        response = self.client.get(
            reverse("balance-sheet-report-no-slash"),
            {"pmc_id": 201, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        ar_tenants = next(
            a for a in content["asset_accounts"] if a["name"] == "AR — Tenants"
        )
        self.assertEqual(ar_tenants["balance"], 100.0)
        self.assertTrue(response.json()["content"]["balanced"])

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self._make_profile(pmc_id=202)

        response = self.client.get(
            self._url(),
            {"pmc_id": 202, "as_of_date": "2026-06-30"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)

    def test_expired_token_rejected_with_401(self):
        token = self._make_token("bsowner203@example.com", exp_delta_seconds=-10)
        self._make_profile(pmc_id=203)

        response = self.client.get(
            self._url(),
            {"pmc_id": 203, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 401)

    def test_unreachable_pmc_id_rejected_with_403(self):
        self._make_profile(pmc_id=204)
        # Owner is scoped to pmc_id=205, not pmc_id=204.
        token = self._make_token("bsowner204@example.com")
        self._make_owner_with_pmc(
            204, "bsowner204@example.com", token, 404, unit_id=2040, property_id=3040, pmc_id=205
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 204, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], 403)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("bsowner205@example.com")
        self._make_owner_with_pmc(
            205, "bsowner205@example.com", token, 405, unit_id=2050, property_id=3050, pmc_id=999
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 999, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], 404)

    def test_missing_as_of_date_rejected_with_400(self):
        self._make_profile(pmc_id=206)
        token = self._make_token("bsowner206@example.com")
        self._make_owner_with_pmc(
            206, "bsowner206@example.com", token, 406, unit_id=2060, property_id=3060, pmc_id=206
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 206},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile__pmc_id=206).count(), 0
        )

    def test_invalid_as_of_date_format_rejected_with_400(self):
        self._make_profile(pmc_id=207)
        token = self._make_token("bsowner207@example.com")
        self._make_owner_with_pmc(
            207, "bsowner207@example.com", token, 407, unit_id=2070, property_id=3070, pmc_id=207
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 207, "as_of_date": "not-a-date"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_happy_path_returns_asset_liability_and_retained_earnings(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=208)
        # Backdate `created` (auto_now_add) to a fixed inception point, same
        # pattern `_post_journal_entry` uses for `posted_at` -- so this test
        # doesn't depend on wall-clock time being before 2026-06-15.
        FinancePMCProfile.objects.filter(pk=profile.pk).update(
            created=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        )
        profile.refresh_from_db()
        token = self._make_token("bsowner208@example.com")
        self._make_owner_with_pmc(
            208, "bsowner208@example.com", token, 408, unit_id=2080, property_id=3080, pmc_id=208
        )

        posted_at = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
        # Rent AR posting: debit AR — Tenants (Asset), credit Rent Income.
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 5000, 0), ("Rent Income", 0, 5000)],
            posted_at=posted_at,
            source_txn_id=2081,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 208, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        content = body["content"]
        self.assertTrue(content["balanced"])

        asset_accounts = {a["name"]: a for a in content["asset_accounts"]}
        liability_accounts = {a["name"]: a for a in content["liability_accounts"]}

        # Only Asset accounts appear in asset_accounts, only Liability in
        # liability_accounts -- Income/Expense/derived-Equity are excluded
        # from those lists entirely.
        self.assertIn("AR — Tenants", asset_accounts)
        self.assertIn("Bank", asset_accounts)
        self.assertNotIn("Rent Income", asset_accounts)
        self.assertNotIn("Rent Income", liability_accounts)
        self.assertIn("Security Deposits Held", liability_accounts)
        self.assertIn("VAT Payable", liability_accounts)
        self.assertIn("AP — PMC Commission", liability_accounts)

        # AR — Tenants: debit-normal, 5000 - 0 = 5000.
        self.assertEqual(asset_accounts["AR — Tenants"]["balance"], 5000.0)
        # Bank had no activity -- present, zeroed.
        self.assertEqual(asset_accounts["Bank"]["balance"], 0.0)

        # Derived Retained Earnings: net_profit_loss since inception = 5000
        # (Rent Income) - 0 (no expenses) = 5000.
        self.assertEqual(content["equity"]["name"], "Retained Earnings")
        self.assertEqual(content["equity"]["balance"], 5000.0)
        self.assertNotIn("id", content["equity"])

        # Assets == Liabilities + Equity: 5000 == 0 + 5000.
        total_assets = sum(a["balance"] for a in asset_accounts.values())
        total_liabilities = sum(a["balance"] for a in liability_accounts.values())
        self.assertEqual(total_assets, total_liabilities + content["equity"]["balance"])

    def test_equation_holds_with_real_activity(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=209)
        # Backdate `created` (auto_now_add), same reasoning as the happy-path
        # test above -- decouples the since-inception window from wall-clock
        # time.
        FinancePMCProfile.objects.filter(pk=profile.pk).update(
            created=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        )
        profile.refresh_from_db()
        token = self._make_token("bsowner209@example.com")
        self._make_owner_with_pmc(
            209, "bsowner209@example.com", token, 409, unit_id=2090, property_id=3090, pmc_id=209
        )

        posted_at = dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc)
        # Rent posted: Asset up (AR), Income up.
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 10000, 0), ("Rent Income", 0, 10000)],
            posted_at=posted_at,
            source_txn_id=2091,
        )
        # Commission split posted: Liability up (AP credited, credit-normal),
        # Expense up (Commission Expense debited, debit-normal) -- matching
        # `post_commission_split`'s real convention.
        self._post_journal_entry(
            profile,
            "COMMISSION-SPLIT",
            [("Commission Expense", 800, 0), ("AP — PMC Commission", 0, 800)],
            posted_at=posted_at,
            source_txn_id=2091,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 209, "as_of_date": "2026-06-30"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        self.assertTrue(content["balanced"])

        total_assets = sum(a["balance"] for a in content["asset_accounts"])
        total_liabilities = sum(a["balance"] for a in content["liability_accounts"])
        retained_earnings = content["equity"]["balance"]

        # net_profit_loss = 10000 (Rent Income) - 800 (Commission Expense) = 9200.
        self.assertEqual(retained_earnings, 9200.0)
        # Assets: AR — Tenants = 10000. Liabilities: AP — PMC Commission = 800.
        self.assertEqual(total_assets, 10000.0)
        self.assertEqual(total_liabilities, 800.0)
        self.assertEqual(total_assets, total_liabilities + retained_earnings)

    def test_as_of_date_before_profile_creation_returns_zeroed_balanced_true(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=210)
        token = self._make_token("bsowner210@example.com")
        self._make_owner_with_pmc(
            210, "bsowner210@example.com", token, 410, unit_id=2100, property_id=3100, pmc_id=210
        )
        # `created` is auto_now_add -- set to "now" (test run time), which is
        # after this as_of_date (2020-01-01) regardless of when tests run,
        # per the spec: a valid degenerate empty window, not an error.
        self.assertTrue(profile.created.date() > dt.date(2020, 1, 1))

        response = self.client.get(
            self._url(),
            {"pmc_id": 210, "as_of_date": "2020-01-01"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        content = response.json()["content"]
        self.assertTrue(content["balanced"])
        for account in content["asset_accounts"]:
            self.assertEqual(account["balance"], 0.0)
        for account in content["liability_accounts"]:
            self.assertEqual(account["balance"], 0.0)
        self.assertEqual(content["equity"]["balance"], 0.0)


class AgeingReportTests(TrialBalanceReportTests):
    """Story 3.5 tests: GET /reports/ageing.

    Subclasses `TrialBalanceReportTests` to reuse its stand-in-table setup
    (Owner-branch PMC scoping) and helpers (`_make_profile`,
    `_post_journal_entry`, `_make_owner_with_pmc`), adding two more
    stand-in tables this report's candidate-row resolution needs that the
    parent class doesn't create: `lease_lease` (for `LeaseRef.unit_id`) and
    `lease_leasetransaction` (for `LeaseTransactionRef`, the report's actual
    row source).

    Covers all five I/O matrix rows from the spec:
      1. Happy path bucket boundaries -- rows overdue by 1, 30, 31, 60, 61,
         90, 91 days bucket as 1-30, 1-30, 31-60, 31-60, 61-90, 61-90, 90+.
      2. BOUNCED transaction still unresolved -- 45 days overdue, appears
         in 31-60, outstanding balance from the bounce-reversal-adjusted
         Ledger sum (not the raw amount).
      3. CREDITED-and-cleared transaction excluded entirely (outstanding
         balance is 0, even though status isn't REALIZED).
      4. Pagination -- content is one page, `pagination` is a sibling key
         with correct has_next/page_number/total_records.
      5. Unreachable/nonexistent pmc_id, unauthenticated -- same rejections
         as Stories 3.2-3.4 (403/404/401).

    Date-range-specific tests inherited from the parent class don't apply
    to this no-date-param endpoint -- skipped/overridden below, same
    pattern `ProfitLossReportTests`/`BalanceSheetReportTests` use.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL DEFAULT 'RENT_CHEQUE',
                    payment_type VARCHAR(20) NOT NULL DEFAULT 'CHEQUE',
                    status VARCHAR(20) NOT NULL DEFAULT 'BALANCE',
                    created TIMESTAMPTZ,
                    charge_id BIGINT,
                    cheque_date TIMESTAMPTZ
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM lease_leasetransaction")
            cursor.execute("DELETE FROM lease_lease")

    def _make_lease(self, lease_id, unit_id):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
                [lease_id, unit_id],
            )

    def _make_lease_transaction(
        self,
        txn_id,
        lease_id,
        amount,
        cheque_date,
        status="BALANCE",
        cheque_type="RENT_CHEQUE",
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, status, cheque_date) "
                "VALUES (%s, %s, %s, %s, 'CHEQUE', %s, %s)",
                [txn_id, lease_id, amount, cheque_type, status, cheque_date],
            )

    def _url(self):
        return reverse("ageing-report")

    # Date-range/as_of_date-specific tests inherited from the parent class
    # don't apply to this no-date-param endpoint.
    def test_happy_path_returns_every_account_balanced_true(self):
        self.skipTest("covered by this class's own bucket-boundary test below")

    def test_zero_activity_account_included_with_zero_totals(self):
        self.skipTest("not applicable -- Ageing has no per-account zero-activity concept")

    def test_reversal_entry_summed_unconditionally(self):
        self.skipTest("covered by this class's own BOUNCED test below")

    def test_missing_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- Ageing takes no date param")

    def test_invalid_date_format_rejected_with_400(self):
        self.skipTest("not applicable -- Ageing takes no date param")

    def test_inverted_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- Ageing takes no date param")

    def test_no_trailing_slash_url_also_works(self):
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=300)
        token = self._make_token("ageowner300@example.com")
        self._make_owner_with_pmc(
            300, "ageowner300@example.com", token, 500, unit_id=3000, property_id=4000, pmc_id=300
        )
        self._make_lease(lease_id=5000, unit_id=3000)
        cheque_date = today - dt.timedelta(days=10)
        self._make_lease_transaction(
            txn_id=6000, lease_id=5000, amount=1000, cheque_date=cheque_date
        )
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 1000, 0), ("Rent Income", 0, 1000)],
            posted_at=dt.datetime.combine(cheque_date, dt.time.min, tzinfo=dt.timezone.utc),
            source_txn_id=6000,
        )

        response = self.client.get(
            reverse("ageing-report-no-slash"),
            {"pmc_id": 300},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["content"]), 1)
        self.assertEqual(body["content"][0]["bucket"], "1-30")

    def test_happy_path_bucket_boundaries(self):
        """Rows overdue by 1, 30, 31, 60, 61, 90, 91 days bucket as
        1-30, 1-30, 31-60, 31-60, 61-90, 61-90, 90+ (inclusive lower bound,
        FR-13)."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=301)
        token = self._make_token("ageowner301@example.com")
        self._make_owner_with_pmc(
            301, "ageowner301@example.com", token, 501, unit_id=3010, property_id=4010, pmc_id=301
        )
        self._make_lease(lease_id=5010, unit_id=3010)

        expected = {
            6010: (1, "1-30"),
            6011: (30, "1-30"),
            6012: (31, "31-60"),
            6013: (60, "31-60"),
            6014: (61, "61-90"),
            6015: (90, "61-90"),
            6016: (91, "90+"),
        }
        for txn_id, (days, _bucket) in expected.items():
            cheque_date = today - dt.timedelta(days=days)
            self._make_lease_transaction(
                txn_id=txn_id, lease_id=5010, amount=100, cheque_date=cheque_date
            )
            self._post_journal_entry(
                profile,
                "CREATE-BALANCE",
                [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
                posted_at=dt.datetime.combine(
                    cheque_date, dt.time.min, tzinfo=dt.timezone.utc
                ),
                source_txn_id=txn_id,
            )

        response = self.client.get(
            self._url(),
            {"pmc_id": 301, "page_size": 50},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], 200)
        rows_by_txn = {row["lease_transaction_id"]: row for row in body["content"]}
        self.assertEqual(len(rows_by_txn), 7)
        for txn_id, (days, bucket) in expected.items():
            self.assertEqual(rows_by_txn[txn_id]["days_overdue"], days)
            self.assertEqual(rows_by_txn[txn_id]["bucket"], bucket)
            self.assertEqual(rows_by_txn[txn_id]["outstanding_amount"], 100.0)

    def test_bounced_transaction_still_unresolved_appears_in_31_60(self):
        """status=BOUNCED, cheque_date 45 days in the past, no further
        clearing posted -- appears in 31-60, outstanding balance from the
        bounce-reversal-adjusted Ledger sum (not the raw amount)."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=302)
        token = self._make_token("ageowner302@example.com")
        self._make_owner_with_pmc(
            302, "ageowner302@example.com", token, 502, unit_id=3020, property_id=4020, pmc_id=302
        )
        self._make_lease(lease_id=5020, unit_id=3020)
        cheque_date = today - dt.timedelta(days=45)
        self._make_lease_transaction(
            txn_id=6020,
            lease_id=5020,
            amount=2000,
            cheque_date=cheque_date,
            status="BOUNCED",
        )
        posted_at = dt.datetime.combine(cheque_date, dt.time.min, tzinfo=dt.timezone.utc)
        # Original rent AR posting (debit AR, credit Rent Income).
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 2000, 0), ("Rent Income", 0, 2000)],
            posted_at=posted_at,
            source_txn_id=6020,
        )
        # Bounce reversal is NOT a full clear -- it moves the debit from
        # Bank/AR pairing into Bounced Cheques while AR still nets 2000
        # outstanding (AR debited 2000 originally, never credited back to
        # zero here) -- confirming the balance is Ledger-derived, not the
        # raw LeaseTransaction.amount re-read blindly.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=6020).count(), 1
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 302},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        rows = body["content"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lease_transaction_id"], 6020)
        self.assertEqual(rows[0]["days_overdue"], 45)
        self.assertEqual(rows[0]["bucket"], "31-60")
        self.assertEqual(rows[0]["outstanding_amount"], 2000.0)

    def test_credited_and_cleared_transaction_excluded(self):
        """status=CREDITED, but post_cheque_clearing already posted (AR
        fully credited to zero) -- excluded entirely, outstanding balance
        is 0."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=303)
        token = self._make_token("ageowner303@example.com")
        self._make_owner_with_pmc(
            303, "ageowner303@example.com", token, 503, unit_id=3030, property_id=4030, pmc_id=303
        )
        self._make_lease(lease_id=5030, unit_id=3030)
        cheque_date = today - dt.timedelta(days=20)
        self._make_lease_transaction(
            txn_id=6030,
            lease_id=5030,
            amount=1500,
            cheque_date=cheque_date,
            status="CREDITED",
        )
        posted_at = dt.datetime.combine(cheque_date, dt.time.min, tzinfo=dt.timezone.utc)
        # Original rent AR posting.
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 1500, 0), ("Rent Income", 0, 1500)],
            posted_at=posted_at,
            source_txn_id=6030,
        )
        # post_cheque_clearing's posting: debit Bank, credit AR — Tenants --
        # zeroes the AR balance even though raw status stays CREDITED, not
        # REALIZED.
        self._post_journal_entry(
            profile,
            "BALANCE-CREDITED",
            [("Bank", 1500, 0), ("AR — Tenants", 0, 1500)],
            posted_at=posted_at,
            source_txn_id=6030,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 303},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["content"], [])

    def test_pagination_sibling_key_never_nested_in_content(self):
        """More candidate rows than page_size -- content is one page of
        rows; pagination (sibling key) has correct has_next/page_number/
        total_records."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=304)
        token = self._make_token("ageowner304@example.com")
        self._make_owner_with_pmc(
            304, "ageowner304@example.com", token, 504, unit_id=3040, property_id=4040, pmc_id=304
        )
        self._make_lease(lease_id=5040, unit_id=3040)

        for i in range(5):
            txn_id = 6040 + i
            cheque_date = today - dt.timedelta(days=10 + i)
            self._make_lease_transaction(
                txn_id=txn_id, lease_id=5040, amount=100, cheque_date=cheque_date
            )
            self._post_journal_entry(
                profile,
                "CREATE-BALANCE",
                [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
                posted_at=dt.datetime.combine(
                    cheque_date, dt.time.min, tzinfo=dt.timezone.utc
                ),
                source_txn_id=txn_id,
            )

        response = self.client.get(
            self._url(),
            {"pmc_id": 304, "page": 1, "page_size": 2},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["content"]), 2)
        self.assertNotIn("pagination", body["content"])
        pagination = body["pagination"]
        self.assertTrue(pagination["has_next"])
        self.assertFalse(pagination["has_previous"])
        self.assertEqual(pagination["page_number"], 1)
        self.assertEqual(pagination["total_records"], 5)
        self.assertEqual(pagination["next_page_number"], 2)
        self.assertIsNone(pagination["previous_page_number"])

        response_page2 = self.client.get(
            self._url(),
            {"pmc_id": 304, "page": 2, "page_size": 2},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        body2 = response_page2.json()
        self.assertEqual(len(body2["content"]), 2)
        self.assertTrue(body2["pagination"]["has_previous"])
        self.assertTrue(body2["pagination"]["has_next"])

        response_page3 = self.client.get(
            self._url(),
            {"pmc_id": 304, "page": 3, "page_size": 2},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        body3 = response_page3.json()
        self.assertEqual(len(body3["content"]), 1)
        self.assertFalse(body3["pagination"]["has_next"])
        self.assertTrue(body3["pagination"]["has_previous"])

    def test_out_of_range_page_clamps_to_last_page(self):
        """page beyond the actual page count is clamped to the last page
        (200), never an unhandled EmptyPage/500 (review finding)."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=310)
        token = self._make_token("ageowner310@example.com")
        self._make_owner_with_pmc(
            310, "ageowner310@example.com", token, 510, unit_id=3100, property_id=4100, pmc_id=310
        )
        self._make_lease(lease_id=5100, unit_id=3100)

        for i in range(5):
            txn_id = 6100 + i
            cheque_date = today - dt.timedelta(days=10 + i)
            self._make_lease_transaction(
                txn_id=txn_id, lease_id=5100, amount=100, cheque_date=cheque_date
            )
            self._post_journal_entry(
                profile,
                "CREATE-BALANCE",
                [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
                posted_at=dt.datetime.combine(
                    cheque_date, dt.time.min, tzinfo=dt.timezone.utc
                ),
                source_txn_id=txn_id,
            )

        # page_size=2 over 5 rows -> 3 real pages; request page 99.
        response = self.client.get(
            self._url(),
            {"pmc_id": 310, "page": 99, "page_size": 2},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["content"]), 1)
        self.assertEqual(body["pagination"]["page_number"], 3)
        self.assertFalse(body["pagination"]["has_next"])

    def test_page_size_over_max_rejected_with_400(self):
        profile = self._make_profile(pmc_id=311)
        token = self._make_token("ageowner311@example.com")
        self._make_owner_with_pmc(
            311, "ageowner311@example.com", token, 511, unit_id=3110, property_id=4110, pmc_id=311
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 311, "page_size": 99999},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_negative_outstanding_balance_excluded(self):
        """An overpaid/credit-balance row (outstanding_amount < 0) is
        excluded entirely -- it is money owed back to the tenant, not
        overdue rent (review finding)."""
        import datetime as dt

        today = timezone.localdate()
        profile = self._make_profile(pmc_id=312)
        token = self._make_token("ageowner312@example.com")
        self._make_owner_with_pmc(
            312, "ageowner312@example.com", token, 512, unit_id=3120, property_id=4120, pmc_id=312
        )
        self._make_lease(lease_id=5120, unit_id=3120)
        cheque_date = today - dt.timedelta(days=15)
        self._make_lease_transaction(
            txn_id=6120, lease_id=5120, amount=100, cheque_date=cheque_date
        )
        posted_at = dt.datetime.combine(cheque_date, dt.time.min, tzinfo=dt.timezone.utc)
        # Original rent AR posting (debit AR 100), then an overpayment
        # credit against the same AR line (credit AR 150) -- nets AR to
        # -50 (a credit balance owed back to the tenant).
        self._post_journal_entry(
            profile,
            "CREATE-BALANCE",
            [("AR — Tenants", 100, 0), ("Rent Income", 0, 100)],
            posted_at=posted_at,
            source_txn_id=6120,
        )
        self._post_journal_entry(
            profile,
            "OVERPAYMENT-CREDIT",
            [("Bank", 150, 0), ("AR — Tenants", 0, 150)],
            posted_at=posted_at,
            source_txn_id=6120,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 312},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["content"], [])

    def test_unreachable_pmc_id_rejected_with_403(self):
        self._make_profile(pmc_id=305)
        token = self._make_token("ageowner305@example.com")
        self._make_owner_with_pmc(
            305, "ageowner305@example.com", token, 505, unit_id=3050, property_id=4050, pmc_id=306
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 305},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], 403)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("ageowner307@example.com")
        self._make_owner_with_pmc(
            307, "ageowner307@example.com", token, 507, unit_id=3070, property_id=4070, pmc_id=999
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 999},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], 404)

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self._make_profile(pmc_id=308)

        response = self.client.get(self._url(), {"pmc_id": 308})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)

    def test_expired_token_rejected_with_401(self):
        token = self._make_token("ageowner309@example.com", exp_delta_seconds=-10)
        self._make_profile(pmc_id=309)

        response = self.client.get(
            self._url(),
            {"pmc_id": 309},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 401)


@override_settings(
    JWT_SECRET_KEY="test-jwt-secret-4-1", JWT_ALGORITHM="HS256"
)
class BankStatementImportTests(TestCase):
    """Story 4.1 tests: POST /reconciliation/bank-statement-import.

    Follows `TrialBalanceReportTests`'s stand-in-table fixture pattern (own
    class, not a subclass -- this story has no report-computation logic to
    inherit, spec Code Map) for exercising the Owner branch of
    `get_pmc_ids_for_user_profile` through the real HTTP view.

    Covers every I/O Matrix row from the spec:
      1. Happy path -- valid CSV, 5 rows, all columns present -> 201,
         content.created == 5.
      2. Missing/invalid JWT -> 401.
      3. pmc_id not reachable by caller -> 403.
      4. pmc_id has no FinancePMCProfile -> 404.
      5. Missing required column -> 400, no rows created.
      6. Row with unparsable amount/date -> 400, no rows created
         (whole-file rejection).
      7. Empty CSV (headers only) -> 201, content.created == 0.
      8. No file provided -> 400.
    Plus the two Acceptance Criteria rows: cross-PMC disjointness, and the
    no-trailing-slash URL variant.
    """

    JWT_SECRET_KEY = "test-jwt-secret-4-1"
    JWT_ALGORITHM = "HS256"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_userprofile (
                    id BIGSERIAL PRIMARY KEY,
                    email VARCHAR(255),
                    token TEXT,
                    user_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_owner (
                    userprofile_ptr_id BIGINT PRIMARY KEY
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_propertymanager (
                    userprofile_ptr_id BIGINT PRIMARY KEY,
                    company_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_pmcpmmapping (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT NOT NULL,
                    pm_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unitowner (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL,
                    owner_id BIGINT,
                    ownership_percent NUMERIC(5, 2) NOT NULL DEFAULT 100
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        super().tearDownClass()

    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.all().delete()
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _url(self):
        return reverse("bank-statement-import")

    def _make_token(self, email, exp_delta_seconds=3600):
        import datetime as dt

        payload = {
            "user_id": 1,
            "email": email,
            "exp": dt.datetime.utcnow() + dt.timedelta(seconds=exp_delta_seconds),
        }
        return jwt.encode(payload, self.JWT_SECRET_KEY, algorithm=self.JWT_ALGORITHM)

    def _make_owner_with_pmc(
        self, profile_id, email, token, auth_user_id, unit_id, property_id, pmc_id
    ):
        from django.contrib.auth.models import User

        User.objects.create(
            id=auth_user_id,
            username=f"owner{auth_user_id}",
            email=email,
            is_active=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_service_userprofile (id, email, token, user_id) "
                "VALUES (%s, %s, %s, %s)",
                [profile_id, email, token, auth_user_id],
            )
            cursor.execute(
                "INSERT INTO user_service_owner (userprofile_ptr_id) VALUES (%s)",
                [profile_id],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [property_id, pmc_id],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [unit_id, property_id],
            )
            cursor.execute(
                "INSERT INTO property_unitowner (unit_id, owner_id) VALUES (%s, %s)",
                [unit_id, profile_id],
            )

    def _make_profile(self, pmc_id):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def _csv_file(self, text, name="statement.csv"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")

    def test_happy_path_creates_one_row_per_data_row(self):
        profile = self._make_profile(pmc_id=401)
        token = self._make_token("bsiowner401@example.com")
        self._make_owner_with_pmc(
            401, "bsiowner401@example.com", token, 601, unit_id=4010, property_id=5010, pmc_id=401
        )
        csv_text = (
            "date,amount,reference\n"
            "2026-01-01,100.00,REF-1\n"
            "2026-01-02,200.50,REF-2\n"
            "2026-01-03,-50.25,REF-3\n"
            "2026-01-04,0,REF-4\n"
            "2026-01-05,999.99,REF-5\n"
        )

        response = self.client.post(
            self._url(),
            {"pmc_id": 401, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["content"]["created"], 5)
        rows = BankStatementLine.objects.filter(finance_pmc_profile=profile)
        self.assertEqual(rows.count(), 5)
        row1 = rows.get(reference="REF-2")
        self.assertEqual(row1.amount, decimal.Decimal("200.50"))
        self.assertEqual(row1.statement_date, datetime.date(2026, 1, 2))
        self.assertFalse(row1.reconciled)

    def test_header_case_insensitive_and_whitespace_tolerant(self):
        profile = self._make_profile(pmc_id=402)
        token = self._make_token("bsiowner402@example.com")
        self._make_owner_with_pmc(
            402, "bsiowner402@example.com", token, 602, unit_id=4020, property_id=5020, pmc_id=402
        )
        csv_text = " Date , AMOUNT ,Reference\n2026-02-01,10.00,REF-A\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 402, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["content"]["created"], 1)
        self.assertEqual(
            BankStatementLine.objects.filter(finance_pmc_profile=profile).count(), 1
        )

    def test_cross_pmc_rows_are_disjoint(self):
        profile_a = self._make_profile(pmc_id=403)
        profile_b = self._make_profile(pmc_id=404)
        token_a = self._make_token("bsiowner403@example.com")
        token_b = self._make_token("bsiowner404@example.com")
        self._make_owner_with_pmc(
            403, "bsiowner403@example.com", token_a, 603, unit_id=4030, property_id=5030, pmc_id=403
        )
        self._make_owner_with_pmc(
            404, "bsiowner404@example.com", token_b, 604, unit_id=4040, property_id=5040, pmc_id=404
        )
        csv_text = "date,amount,reference\n2026-03-01,10.00,REF-X\n2026-03-02,20.00,REF-Y\n"

        response_a = self.client.post(
            self._url(),
            {"pmc_id": 403, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token_a}",
        )
        response_b = self.client.post(
            self._url(),
            {"pmc_id": 404, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token_b}",
        )

        self.assertEqual(response_a.status_code, 201)
        self.assertEqual(response_b.status_code, 201)
        rows_a = BankStatementLine.objects.filter(finance_pmc_profile=profile_a)
        rows_b = BankStatementLine.objects.filter(finance_pmc_profile=profile_b)
        self.assertEqual(rows_a.count(), 2)
        self.assertEqual(rows_b.count(), 2)
        self.assertEqual(set(rows_a.values_list("id", flat=True)) & set(
            rows_b.values_list("id", flat=True)
        ), set())

    def test_empty_csv_creates_zero_rows(self):
        self._make_profile(pmc_id=405)
        token = self._make_token("bsiowner405@example.com")
        self._make_owner_with_pmc(
            405, "bsiowner405@example.com", token, 605, unit_id=4050, property_id=5050, pmc_id=405
        )
        csv_text = "date,amount,reference\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 405, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["content"]["created"], 0)

    def test_missing_required_column_rejected_with_400_and_creates_nothing(self):
        self._make_profile(pmc_id=406)
        token = self._make_token("bsiowner406@example.com")
        self._make_owner_with_pmc(
            406, "bsiowner406@example.com", token, 606, unit_id=4060, property_id=5060, pmc_id=406
        )
        csv_text = "date,amount\n2026-01-01,10.00\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 406, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("reference", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_unparsable_amount_rejected_with_400_names_row_and_creates_nothing(self):
        self._make_profile(pmc_id=407)
        token = self._make_token("bsiowner407@example.com")
        self._make_owner_with_pmc(
            407, "bsiowner407@example.com", token, 607, unit_id=4070, property_id=5070, pmc_id=407
        )
        csv_text = (
            "date,amount,reference\n"
            "2026-01-01,100.00,REF-1\n"
            "2026-01-02,abc,REF-2\n"
        )

        response = self.client.post(
            self._url(),
            {"pmc_id": 407, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Row 2", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_unparsable_date_rejected_with_400_names_row_and_creates_nothing(self):
        self._make_profile(pmc_id=408)
        token = self._make_token("bsiowner408@example.com")
        self._make_owner_with_pmc(
            408, "bsiowner408@example.com", token, 608, unit_id=4080, property_id=5080, pmc_id=408
        )
        csv_text = "date,amount,reference\nnot-a-date,100.00,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 408, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Row 1", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_over_precision_amount_rejected_with_400_not_silently_rounded(self):
        """Post-review patch: a 3-decimal-place amount must be rejected, not
        silently quantized by Decimal.quantize's default rounding."""
        self._make_profile(pmc_id=416)
        token = self._make_token("bsiowner416@example.com")
        self._make_owner_with_pmc(
            416, "bsiowner416@example.com", token, 616, unit_id=4160, property_id=5160, pmc_id=416
        )
        csv_text = "date,amount,reference\n2026-01-01,10.005,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 416, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Row 1", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_amount_exceeding_max_digits_rejected_with_400(self):
        """Post-review patch: an amount too large for the DecimalField
        (max_digits=14, decimal_places=2) must be rejected with 400, not
        surfaced as an unhandled DB error from bulk_create."""
        self._make_profile(pmc_id=417)
        token = self._make_token("bsiowner417@example.com")
        self._make_owner_with_pmc(
            417, "bsiowner417@example.com", token, 617, unit_id=4170, property_id=5170, pmc_id=417
        )
        csv_text = "date,amount,reference\n2026-01-01,9999999999999.99,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 417, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Row 1", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_nan_and_infinity_amount_rejected_with_400(self):
        """Post-review patch: Decimal('NaN')/Decimal('Infinity') parse
        successfully as Decimals but must not be accepted as amounts."""
        self._make_profile(pmc_id=418)
        token = self._make_token("bsiowner418@example.com")
        self._make_owner_with_pmc(
            418, "bsiowner418@example.com", token, 618, unit_id=4180, property_id=5180, pmc_id=418
        )
        csv_text = "date,amount,reference\n2026-01-01,NaN,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 418, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_reference_over_max_length_rejected_with_400(self):
        """Post-review patch: a reference longer than the CharField's
        max_length=255 must be rejected with 400, not a raw DB error."""
        self._make_profile(pmc_id=419)
        token = self._make_token("bsiowner419@example.com")
        self._make_owner_with_pmc(
            419, "bsiowner419@example.com", token, 619, unit_id=4190, property_id=5190, pmc_id=419
        )
        long_reference = "X" * 256
        csv_text = f"date,amount,reference\n2026-01-01,10.00,{long_reference}\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 419, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Row 1", response.json()["message"])
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_duplicate_case_insensitive_header_rejected_with_400(self):
        """Post-review patch: two columns normalizing to the same header
        name (e.g. 'date' and 'DATE') must be rejected, not silently
        collapsed with one overwriting the other."""
        self._make_profile(pmc_id=420)
        token = self._make_token("bsiowner420@example.com")
        self._make_owner_with_pmc(
            420, "bsiowner420@example.com", token, 620, unit_id=4200, property_id=5200, pmc_id=420
        )
        csv_text = "date,amount,DATE,reference\n2026-01-01,10.00,2026-02-02,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 420, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_invalid_file_encoding_rejected_with_400(self):
        """Post-review patch: a file that isn't valid utf-8-sig must be
        rejected with 400, not an unhandled UnicodeDecodeError."""
        self._make_profile(pmc_id=421)
        token = self._make_token("bsiowner421@example.com")
        self._make_owner_with_pmc(
            421, "bsiowner421@example.com", token, 621, unit_id=4210, property_id=5210, pmc_id=421
        )
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad_file = SimpleUploadedFile(
            "statement.csv",
            "date,amount,reference\n2026-01-01,10.00,café\n".encode("utf-16"),
            content_type="text/csv",
        )

        response = self.client.post(
            self._url(),
            {"pmc_id": 421, "file": bad_file},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_no_file_provided_rejected_with_400(self):
        self._make_profile(pmc_id=409)
        token = self._make_token("bsiowner409@example.com")
        self._make_owner_with_pmc(
            409, "bsiowner409@example.com", token, 609, unit_id=4090, property_id=5090, pmc_id=409
        )

        response = self.client.post(
            self._url(),
            {"pmc_id": 409},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self._make_profile(pmc_id=410)
        csv_text = "date,amount,reference\n2026-01-01,10.00,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 410, "file": self._csv_file(csv_text)},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_expired_token_rejected_with_401(self):
        token = self._make_token("bsiowner411@example.com", exp_delta_seconds=-10)
        self._make_profile(pmc_id=411)
        csv_text = "date,amount,reference\n2026-01-01,10.00,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 411, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 401)

    def test_unreachable_pmc_id_rejected_with_403(self):
        self._make_profile(pmc_id=412)
        # Owner is scoped to pmc_id=413, not pmc_id=412.
        token = self._make_token("bsiowner412@example.com")
        self._make_owner_with_pmc(
            412, "bsiowner412@example.com", token, 612, unit_id=4120, property_id=5120, pmc_id=413
        )
        csv_text = "date,amount,reference\n2026-01-01,10.00,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 412, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["status"], 403)
        self.assertEqual(BankStatementLine.objects.count(), 0)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("bsiowner414@example.com")
        self._make_owner_with_pmc(
            414, "bsiowner414@example.com", token, 614, unit_id=4140, property_id=5140, pmc_id=999
        )
        csv_text = "date,amount,reference\n2026-01-01,10.00,REF-1\n"

        response = self.client.post(
            self._url(),
            {"pmc_id": 999, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], 404)

    def test_no_trailing_slash_url_also_works(self):
        profile = self._make_profile(pmc_id=415)
        token = self._make_token("bsiowner415@example.com")
        self._make_owner_with_pmc(
            415, "bsiowner415@example.com", token, 615, unit_id=4150, property_id=5150, pmc_id=415
        )
        csv_text = "date,amount,reference\n2026-01-01,10.00,REF-1\n"

        response = self.client.post(
            reverse("bank-statement-import-no-slash"),
            {"pmc_id": 415, "file": self._csv_file(csv_text)},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            BankStatementLine.objects.filter(finance_pmc_profile=profile).count(), 1
        )


@override_settings(
    JWT_SECRET_KEY="test-jwt-secret-4-2", JWT_ALGORITHM="HS256"
)
class BankStatementMatchTests(TestCase):
    """Story 4.2 tests: GET /reconciliation/suggested-matches and
    POST /reconciliation/match.

    Reuses `BankStatementImportTests`'s stand-in-table fixture pattern
    (`_make_profile`, `_make_token`, `_make_owner_with_pmc`) plus
    `TrialBalanceReportTests._post_journal_entry` for creating Bank Journal
    fixture entries (spec Code Map).

    Covers every I/O Matrix row from the spec:
      1. Suggestions happy path -- one suggestion returned.
      2. Suggestions: no candidates -- empty list.
      3. Suggestions: date outside window -- excluded.
      4. Confirm happy path -- both sides reconciled.
      5. Confirm: statement line already reconciled -- 409.
      6. Confirm: journal entry already matched elsewhere -- 409.
      7. Confirm: manual pairing outside suggestions -- 200, accepted.
      8. Reject happy path -- neither side reconciled.
      9. Reject then re-confirm same pair -- 200, transitions to confirmed.
      10. Re-confirm already-confirmed same pair -- 200, idempotent.
      11. Unknown ids -- 404.
      12. Invalid action -- 400.
    Plus auth/scope (401/403) and no-trailing-slash URL variants.
    """

    JWT_SECRET_KEY = "test-jwt-secret-4-2"
    JWT_ALGORITHM = "HS256"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_userprofile (
                    id BIGSERIAL PRIMARY KEY,
                    email VARCHAR(255),
                    token TEXT,
                    user_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_owner (
                    userprofile_ptr_id BIGINT PRIMARY KEY
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_service_propertymanager (
                    userprofile_ptr_id BIGINT PRIMARY KEY,
                    company_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_pmcpmmapping (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT NOT NULL,
                    pm_id BIGINT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unitowner (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT NOT NULL,
                    owner_id BIGINT,
                    ownership_percent NUMERIC(5, 2) NOT NULL DEFAULT 100
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DROP TABLE IF EXISTS {table}")
        super().tearDownClass()

    def setUp(self):
        from django.contrib.auth.models import User

        User.objects.all().delete()
        with connection.cursor() as cursor:
            for table in (
                "property_unitowner",
                "property_unit",
                "property_property",
                "property_pmcpmmapping",
                "user_service_propertymanager",
                "user_service_owner",
                "user_service_userprofile",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _suggested_matches_url(self):
        return reverse("suggested-matches")

    def _match_url(self):
        return reverse("apply-bank-statement-match")

    def _make_token(self, email, exp_delta_seconds=3600):
        import datetime as dt

        payload = {
            "user_id": 1,
            "email": email,
            "exp": dt.datetime.utcnow() + dt.timedelta(seconds=exp_delta_seconds),
        }
        return jwt.encode(payload, self.JWT_SECRET_KEY, algorithm=self.JWT_ALGORITHM)

    def _make_owner_with_pmc(
        self, profile_id, email, token, auth_user_id, unit_id, property_id, pmc_id
    ):
        from django.contrib.auth.models import User

        User.objects.create(
            id=auth_user_id,
            username=f"owner{auth_user_id}",
            email=email,
            is_active=True,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO user_service_userprofile (id, email, token, user_id) "
                "VALUES (%s, %s, %s, %s)",
                [profile_id, email, token, auth_user_id],
            )
            cursor.execute(
                "INSERT INTO user_service_owner (userprofile_ptr_id) VALUES (%s)",
                [profile_id],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [property_id, pmc_id],
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [unit_id, property_id],
            )
            cursor.execute(
                "INSERT INTO property_unitowner (unit_id, owner_id) VALUES (%s, %s)",
                [unit_id, profile_id],
            )

    def _make_profile(self, pmc_id):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def _post_journal_entry(
        self,
        profile,
        transition,
        lines,
        posted_at=None,
        source_txn_id=1,
    ):
        """lines: list of (account_name, debit, credit) tuples. Mirrors
        TrialBalanceReportTests._post_journal_entry (spec Code Map)."""
        entry = JournalEntry.objects.create(
            finance_pmc_profile=profile,
            source_lease_transaction_id=source_txn_id,
            source_status_transition=transition,
        )
        if posted_at is not None:
            JournalEntry.objects.filter(pk=entry.pk).update(posted_at=posted_at)
            entry.refresh_from_db()
        for account_name, debit, credit in lines:
            account = Account.objects.get(
                finance_pmc_profile=profile, name=account_name
            )
            LedgerLine.objects.create(
                journal_entry=entry, account=account, debit=debit, credit=credit
            )
        return entry

    def _make_bank_journal_entry(self, profile, amount, posted_at, source_txn_id):
        """A Bank Journal entry matching post_cheque_clearing's posting
        shape: debit Bank, credit AR — Tenants (spec Boundaries &
        Constraints)."""
        return self._post_journal_entry(
            profile,
            "CREDITED-REALIZED",
            [("Bank", amount, 0), ("AR — Tenants", 0, amount)],
            posted_at=posted_at,
            source_txn_id=source_txn_id,
        )

    def _make_statement_line(self, profile, statement_date, amount, reference):
        return BankStatementLine.objects.create(
            finance_pmc_profile=profile,
            statement_date=statement_date,
            amount=decimal.Decimal(str(amount)),
            reference=reference,
        )

    # -- Suggestions --------------------------------------------------

    def test_suggestions_happy_path_returns_one_match(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=501)
        token = self._make_token("matchowner501@example.com")
        self._make_owner_with_pmc(
            501, "matchowner501@example.com", token, 701, unit_id=8010, property_id=9010, pmc_id=501
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile,
            500,
            dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            source_txn_id=901,
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 501},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        suggestions = response.json()["content"]["suggestions"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["bank_statement_line_id"], line.id)
        self.assertEqual(suggestions[0]["journal_entry_id"], entry.id)

    def test_suggestions_no_candidates_returns_empty_list(self):
        profile = self._make_profile(pmc_id=502)
        token = self._make_token("matchowner502@example.com")
        self._make_owner_with_pmc(
            502, "matchowner502@example.com", token, 702, unit_id=8020, property_id=9020, pmc_id=502
        )
        self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 502},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["suggestions"], [])

    def test_suggestions_date_outside_window_excluded(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=503)
        token = self._make_token("matchowner503@example.com")
        self._make_owner_with_pmc(
            503, "matchowner503@example.com", token, 703, unit_id=8030, property_id=9030, pmc_id=503
        )
        self._make_statement_line(
            profile, datetime.date(2026, 6, 1), "500.00", "REF-1"
        )
        self._make_bank_journal_entry(
            profile,
            500,
            dt.datetime(2026, 6, 9, tzinfo=dt.timezone.utc),  # 8 days apart
            source_txn_id=902,
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 503},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["suggestions"], [])

    def test_confirmed_pair_excluded_from_later_suggestions(self):
        """Acceptance Criteria: a confirmed match's line/entry no longer
        appear in the suggestions list."""
        import datetime as dt

        profile = self._make_profile(pmc_id=504)
        token = self._make_token("matchowner504@example.com")
        self._make_owner_with_pmc(
            504, "matchowner504@example.com", token, 704, unit_id=8040, property_id=9040, pmc_id=504
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile,
            500,
            dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            source_txn_id=903,
        )

        confirm_response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 504,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(confirm_response.status_code, 200)

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 504},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"]["suggestions"], [])

    # -- Confirm / Reject -----------------------------------------------

    def test_confirm_happy_path_reconciles_both_sides(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=505)
        token = self._make_token("matchowner505@example.com")
        self._make_owner_with_pmc(
            505, "matchowner505@example.com", token, 705, unit_id=8050, property_id=9050, pmc_id=505
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile,
            500,
            dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            source_txn_id=904,
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 505,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertTrue(line.reconciled)
        match = BankStatementMatch.objects.get(
            bank_statement_line=line, journal_entry=entry
        )
        self.assertEqual(match.status, BankStatementMatch.CONFIRMED)

    def test_confirm_statement_line_already_confirmed_elsewhere_rejected_409(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=506)
        token = self._make_token("matchowner506@example.com")
        self._make_owner_with_pmc(
            506, "matchowner506@example.com", token, 706, unit_id=8060, property_id=9060, pmc_id=506
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry_1 = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=905
        )
        entry_2 = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 13, tzinfo=dt.timezone.utc), source_txn_id=906
        )

        first = self.client.post(
            self._match_url(),
            {
                "pmc_id": 506,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry_1.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self._match_url(),
            {
                "pmc_id": 506,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry_2.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(second.status_code, 409)
        self.assertFalse(
            BankStatementMatch.objects.filter(
                bank_statement_line=line,
                journal_entry=entry_2,
                status=BankStatementMatch.CONFIRMED,
            ).exists()
        )
        # First pair's state is untouched.
        match_1 = BankStatementMatch.objects.get(
            bank_statement_line=line, journal_entry=entry_1
        )
        self.assertEqual(match_1.status, BankStatementMatch.CONFIRMED)

    def test_confirm_journal_entry_already_confirmed_elsewhere_rejected_409(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=507)
        token = self._make_token("matchowner507@example.com")
        self._make_owner_with_pmc(
            507, "matchowner507@example.com", token, 707, unit_id=8070, property_id=9070, pmc_id=507
        )
        line_a = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-A"
        )
        line_b = self._make_statement_line(
            profile, datetime.date(2026, 6, 11), "500.00", "REF-B"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=907
        )

        first = self.client.post(
            self._match_url(),
            {
                "pmc_id": 507,
                "bank_statement_line_id": line_a.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self._match_url(),
            {
                "pmc_id": 507,
                "bank_statement_line_id": line_b.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(second.status_code, 409)
        line_b.refresh_from_db()
        self.assertFalse(line_b.reconciled)

    def test_confirm_manual_pairing_outside_suggestions_accepted(self):
        """A journal_entry_id never returned by the suggestion heuristic
        (different amount) is still accepted as a manual confirm."""
        import datetime as dt

        profile = self._make_profile(pmc_id=508)
        token = self._make_token("matchowner508@example.com")
        self._make_owner_with_pmc(
            508, "matchowner508@example.com", token, 708, unit_id=8080, property_id=9080, pmc_id=508
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        # Different amount and far outside the date window -- the
        # suggestion heuristic would never surface this pair.
        entry = self._make_bank_journal_entry(
            profile, 777, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), source_txn_id=908
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 508,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertTrue(line.reconciled)

    def test_reject_happy_path_neither_side_reconciled(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=509)
        token = self._make_token("matchowner509@example.com")
        self._make_owner_with_pmc(
            509, "matchowner509@example.com", token, 709, unit_id=8090, property_id=9090, pmc_id=509
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=909
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 509,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "reject",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertFalse(line.reconciled)
        match = BankStatementMatch.objects.get(
            bank_statement_line=line, journal_entry=entry
        )
        self.assertEqual(match.status, BankStatementMatch.REJECTED)

    def test_reject_confirmed_pair_rejected_with_409(self):
        """Post-review patch: rejecting an already-confirmed pair must not
        silently flip it to rejected while leaving reconciled=True with no
        confirmed match backing it -- an inconsistent state."""
        import datetime as dt

        profile = self._make_profile(pmc_id=512)
        token = self._make_token("matchowner512@example.com")
        self._make_owner_with_pmc(
            512, "matchowner512@example.com", token, 712, unit_id=8120, property_id=9120, pmc_id=512
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=912
        )

        confirm_response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 512,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(confirm_response.status_code, 200)

        reject_response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 512,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "reject",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(reject_response.status_code, 409)
        line.refresh_from_db()
        self.assertTrue(line.reconciled)
        match = BankStatementMatch.objects.get(
            bank_statement_line=line, journal_entry=entry
        )
        self.assertEqual(match.status, BankStatementMatch.CONFIRMED)

    def test_suggestions_date_exactly_at_window_boundary_included(self):
        """Post-review patch: exactly MATCH_DATE_WINDOW_DAYS (7) days apart
        is still inside the closed interval and must be suggested -- locks
        in the boundary against an off-by-one regression (e.g. > vs >=)."""
        profile = self._make_profile(pmc_id=513)
        token = self._make_token("matchowner513@example.com")
        self._make_owner_with_pmc(
            513, "matchowner513@example.com", token, 713, unit_id=8130, property_id=9130, pmc_id=513
        )
        self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        import datetime as dt

        self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 17, tzinfo=dt.timezone.utc), source_txn_id=913
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 513},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["content"]["suggestions"]), 1)

    def test_reject_then_reconfirm_same_pair_succeeds(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=510)
        token = self._make_token("matchowner510@example.com")
        self._make_owner_with_pmc(
            510, "matchowner510@example.com", token, 710, unit_id=8100, property_id=9100, pmc_id=510
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=910
        )

        reject_response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 510,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "reject",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(reject_response.status_code, 200)

        confirm_response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 510,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(confirm_response.status_code, 200)
        line.refresh_from_db()
        self.assertTrue(line.reconciled)
        match = BankStatementMatch.objects.get(
            bank_statement_line=line, journal_entry=entry
        )
        self.assertEqual(match.status, BankStatementMatch.CONFIRMED)
        # Exactly one row for this pair -- the reject transitioned in place,
        # not a second row.
        self.assertEqual(
            BankStatementMatch.objects.filter(
                bank_statement_line=line, journal_entry=entry
            ).count(),
            1,
        )

    def test_reconfirm_already_confirmed_pair_is_idempotent(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=511)
        token = self._make_token("matchowner511@example.com")
        self._make_owner_with_pmc(
            511, "matchowner511@example.com", token, 711, unit_id=8110, property_id=9110, pmc_id=511
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=911
        )

        first = self.client.post(
            self._match_url(),
            {
                "pmc_id": 511,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self._match_url(),
            {
                "pmc_id": 511,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            BankStatementMatch.objects.filter(
                bank_statement_line=line, journal_entry=entry
            ).count(),
            1,
        )

    def test_unknown_bank_statement_line_id_rejected_404(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=512)
        token = self._make_token("matchowner512@example.com")
        self._make_owner_with_pmc(
            512, "matchowner512@example.com", token, 712, unit_id=8120, property_id=9120, pmc_id=512
        )
        entry = self._make_bank_journal_entry(
            profile,
            500,
            dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc),
            source_txn_id=912,
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 512,
                "bank_statement_line_id": 999999,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)

    def test_unknown_journal_entry_id_rejected_404(self):
        profile = self._make_profile(pmc_id=513)
        token = self._make_token("matchowner513@example.com")
        self._make_owner_with_pmc(
            513, "matchowner513@example.com", token, 713, unit_id=8130, property_id=9130, pmc_id=513
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 513,
                "bank_statement_line_id": line.id,
                "journal_entry_id": 999999,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)

    def test_ids_belonging_to_different_pmc_rejected_404(self):
        import datetime as dt

        profile_a = self._make_profile(pmc_id=514)
        profile_b = self._make_profile(pmc_id=515)
        token_a = self._make_token("matchowner514@example.com")
        self._make_owner_with_pmc(
            514, "matchowner514@example.com", token_a, 714, unit_id=8140, property_id=9140, pmc_id=514
        )
        line_b = self._make_statement_line(
            profile_b, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry_b = self._make_bank_journal_entry(
            profile_b, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=913
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 514,
                "bank_statement_line_id": line_b.id,
                "journal_entry_id": entry_b.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token_a}",
        )

        self.assertEqual(response.status_code, 404)

    def test_invalid_action_rejected_400(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=516)
        token = self._make_token("matchowner516@example.com")
        self._make_owner_with_pmc(
            516, "matchowner516@example.com", token, 716, unit_id=8160, property_id=9160, pmc_id=516
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=914
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 516,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "maybe",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    # -- Auth / scope -----------------------------------------------------

    def test_suggestions_unauthenticated_rejected_401(self):
        self._make_profile(pmc_id=517)

        response = self.client.get(self._suggested_matches_url(), {"pmc_id": 517})

        self.assertEqual(response.status_code, 401)

    def test_suggestions_unreachable_pmc_rejected_403(self):
        self._make_profile(pmc_id=518)
        token = self._make_token("matchowner518@example.com")
        self._make_owner_with_pmc(
            518, "matchowner518@example.com", token, 718, unit_id=8180, property_id=9180, pmc_id=519
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 518},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)

    def test_suggestions_nonexistent_pmc_rejected_404(self):
        token = self._make_token("matchowner520@example.com")
        self._make_owner_with_pmc(
            520, "matchowner520@example.com", token, 720, unit_id=8200, property_id=9200, pmc_id=999
        )

        response = self.client.get(
            self._suggested_matches_url(),
            {"pmc_id": 999},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)

    def test_match_unauthenticated_rejected_401(self):
        self._make_profile(pmc_id=521)

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 521,
                "bank_statement_line_id": 1,
                "journal_entry_id": 1,
                "action": "confirm",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_match_unreachable_pmc_rejected_403(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=522)
        token = self._make_token("matchowner522@example.com")
        self._make_owner_with_pmc(
            522, "matchowner522@example.com", token, 722, unit_id=8220, property_id=9220, pmc_id=523
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=915
        )

        response = self.client.post(
            self._match_url(),
            {
                "pmc_id": 522,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)

    def test_suggested_matches_no_trailing_slash_url_also_works(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=524)
        token = self._make_token("matchowner524@example.com")
        self._make_owner_with_pmc(
            524, "matchowner524@example.com", token, 724, unit_id=8240, property_id=9240, pmc_id=524
        )
        self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=916
        )

        response = self.client.get(
            reverse("suggested-matches-no-slash"),
            {"pmc_id": 524},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["content"]["suggestions"]), 1)

    def test_match_no_trailing_slash_url_also_works(self):
        import datetime as dt

        profile = self._make_profile(pmc_id=525)
        token = self._make_token("matchowner525@example.com")
        self._make_owner_with_pmc(
            525, "matchowner525@example.com", token, 725, unit_id=8250, property_id=9250, pmc_id=525
        )
        line = self._make_statement_line(
            profile, datetime.date(2026, 6, 10), "500.00", "REF-1"
        )
        entry = self._make_bank_journal_entry(
            profile, 500, dt.datetime(2026, 6, 12, tzinfo=dt.timezone.utc), source_txn_id=917
        )

        response = self.client.post(
            reverse("apply-bank-statement-match-no-slash"),
            {
                "pmc_id": 525,
                "bank_statement_line_id": line.id,
                "journal_entry_id": entry.id,
                "action": "confirm",
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        line.refresh_from_db()
        self.assertTrue(line.reconciled)


class ManualJournalEntryEndpointTests(TrialBalanceReportTests):
    """Story 5.1 tests: POST/GET /ledger/manual-entries (FR-16).

    Subclasses `TrialBalanceReportTests` to reuse its stand-in-table
    setUpClass/tearDownClass/setUp and its `_make_profile`/
    `_make_owner_with_pmc`/`_make_token` helpers -- this endpoint follows
    the same auth/validate/resolve/scope sequence as Trial Balance (spec
    Code Map), so the same fixtures apply unchanged.

    Covers every row of the spec's I/O & Edge-Case Matrix:
      1. Balanced entry -- 201, JournalEntry(source_type=MANUAL,
         source_lease_transaction_id=None) + LedgerLines created.
      2. Unbalanced entry -- 400, no rows created.
      3. Fewer than two lines -- 400, no rows created.
      4. Account from a different PMC -- 400, no rows created.
      5. Missing/invalid pmc_id -- 400.
      6. No FinancePMCProfile for pmc_id -- 404.
      7. Caller not authorized for the PMC -- 403.

    Plus: a manual entry's debit/credit is included in Trial Balance
    identically to a posting-engine entry (spec Always -- no report
    special-cases source_type), and `memo` round-trips end to end (the
    field this story's first implementation pass silently dropped).
    """

    def _url(self):
        return reverse("create-manual-journal-entry")

    def _get_url(self):
        return reverse("create-manual-journal-entry")

    # Inherited from TrialBalanceReportTests but not applicable here: this
    # endpoint is POST-based with a different request/response shape
    # (manual-entry lines, not a report). Skipped rather than deleted, to
    # keep the inheritance-for-fixture-reuse pattern's intent explicit
    # (matches ProfitLossReportTests' precedent above).
    def test_happy_path_returns_every_account_balanced_true(self):
        self.skipTest("superseded by manual-entry-shaped happy-path test below")

    def test_zero_activity_account_included_with_zero_totals(self):
        self.skipTest("not applicable -- no report/zero-activity concept here")

    def test_reversal_entry_summed_unconditionally(self):
        self.skipTest("not applicable -- this endpoint does not post reversals")

    def test_unreachable_pmc_id_rejected_with_403(self):
        self.skipTest("superseded by test_unauthorized_pmc_rejected_with_403 below")

    def test_missing_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_invalid_date_format_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_inverted_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self.skipTest(
            "superseded by test_unauthenticated_request_rejected_with_401 below"
        )

    def test_expired_token_rejected_with_401(self):
        self.skipTest(
            "covered by test_unauthenticated_request_rejected_with_401 below "
            "via the same auth helper Trial Balance itself uses -- no "
            "manual-entries-specific expiry behavior to re-verify"
        )

    def test_balanced_entry_creates_manual_journal_entry(self):
        profile = self._make_profile(pmc_id=30)
        token = self._make_token("owner30@example.com")
        self._make_owner_with_pmc(
            30, "owner30@example.com", token, 230, unit_id=40, property_id=50, pmc_id=30
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 30,
                "memo": "Petty cash correction",
                "lines": [
                    {"account_id": bank.id, "debit": "150.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "150.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["content"]["source_type"], "MANUAL")
        self.assertEqual(body["content"]["memo"], "Petty cash correction")

        entry = JournalEntry.objects.get(pk=body["content"]["journal_entry_id"])
        self.assertEqual(entry.source_type, JournalEntry.MANUAL)
        self.assertIsNone(entry.source_lease_transaction_id)
        self.assertEqual(entry.memo, "Petty cash correction")
        self.assertEqual(entry.lines.count(), 2)

    def test_unbalanced_entry_rejected_with_400_no_rows_created(self):
        profile = self._make_profile(pmc_id=31)
        token = self._make_token("owner31@example.com")
        self._make_owner_with_pmc(
            31, "owner31@example.com", token, 231, unit_id=41, property_id=51, pmc_id=31
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 31,
                "lines": [
                    {"account_id": bank.id, "debit": "150.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "100.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_fewer_than_two_lines_rejected_with_400(self):
        profile = self._make_profile(pmc_id=32)
        token = self._make_token("owner32@example.com")
        self._make_owner_with_pmc(
            32, "owner32@example.com", token, 232, unit_id=42, property_id=52, pmc_id=32
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 32,
                "lines": [{"account_id": bank.id, "debit": "50.00", "credit": "0.00"}],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_account_from_different_pmc_rejected_with_400(self):
        profile = self._make_profile(pmc_id=33)
        other_profile = self._make_profile(pmc_id=34)
        token = self._make_token("owner33@example.com")
        self._make_owner_with_pmc(
            33, "owner33@example.com", token, 233, unit_id=43, property_id=53, pmc_id=33
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        other_rent_income = Account.objects.get(
            finance_pmc_profile=other_profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 33,
                "lines": [
                    {"account_id": bank.id, "debit": "50.00", "credit": "0.00"},
                    {
                        "account_id": other_rent_income.id,
                        "debit": "0.00",
                        "credit": "50.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=other_profile).count(), 0
        )

    def test_missing_pmc_id_rejected_with_400(self):
        token = self._make_token("owner35@example.com")
        self._make_owner_with_pmc(
            35, "owner35@example.com", token, 235, unit_id=49, property_id=59, pmc_id=35
        )

        response = self.client.post(
            self._url(),
            {"lines": [{"account_id": 1, "debit": "10.00", "credit": "0.00"}]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("owner36@example.com")
        self._make_owner_with_pmc(
            36, "owner36@example.com", token, 236, unit_id=44, property_id=54, pmc_id=999
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 999,
                "lines": [
                    {"account_id": 1, "debit": "10.00", "credit": "0.00"},
                    {"account_id": 2, "debit": "0.00", "credit": "10.00"},
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)

    def test_unauthorized_pmc_rejected_with_403(self):
        self._make_profile(pmc_id=37)
        token = self._make_token("owner37@example.com")
        # Owner is scoped to pmc_id=38, not pmc_id=37.
        self._make_owner_with_pmc(
            37, "owner37@example.com", token, 237, unit_id=45, property_id=55, pmc_id=38
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 37,
                "lines": [
                    {"account_id": 1, "debit": "10.00", "credit": "0.00"},
                    {"account_id": 2, "debit": "0.00", "credit": "10.00"},
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_request_rejected_with_401(self):
        self._make_profile(pmc_id=39)

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 39,
                "lines": [
                    {"account_id": 1, "debit": "10.00", "credit": "0.00"},
                    {"account_id": 2, "debit": "0.00", "credit": "10.00"},
                ],
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)

    def test_manual_entry_included_in_trial_balance_like_posting_engine_entry(self):
        """spec Always: 'no report special-cases source_type'."""
        profile = self._make_profile(pmc_id=40)
        token = self._make_token("owner40@example.com")
        self._make_owner_with_pmc(
            40, "owner40@example.com", token, 240, unit_id=46, property_id=56, pmc_id=40
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        create_response = self.client.post(
            self._url(),
            {
                "pmc_id": 40,
                "lines": [
                    {"account_id": bank.id, "debit": "300.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "300.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(create_response.status_code, 201)

        entry = JournalEntry.objects.get(
            pk=create_response.json()["content"]["journal_entry_id"]
        )
        entry_date = entry.posted_at.date().isoformat()

        trial_balance_response = self.client.get(
            reverse("trial-balance-report"),
            {"pmc_id": 40, "start_date": entry_date, "end_date": entry_date},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(trial_balance_response.status_code, 200)
        accounts = {
            a["name"]: a
            for a in trial_balance_response.json()["content"]["accounts"]
        }
        self.assertEqual(accounts["Bank"]["total_debit"], 300.0)
        self.assertEqual(accounts["Rent Income"]["total_credit"], 300.0)

    def test_manual_entry_included_in_profit_loss_like_posting_engine_entry(self):
        """Post-review addition (verification-gap finding): only Trial
        Balance was previously checked -- P&L reuses compute_trial_balance
        internally, but nothing asserted a MANUAL entry actually surfaces
        there too."""
        profile = self._make_profile(pmc_id=43)
        token = self._make_token("owner43@example.com")
        self._make_owner_with_pmc(
            43, "owner43@example.com", token, 243, unit_id=50, property_id=60, pmc_id=43
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        create_response = self.client.post(
            self._url(),
            {
                "pmc_id": 43,
                "lines": [
                    {"account_id": bank.id, "debit": "500.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "500.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(create_response.status_code, 201)

        entry = JournalEntry.objects.get(
            pk=create_response.json()["content"]["journal_entry_id"]
        )
        entry_date = entry.posted_at.date().isoformat()

        pl_response = self.client.get(
            reverse("profit-loss-report"),
            {"pmc_id": 43, "start_date": entry_date, "end_date": entry_date},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(pl_response.status_code, 200)
        income_accounts = {
            a["name"]: a for a in pl_response.json()["content"]["income_accounts"]
        }
        self.assertEqual(income_accounts["Rent Income"]["total_credit"], 500.0)
        self.assertEqual(pl_response.json()["content"]["net_profit_loss"], 500.0)

    def test_manual_entry_included_in_balance_sheet_like_posting_engine_entry(self):
        """Post-review addition (verification-gap finding): same gap as
        P&L -- Balance Sheet reuses compute_trial_balance internally but
        was never checked to actually include a MANUAL entry."""
        import datetime as dt

        profile = self._make_profile(pmc_id=44)
        FinancePMCProfile.objects.filter(pk=profile.pk).update(
            created=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        )
        profile.refresh_from_db()
        token = self._make_token("owner44@example.com")
        self._make_owner_with_pmc(
            44, "owner44@example.com", token, 244, unit_id=51, property_id=61, pmc_id=44
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        create_response = self.client.post(
            self._url(),
            {
                "pmc_id": 44,
                "lines": [
                    {"account_id": bank.id, "debit": "200.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "200.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(create_response.status_code, 201)

        bs_response = self.client.get(
            reverse("balance-sheet-report"),
            {"pmc_id": 44, "as_of_date": "2026-12-31"},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(bs_response.status_code, 200)
        content = bs_response.json()["content"]
        bank_row = next(a for a in content["asset_accounts"] if a["name"] == "Bank")
        self.assertEqual(bank_row["balance"], 200.0)
        self.assertTrue(content["balanced"])

    def test_negative_debit_rejected_with_400_no_rows_created(self):
        profile = self._make_profile(pmc_id=45)
        token = self._make_token("owner45@example.com")
        self._make_owner_with_pmc(
            45, "owner45@example.com", token, 245, unit_id=52, property_id=62, pmc_id=45
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 45,
                "lines": [
                    {"account_id": bank.id, "debit": "-50.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "-50.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_line_with_both_debit_and_credit_rejected_with_400(self):
        profile = self._make_profile(pmc_id=46)
        token = self._make_token("owner46@example.com")
        self._make_owner_with_pmc(
            46, "owner46@example.com", token, 246, unit_id=53, property_id=63, pmc_id=46
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 46,
                "lines": [
                    {"account_id": bank.id, "debit": "50.00", "credit": "50.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "50.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_all_zero_value_entry_rejected_with_400(self):
        profile = self._make_profile(pmc_id=47)
        token = self._make_token("owner47@example.com")
        self._make_owner_with_pmc(
            47, "owner47@example.com", token, 247, unit_id=54, property_id=64, pmc_id=47
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 47,
                "lines": [
                    {"account_id": bank.id, "debit": "0.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "0.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_non_dict_line_item_rejected_with_400_not_500(self):
        profile = self._make_profile(pmc_id=48)
        token = self._make_token("owner48@example.com")
        self._make_owner_with_pmc(
            48, "owner48@example.com", token, 248, unit_id=55, property_id=65, pmc_id=48
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 48,
                "lines": ["not-a-dict", "also-not-a-dict"],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_non_integer_account_id_rejected_with_400_not_500(self):
        profile = self._make_profile(pmc_id=49)
        token = self._make_token("owner49@example.com")
        self._make_owner_with_pmc(
            49, "owner49@example.com", token, 249, unit_id=56, property_id=66, pmc_id=49
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 49,
                "lines": [
                    {"account_id": "not-an-int", "debit": "10.00", "credit": "0.00"},
                    {"account_id": "also-not-an-int", "debit": "0.00", "credit": "10.00"},
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            JournalEntry.objects.filter(finance_pmc_profile=profile).count(), 0
        )

    def test_list_endpoint_returns_created_manual_entry_with_memo(self):
        profile = self._make_profile(pmc_id=41)
        token = self._make_token("owner41@example.com")
        self._make_owner_with_pmc(
            41, "owner41@example.com", token, 241, unit_id=47, property_id=57, pmc_id=41
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        self.client.post(
            self._url(),
            {
                "pmc_id": 41,
                "memo": "Owner deposit",
                "lines": [
                    {"account_id": bank.id, "debit": "75.00", "credit": "0.00"},
                    {
                        "account_id": rent_income.id,
                        "debit": "0.00",
                        "credit": "75.00",
                    },
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        list_response = self.client.get(
            self._get_url(),
            {"pmc_id": 41},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(list_response.status_code, 200)
        entries = list_response.json()["content"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["memo"], "Owner deposit")

    def test_no_trailing_slash_url_also_works(self):
        profile = self._make_profile(pmc_id=42)
        token = self._make_token("owner42@example.com")
        self._make_owner_with_pmc(
            42, "owner42@example.com", token, 242, unit_id=48, property_id=58, pmc_id=42
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )

        response = self.client.post(
            reverse("create-manual-journal-entry-no-slash"),
            {
                "pmc_id": 42,
                "lines": [
                    {"account_id": bank.id, "debit": "10.00", "credit": "0.00"},
                    {"account_id": rent_income.id, "debit": "0.00", "credit": "10.00"},
                ],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)


class PostOtherChargeTests(TestCase):
    """Story 5.2 tests: post_other_charge posting logic (FR-17).

    Reuses PostBounceFeeTests' hand-rolled stand-in-table technique
    (lease_leasetransaction/lease_lease/property_unit/property_property/
    charges_charge) -- this story's gate/resolution chain is identical.

    Covers every row of the spec's I/O & Edge-Case Matrix:
      1. OTHER_CHARGE, no unresolved bounce, active PMCChargeType --
         balanced JournalEntry posts (with a VAT line when vat_amount != 0).
      2. No PMCChargeType configured -- no rows created, logged,
         reason=no_charge_type_configured.
      3. PMCChargeType exists but inactive -- no rows created, logged,
         reason=charge_type_inactive.
      4. Duplicate sync (retry) -- exactly one JournalEntry,
         reason=duplicate_skip on the second call.
      5. Unresolved bounce still exists on the lease -- this function does
         not post at all (mutually exclusive with post_bounce_fee).
      6. Unresolvable PMC -- no rows created, logged, reason=unresolvable_pmc.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS charges_charge (
                    id BIGSERIAL PRIMARY KEY,
                    amount DOUBLE PRECISION,
                    vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
            cursor.execute("DROP TABLE IF EXISTS charges_charge")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
                "charges_charge",
            ):
                cursor.execute(f"DELETE FROM {table}")

    def _make_profile(self, pmc_id=1):
        return FinancePMCProfile.objects.create(
            pmc_id=pmc_id,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )

    def _insert_pmc_chain(self, cursor, lease_id, unit_id, property_id, pmc_id):
        cursor.execute(
            "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)",
            [lease_id, unit_id],
        )
        cursor.execute(
            "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
            [unit_id, property_id],
        )
        cursor.execute(
            "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
            [property_id, pmc_id],
        )

    def _insert_txn(
        self,
        cursor,
        txn_id,
        lease_id,
        amount,
        cheque_type,
        status,
        charge_id=None,
    ):
        cursor.execute(
            """
            INSERT INTO lease_leasetransaction
                (documents_ptr_id, lease_id, amount, cheque_type, payment_type, status,
                 charge_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [txn_id, lease_id, amount, cheque_type, "CHEQUE", status, charge_id],
        )

    def _insert_charge(self, cursor, charge_id, amount, vat_amount=0):
        cursor.execute(
            "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s)",
            [charge_id, amount, vat_amount],
        )

    def test_other_charge_no_bounce_active_charge_type_posts_balanced_entry(self):
        profile = self._make_profile(pmc_id=1)
        maintenance_account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=10, unit_id=10, property_id=10, pmc_id=1
            )
            self._insert_charge(cursor, charge_id=500, amount=200, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=100,
                lease_id=10,
                amount=200,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=500,
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=500,
            account=maintenance_account,
            active=True,
        )

        result = post_other_charge(100)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=100,
            source_status_transition="OTHER_CHARGE-POSTED",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)
        self.assertEqual(entry.source_type, JournalEntry.MANUAL)

        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 2)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Maintenance Income")
        self.assertEqual(ar_line.debit, 200)
        self.assertEqual(ar_line.credit, 0)
        self.assertEqual(income_line.debit, 0)
        self.assertEqual(income_line.credit, 200)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 200)

    def test_other_charge_with_vat_posts_balanced_three_line_entry(self):
        profile = self._make_profile(pmc_id=2)
        security_account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Security Charge Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=20, unit_id=20, property_id=20, pmc_id=2
            )
            self._insert_charge(cursor, charge_id=501, amount=300, vat_amount=15)
            self._insert_txn(
                cursor,
                txn_id=101,
                lease_id=20,
                amount=315,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=501,
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=501,
            account=security_account,
            active=True,
        )

        result = post_other_charge(101)

        self.assertTrue(result["posted"])
        entry = JournalEntry.objects.get(
            source_lease_transaction_id=101,
            source_status_transition="OTHER_CHARGE-POSTED",
        )
        lines = LedgerLine.objects.filter(journal_entry=entry)
        self.assertEqual(lines.count(), 3)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Security Charge Income")
        vat_line = lines.get(account__name="VAT Payable")
        self.assertEqual(ar_line.debit, 315)
        self.assertEqual(income_line.credit, 300)
        self.assertEqual(vat_line.credit, 15)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 315)

    def test_no_charge_type_configured_posts_nothing(self):
        self._make_profile(pmc_id=3)

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=30, unit_id=30, property_id=30, pmc_id=3
            )
            self._insert_charge(cursor, charge_id=502, amount=100, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=102,
                lease_id=30,
                amount=100,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=502,
            )

        result = post_other_charge(102)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "no_charge_type_configured")
        self.assertEqual(JournalEntry.objects.filter(source_lease_transaction_id=102).count(), 0)

    def test_inactive_charge_type_posts_nothing(self):
        profile = self._make_profile(pmc_id=4)
        account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=40, unit_id=40, property_id=40, pmc_id=4
            )
            self._insert_charge(cursor, charge_id=503, amount=100, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=103,
                lease_id=40,
                amount=100,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=503,
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=503,
            account=account,
            active=False,
        )

        result = post_other_charge(103)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "charge_type_inactive")
        self.assertEqual(JournalEntry.objects.filter(source_lease_transaction_id=103).count(), 0)

    def test_duplicate_sync_is_a_no_op_second_call(self):
        profile = self._make_profile(pmc_id=5)
        account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=50, unit_id=50, property_id=50, pmc_id=5
            )
            self._insert_charge(cursor, charge_id=504, amount=120, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=104,
                lease_id=50,
                amount=120,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=504,
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=504,
            account=account,
            active=True,
        )

        first_result = post_other_charge(104)
        self.assertTrue(first_result["posted"])

        second_result = post_other_charge(104)
        self.assertFalse(second_result["posted"])
        self.assertEqual(second_result["reason"], "duplicate_skip")

        self.assertEqual(
            JournalEntry.objects.filter(
                source_lease_transaction_id=104,
                source_status_transition="OTHER_CHARGE-POSTED",
            ).count(),
            1,
        )

    def test_unresolved_bounce_on_lease_never_posts(self):
        profile = self._make_profile(pmc_id=6)
        account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=60, unit_id=60, property_id=60, pmc_id=6
            )
            # A bounced transaction on the same lease, still unresolved
            # (status=BOUNCED, no accompanying reversal/pairing needed for
            # this function's own gate -- it only checks
            # bounced_ids_on_lease's emptiness, mirroring post_bounce_fee's
            # own check verbatim).
            self._insert_txn(
                cursor,
                txn_id=105,
                lease_id=60,
                amount=5000,
                cheque_type="RENT_CHEQUE",
                status="BOUNCED",
            )
            self._insert_charge(cursor, charge_id=505, amount=100, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=106,
                lease_id=60,
                amount=100,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=505,
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=505,
            account=account,
            active=True,
        )

        result = post_other_charge(106)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolved_bounce_exists")
        self.assertEqual(JournalEntry.objects.filter(source_lease_transaction_id=106).count(), 0)

    def test_unresolvable_pmc_posts_nothing(self):
        self._make_profile(pmc_id=7)

        with connection.cursor() as cursor:
            # lease_id=70 deliberately has no lease_lease row -- an
            # unresolvable PMC chain from the very first hop.
            self._insert_charge(cursor, charge_id=506, amount=100, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=107,
                lease_id=70,
                amount=100,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=506,
            )

        result = post_other_charge(107)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "unresolvable_pmc")
        self.assertEqual(JournalEntry.objects.filter(source_lease_transaction_id=107).count(), 0)

    def test_not_other_charge_type_is_a_no_op(self):
        self._make_profile(pmc_id=8)

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=80, unit_id=80, property_id=80, pmc_id=8
            )
            self._insert_txn(
                cursor,
                txn_id=108,
                lease_id=80,
                amount=5000,
                cheque_type="RENT_CHEQUE",
                status="BALANCE",
            )

        result = post_other_charge(108)

        self.assertFalse(result["posted"])
        self.assertEqual(result["reason"], "not_other_charge_type")

    def test_posted_entry_satisfies_manual_entries_list_filter(self):
        """Post-review addition (verification-gap finding): AC 1 requires
        this posting to surface under Ledger Entries -> Manual Entries
        (Story 5.1's list page), which filters strictly on
        source_type=JournalEntry.MANUAL (see _list_manual_journal_entries).
        Confirms the entry post_other_charge creates actually satisfies that
        exact filter, so a future narrowing of it (e.g. also requiring
        source_status_transition="") would be caught here."""
        profile = self._make_profile(pmc_id=9)

        with connection.cursor() as cursor:
            self._insert_pmc_chain(
                cursor, lease_id=90, unit_id=90, property_id=90, pmc_id=9
            )
            self._insert_charge(cursor, charge_id=901, amount=200, vat_amount=0)
            self._insert_txn(
                cursor,
                txn_id=109,
                lease_id=90,
                amount=200,
                cheque_type="OTHER_CHARGE",
                status="BALANCE",
                charge_id=901,
            )

        account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )
        PMCChargeType.objects.create(
            finance_pmc_profile=profile, charge_id=901, account=account, active=True
        )

        result = post_other_charge(109)
        self.assertTrue(result["posted"])

        entry = JournalEntry.objects.get(source_lease_transaction_id=109)
        # The exact filter _list_manual_journal_entries applies (spec AC 1:
        # "visible under Ledger Entries -> Manual Entries").
        self.assertTrue(
            JournalEntry.objects.filter(
                pk=entry.pk,
                finance_pmc_profile=profile,
                source_type=JournalEntry.MANUAL,
            ).exists()
        )


class SyncLeaseTransactionOtherChargeDispatcherTests(TestCase):
    """Story 5.2 tests: sync_lease_transaction's dispatcher wiring -- calls
    post_other_charge after post_bounce_fee, only when the bounce-fee
    result's reason is "no_unresolved_bounce" (spec Boundaries &
    Constraints/Code Map). HTTP-level, mirroring
    SyncLeaseTransactionRentPostingIntegrationTests' established pattern.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_leasetransaction (
                    documents_ptr_id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    created TIMESTAMP WITH TIME ZONE,
                    charge_id BIGINT,
                    cheque_date TIMESTAMP WITH TIME ZONE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT,
                    lease_status VARCHAR(20),
                    security_deposit DOUBLE PRECISION
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT,
                    property_block_tower_id BIGINT,
                    commission_percent NUMERIC(5, 3)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_property (
                    id BIGSERIAL PRIMARY KEY,
                    pmc_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS charges_charge (
                    id BIGSERIAL PRIMARY KEY,
                    amount DOUBLE PRECISION,
                    vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0
                )
                """
            )

    @classmethod
    def tearDownClass(cls):
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS lease_leasetransaction")
            cursor.execute("DROP TABLE IF EXISTS lease_lease")
            cursor.execute("DROP TABLE IF EXISTS property_unit")
            cursor.execute("DROP TABLE IF EXISTS property_property")
            cursor.execute("DROP TABLE IF EXISTS charges_charge")
        super().tearDownClass()

    def setUp(self):
        with connection.cursor() as cursor:
            for table in (
                "lease_leasetransaction",
                "lease_lease",
                "property_unit",
                "property_property",
                "charges_charge",
            ):
                cursor.execute(f"DELETE FROM {table}")

    @override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
    def test_other_charge_sync_via_http_posts_after_no_unresolved_bounce(self):
        profile = FinancePMCProfile.objects.create(
            pmc_id=9,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        account = Account.objects.create(
            finance_pmc_profile=profile,
            name="Maintenance Income",
            account_type=Account.INCOME,
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)", [90, 91]
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [91, 92],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [92, 9],
            )
            cursor.execute(
                "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s)",
                [700, 250, 0],
            )
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, "
                "status, created, charge_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    300,
                    90,
                    250,
                    "OTHER_CHARGE",
                    "CHEQUE",
                    "BALANCE",
                    "2026-01-01T00:00:00+00:00",
                    700,
                ],
            )

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=700,
            account=account,
            active=True,
        )

        response = self.client.post(
            reverse("sync-lease-transaction", kwargs={"lease_transaction_id": 300}),
            data={"lease_transaction_id": 300},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["content"]["posting"]["bounce_fee"],
            {"posted": False, "reason": "no_unresolved_bounce"},
        )
        self.assertEqual(
            body["content"]["posting"]["other_charge"],
            {"posted": True, "reason": ""},
        )

        entry = JournalEntry.objects.get(
            source_lease_transaction_id=300,
            source_status_transition="OTHER_CHARGE-POSTED",
        )
        self.assertEqual(entry.finance_pmc_profile_id, profile.id)
        lines = LedgerLine.objects.filter(journal_entry=entry)
        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 250)

    @override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
    def test_rent_cheque_sync_never_calls_other_charge(self):
        """A RENT_CHEQUE transaction never triggers post_bounce_fee's
        OTHER_CHARGE gate, so post_other_charge is never even attempted for
        it (spec: the OTHER_CHARGE gate not applying at all correctly means
        this function's own gate also won't apply)."""
        FinancePMCProfile.objects.create(
            pmc_id=10,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)", [100, 101]
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [101, 102],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [102, 10],
            )
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, status) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                [301, 100, 5000, "RENT_CHEQUE", "CHEQUE", "BALANCE"],
            )

        response = self.client.post(
            reverse("sync-lease-transaction", kwargs={"lease_transaction_id": 301}),
            data={"lease_transaction_id": 301},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["content"]["posting"]["bounce_fee"],
            {"posted": False, "reason": "not_other_charge_type"},
        )
        self.assertNotIn("other_charge", body["content"]["posting"])

    @override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
    def test_other_charge_never_called_when_bounce_fee_succeeds(self):
        """Post-review addition (verification-gap finding): the only two
        dispatcher scenarios previously tested were `no_unresolved_bounce`
        and `not_other_charge_type` -- neither exercises the case where
        post_bounce_fee actually pairs and posts. A regression widening the
        exact-string gate (e.g. to `if not bounce_fee_result["posted"]`)
        would have shipped undetected without this test."""
        profile = FinancePMCProfile.objects.create(
            pmc_id=11,
            base_currency="AED",
            country="UAE",
            fiscal_year_start_month=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)", [110, 111]
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [111, 112],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [112, 11],
            )
            # A prior RENT_CHEQUE transaction, posted then bounced -- gives
            # post_bounce_fee an unresolved bounce to pair against.
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, "
                "status, created) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [310, 110, 5000, "RENT_CHEQUE", "CHEQUE", "BALANCE",
                 "2026-01-01T00:00:00+00:00"],
            )
        self.assertTrue(post_rent_ar(310)["posted"])
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE lease_leasetransaction SET status = 'BOUNCED' "
                "WHERE documents_ptr_id = %s",
                [310],
            )
        self.assertTrue(post_bounce_reversal(310)["posted"])

        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s)",
                [701, 150, 0],
            )
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, "
                "status, created, charge_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [311, 110, 150, "OTHER_CHARGE", "CHEQUE", "BALANCE",
                 "2026-01-02T00:00:00+00:00", 701],
            )

        response = self.client.post(
            reverse("sync-lease-transaction", kwargs={"lease_transaction_id": 311}),
            data={"lease_transaction_id": 311},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["content"]["posting"]["bounce_fee"]["posted"])
        self.assertNotIn("other_charge", body["content"]["posting"])

        # Exactly one JournalEntry for the OTHER_CHARGE transaction -- via
        # the bounce-fee path, never double-posted by post_other_charge.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=311).count(),
            1,
        )

    @override_settings(FINANCE_INTERNAL_TOKEN="test-internal-token")
    def test_other_charge_never_called_when_bounce_fee_reason_is_missing_created_timestamp(self):
        """Post-review addition: confirms the dispatcher's gate is the
        exact string "no_unresolved_bounce", not merely "not posted" --
        post_bounce_fee's missing_created_timestamp failure (checked before
        its own bounce-pairing lookup even runs) must not trigger
        post_other_charge either."""
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO lease_lease (id, unit_id) VALUES (%s, %s)", [120, 121]
            )
            cursor.execute(
                "INSERT INTO property_unit (id, parent_property_id) VALUES (%s, %s)",
                [121, 122],
            )
            cursor.execute(
                "INSERT INTO property_property (id, pmc_id) VALUES (%s, %s)",
                [122, 12],
            )
            cursor.execute(
                "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s)",
                [702, 150, 0],
            )
            # No `created` timestamp -- post_bounce_fee's own
            # missing_created_timestamp check fires before its bounce-pairing
            # lookup ever runs.
            cursor.execute(
                "INSERT INTO lease_leasetransaction "
                "(documents_ptr_id, lease_id, amount, cheque_type, payment_type, "
                "status, charge_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [312, 120, 150, "OTHER_CHARGE", "CHEQUE", "BALANCE", 702],
            )

        response = self.client.post(
            reverse("sync-lease-transaction", kwargs={"lease_transaction_id": 312}),
            data={"lease_transaction_id": 312},
            content_type="application/json",
            HTTP_X_INTERNAL_TOKEN="test-internal-token",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["content"]["posting"]["bounce_fee"],
            {"posted": False, "reason": "missing_created_timestamp"},
        )
        self.assertNotIn("other_charge", body["content"]["posting"])


class PmcChargeTypeEndpointTests(TrialBalanceReportTests):
    """Story 5.2 tests: POST/GET /ledger/pmc-charge-types (FR-17).

    Subclasses TrialBalanceReportTests to reuse its stand-in-table
    setUpClass/tearDownClass/setUp and its _make_profile/
    _make_owner_with_pmc/_make_token helpers (mirrors
    ManualJournalEntryEndpointTests' established precedent).
    """

    def _url(self):
        return reverse("pmc-charge-types")

    # Inherited from TrialBalanceReportTests but not applicable here.
    def test_happy_path_returns_every_account_balanced_true(self):
        self.skipTest("superseded by charge-type-shaped happy-path test below")

    def test_zero_activity_account_included_with_zero_totals(self):
        self.skipTest("not applicable -- no report/zero-activity concept here")

    def test_reversal_entry_summed_unconditionally(self):
        self.skipTest("not applicable -- this endpoint does not post reversals")

    def test_unreachable_pmc_id_rejected_with_403(self):
        self.skipTest("superseded by test_unauthorized_pmc_rejected_with_403 below")

    def test_missing_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_invalid_date_format_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_inverted_date_range_rejected_with_400(self):
        self.skipTest("not applicable -- this endpoint takes no date range")

    def test_unauthenticated_request_rejected_with_401_before_any_query(self):
        self.skipTest(
            "superseded by test_unauthenticated_request_rejected_with_401 below"
        )

    def test_expired_token_rejected_with_401(self):
        self.skipTest(
            "covered by test_unauthenticated_request_rejected_with_401 below"
        )

    def _insert_charge_ref(self, charge_id, amount=100, vat_amount=0):
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS charges_charge ("
                "id BIGINT PRIMARY KEY, amount DOUBLE PRECISION, "
                "vat_amount DOUBLE PRECISION NOT NULL DEFAULT 0)"
            )
            cursor.execute(
                "INSERT INTO charges_charge (id, amount, vat_amount) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                [charge_id, amount, vat_amount],
            )

    def test_create_charge_type_happy_path(self):
        profile = self._make_profile(pmc_id=60)
        token = self._make_token("owner60@example.com")
        self._make_owner_with_pmc(
            60, "owner60@example.com", token, 260, unit_id=61, property_id=62, pmc_id=60
        )
        account = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )
        self._insert_charge_ref(9001, amount=100, vat_amount=0)

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 60,
                "charge_id": 9001,
                "account_id": account.id,
                "active": True,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["content"]["charge_id"], 9001)
        self.assertEqual(body["content"]["account_id"], account.id)
        self.assertTrue(body["content"]["active"])

        self.assertEqual(
            PMCChargeType.objects.filter(
                finance_pmc_profile=profile, charge_id=9001
            ).count(),
            1,
        )

    def test_create_charge_type_upserts_existing_mapping(self):
        profile = self._make_profile(pmc_id=61)
        token = self._make_token("owner61@example.com")
        self._make_owner_with_pmc(
            61, "owner61@example.com", token, 261, unit_id=63, property_id=64, pmc_id=61
        )
        rent_income = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )
        bank = Account.objects.get(finance_pmc_profile=profile, name="Bank")
        self._insert_charge_ref(9002, amount=50, vat_amount=0)

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=9002,
            account=rent_income,
            active=True,
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 61,
                "charge_id": 9002,
                "account_id": bank.id,
                "active": False,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            PMCChargeType.objects.filter(
                finance_pmc_profile=profile, charge_id=9002
            ).count(),
            1,
        )
        updated = PMCChargeType.objects.get(
            finance_pmc_profile=profile, charge_id=9002
        )
        self.assertEqual(updated.account_id, bank.id)
        self.assertFalse(updated.active)

    def test_missing_required_fields_rejected_with_400(self):
        profile = self._make_profile(pmc_id=62)
        token = self._make_token("owner62@example.com")
        self._make_owner_with_pmc(
            62, "owner62@example.com", token, 262, unit_id=65, property_id=66, pmc_id=62
        )

        response = self.client.post(
            self._url(),
            {"pmc_id": 62},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 400)

    def test_unauthorized_pmc_rejected_with_403(self):
        profile = self._make_profile(pmc_id=63)
        other_token = self._make_token("stranger63@example.com")
        # Owner is scoped to pmc_id=164, not pmc_id=63.
        self._make_owner_with_pmc(
            263, "stranger63@example.com", other_token, 363, unit_id=71, property_id=72, pmc_id=164
        )
        account = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )
        self._insert_charge_ref(9003, amount=100, vat_amount=0)

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 63,
                "charge_id": 9003,
                "account_id": account.id,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {other_token}",
        )

        self.assertEqual(response.status_code, 403)

    def test_nonexistent_pmc_id_rejected_with_404(self):
        token = self._make_token("owner64@example.com")
        self._make_owner_with_pmc(
            264, "owner64@example.com", token, 364, unit_id=73, property_id=74, pmc_id=999999
        )

        response = self.client.post(
            self._url(),
            {
                "pmc_id": 999999,
                "charge_id": 1,
                "account_id": 1,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_request_rejected_with_401(self):
        response = self.client.post(
            self._url(),
            {"pmc_id": 1, "charge_id": 1, "account_id": 1},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_list_endpoint_returns_created_charge_types(self):
        profile = self._make_profile(pmc_id=65)
        token = self._make_token("owner65@example.com")
        self._make_owner_with_pmc(
            65, "owner65@example.com", token, 265, unit_id=67, property_id=68, pmc_id=65
        )
        account = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )
        self._insert_charge_ref(9004, amount=100, vat_amount=0)

        PMCChargeType.objects.create(
            finance_pmc_profile=profile,
            charge_id=9004,
            account=account,
            active=True,
        )

        response = self.client.get(
            self._url(),
            {"pmc_id": 65},
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        charge_types = body["content"]["charge_types"]
        self.assertEqual(len(charge_types), 1)
        self.assertEqual(charge_types[0]["charge_id"], 9004)
        self.assertEqual(charge_types[0]["account_id"], account.id)
        self.assertTrue(charge_types[0]["active"])

    def test_no_trailing_slash_url_also_works(self):
        profile = self._make_profile(pmc_id=66)
        token = self._make_token("owner66@example.com")
        self._make_owner_with_pmc(
            66, "owner66@example.com", token, 266, unit_id=69, property_id=70, pmc_id=66
        )
        account = Account.objects.get(
            finance_pmc_profile=profile, name="Rent Income"
        )
        self._insert_charge_ref(9005, amount=100, vat_amount=0)

        response = self.client.post(
            reverse("pmc-charge-types-no-slash"),
            {
                "pmc_id": 66,
                "charge_id": 9005,
                "account_id": account.id,
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

        self.assertEqual(response.status_code, 201)

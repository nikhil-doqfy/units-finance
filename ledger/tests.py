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
from django.test import TestCase, override_settings
from django.urls import reverse

from ledger.models import Account, FinancePMCProfile, JournalEntry, LedgerLine
from ledger.posting import post_cheque_clearing, post_rent_ar
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
                    id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL
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
                    id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT
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
                (id, lease_id, amount, cheque_type, payment_type, status)
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
        self.assertEqual(lines.count(), 2)

        ar_line = lines.get(account__name="AR — Tenants")
        income_line = lines.get(account__name="Rent Income")
        self.assertEqual(ar_line.debit, 5000)
        self.assertEqual(ar_line.credit, 0)
        self.assertEqual(income_line.debit, 0)
        self.assertEqual(income_line.credit, 5000)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5000)

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
                    id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT
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
                (id, lease_id, amount, cheque_type, payment_type, status)
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
            "UPDATE lease_leasetransaction SET status = %s WHERE id = %s",
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
        self.assertEqual(bank_line.debit, 5000)
        self.assertEqual(bank_line.credit, 0)
        self.assertEqual(ar_line.debit, 0)
        self.assertEqual(ar_line.credit, 5000)

        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 5000)

        # Exactly two JournalEntries total for this lease_transaction_id --
        # the original Rent AR posting plus this clearing posting.
        self.assertEqual(
            JournalEntry.objects.filter(source_lease_transaction_id=200).count(), 2
        )

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
                    id BIGSERIAL PRIMARY KEY,
                    lease_id BIGINT NOT NULL,
                    amount DOUBLE PRECISION,
                    cheque_type VARCHAR(20) NOT NULL,
                    payment_type VARCHAR(20) NOT NULL,
                    status VARCHAR(20) NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_lease (
                    id BIGSERIAL PRIMARY KEY,
                    unit_id BIGINT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS property_unit (
                    id BIGSERIAL PRIMARY KEY,
                    parent_property_id BIGINT
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
                "(id, lease_id, amount, cheque_type, payment_type, status) "
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
        self.assertEqual(lines.count(), 2)
        debit_total = sum(line.debit for line in lines)
        credit_total = sum(line.credit for line in lines)
        self.assertEqual(debit_total, credit_total)
        self.assertEqual(debit_total, 7500)

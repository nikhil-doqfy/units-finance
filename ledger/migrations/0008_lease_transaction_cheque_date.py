# Generated for Story 3.5 (spec-3-5-ageing-report): adds `cheque_date` to
# the existing unmanaged LeaseTransactionRef. A no-op against the real
# database -- `managed = False` models never get their table/column
# created/altered by Django migrations; this migration exists only so
# Django's migration graph/state stays consistent with models.py (same
# pattern as 0005/0007's unmanaged-ref field additions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0007_reporting_auth_refs'),
    ]

    operations = [
        migrations.AddField(
            model_name='leasetransactionref',
            name='cheque_date',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="units-backend LeaseTransaction.cheque_date -- "
                "Story 3.5's Ageing report days-overdue basis; normalize to "
                ".date() before subtracting from today (spec Boundaries & "
                "Constraints).",
            ),
        ),
    ]

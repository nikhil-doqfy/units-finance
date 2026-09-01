# Generated for Story 2.5: adds the unmanaged ChargeRef model and two new
# fields (created, charge_id) to the existing unmanaged LeaseTransactionRef.
# Both are no-ops against the real database -- `managed = False` models
# never get their table created/altered by Django migrations; this
# migration exists only so Django's migration graph/state stays consistent
# with models.py (same pattern as the prior unmanaged ref additions in
# 0003_journal_entry_ledger_line.py).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0003_journal_entry_ledger_line'),
    ]

    operations = [
        migrations.AddField(
            model_name='leasetransactionref',
            name='created',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="units-backend LeaseTransaction.created — drives Story "
                "2.5's nearest-neighbor OTHER_CHARGE/bounce pairing query.",
            ),
        ),
        migrations.AddField(
            model_name='leasetransactionref',
            name='charge_id',
            field=models.BigIntegerField(
                blank=True,
                null=True,
                help_text="units-backend Charge.id (LeaseTransaction.charge FK) — "
                "set only when cheque_type == OTHER_CHARGE; not a cross-DB FK "
                "(AD-19 precedent).",
            ),
        ),
        migrations.CreateModel(
            name='ChargeRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.FloatField()),
                ('vat_amount', models.FloatField(default=0)),
            ],
            options={
                'db_table': 'charges_charge',
                'managed': False,
            },
        ),
    ]

# Generated for Story 2.6: adds two new fields (security_deposit,
# lease_status) to the existing unmanaged LeaseRef. A no-op against the real
# database -- `managed = False` models never get their table created/altered
# by Django migrations; this migration exists only so Django's migration
# graph/state stays consistent with models.py (same pattern as
# 0004_charge_ref.py's LeaseTransactionRef field additions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0004_charge_ref'),
    ]

    operations = [
        migrations.AddField(
            model_name='leaseref',
            name='security_deposit',
            field=models.FloatField(
                blank=True,
                null=True,
                help_text="units-backend Lease.security_deposit -- Story 2.6's "
                "posting amount.",
            ),
        ),
        migrations.AddField(
            model_name='leaseref',
            name='lease_status',
            field=models.CharField(
                blank=True,
                max_length=20,
                null=True,
                help_text="units-backend Lease.lease_status -- Story 2.6 posts "
                "only when 'ACTIVE'.",
            ),
        ),
    ]

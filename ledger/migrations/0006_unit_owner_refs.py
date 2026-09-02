# Generated for Story 2.7 (spec-2-7-commission-split-posting): adds
# `commission_percent` to the existing unmanaged UnitRef, and adds the new
# unmanaged UnitOwnerRef model. Both are no-ops against the real database --
# `managed = False` models never get their table created/altered by Django
# migrations; this migration exists only so Django's migration graph/state
# stays consistent with models.py (same pattern as 0004_charge_ref.py's and
# 0005_lease_ref_fields.py's unmanaged-ref additions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0005_lease_ref_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='unitref',
            name='commission_percent',
            field=models.DecimalField(
                max_digits=5,
                decimal_places=3,
                blank=True,
                null=True,
                help_text="units-backend Unit.commission_percent -- Story "
                "2.7's sole commission source (spec Never: Lease.commission "
                "is not read).",
            ),
        ),
        migrations.CreateModel(
            name='UnitOwnerRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('unit_id', models.BigIntegerField(help_text="units-backend Unit.id — not a cross-DB FK (AD-19 precedent).")),
                ('ownership_percent', models.DecimalField(
                    max_digits=5,
                    decimal_places=2,
                    help_text="units-backend UnitOwner.ownership_percent -- "
                    "AD-15's sum-to-100 invariant, validated (not itself "
                    "posted) by post_commission_split.",
                )),
            ],
            options={
                'db_table': 'property_unitowner',
                'managed': False,
            },
        ),
    ]

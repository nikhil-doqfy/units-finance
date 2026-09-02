# Generated for Story 3.1 (spec-3-1-reporting-auth-pmc-scoping): adds the
# unmanaged reference models the reporting-auth + PMC-scoping foundation
# needs (UserProfileRef, AuthUserRef, PropertyManagerRef, OwnerRef,
# PMCPMMappingRef, PropertyBlocksRef), and extends the existing UnitRef /
# UnitOwnerRef with the fields the Owner branch's dual join path requires.
# All of this is a no-op against the real database -- `managed = False`
# models/fields never get their table/column created or altered by Django
# migrations; this migration exists only so Django's migration graph/state
# stays consistent with models.py (same pattern as 0004-0006's unmanaged-ref
# additions).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0006_unit_owner_refs'),
    ]

    operations = [
        migrations.AddField(
            model_name='unitref',
            name='property_block_tower_id',
            field=models.BigIntegerField(
                blank=True,
                null=True,
                help_text="units-backend PropertyBlocks.id — not a cross-DB "
                "FK; the alternate Owner-branch join path to a Unit's PMC "
                "(Story 3.1).",
            ),
        ),
        migrations.AddField(
            model_name='unitownerref',
            name='owner_id',
            field=models.BigIntegerField(
                blank=True,
                null=True,
                help_text="units-backend Owner.id (UnitOwner.owner_id) — "
                "not a cross-DB FK; joins back to OwnerRef for PMC-scoping "
                "(Story 3.1).",
            ),
        ),
        migrations.CreateModel(
            name='UserProfileRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=255, blank=True, null=True)),
                ('token', models.TextField(blank=True, null=True)),
                ('user_id', models.BigIntegerField(help_text="units-backend auth_user.id (Django's User model) — the FK column backing UserProfile.user_id; not a cross-DB FK.")),
            ],
            options={
                'db_table': 'user_service_userprofile',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='AuthUserRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=255, blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
            ],
            options={
                'db_table': 'auth_user',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='PropertyManagerRef',
            fields=[
                ('userprofile_ptr_id', models.BigIntegerField(primary_key=True, serialize=False, db_column='userprofile_ptr_id', help_text="units-backend UserProfile.id, via PropertyManager's multi-table-inheritance parent link (userprofile_ptr_id) — not a cross-DB FK.")),
                ('company_id', models.BigIntegerField(blank=True, null=True, help_text="units-backend PropertyManagmentCompany.id (PropertyManager.company_id) — the PMCPMMapping fallback (AD-19 precedent); may be null.")),
            ],
            options={
                'db_table': 'user_service_propertymanager',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='OwnerRef',
            fields=[
                ('userprofile_ptr_id', models.BigIntegerField(primary_key=True, serialize=False, db_column='userprofile_ptr_id', help_text="units-backend UserProfile.id, via Owner's multi-table-inheritance parent link (userprofile_ptr_id) — not a cross-DB FK.")),
            ],
            options={
                'db_table': 'user_service_owner',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='PMCPMMappingRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pmc_id', models.BigIntegerField(help_text="units-backend PropertyManagmentCompany.id (PMCPMMapping.pmc_id) — not a cross-DB FK.")),
                ('pm_id', models.BigIntegerField(help_text="units-backend PropertyManager.id (PMCPMMapping.pm_id) — not a cross-DB FK.")),
            ],
            options={
                'db_table': 'property_pmcpmmapping',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='PropertyBlocksRef',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('property_id', models.BigIntegerField(help_text="units-backend Property.id (PropertyBlocks.property_id) — not a cross-DB FK.")),
            ],
            options={
                'db_table': 'property_propertyblocks',
                'managed': False,
            },
        ),
    ]

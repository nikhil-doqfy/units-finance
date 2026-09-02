# Generated for Story 4.2: new Finance-owned BankStatementMatch table (spec
# Code Map -- next available migration number after Story 4.1's 0009).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0009_bank_statement_line'),
    ]

    operations = [
        migrations.CreateModel(
            name='BankStatementMatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('suggested', 'Suggested'), ('confirmed', 'Confirmed'), ('rejected', 'Rejected')], max_length=20)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('modified', models.DateTimeField(auto_now=True)),
                ('bank_statement_line', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='matches', to='ledger.bankstatementline')),
                ('journal_entry', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='statement_matches', to='ledger.journalentry')),
            ],
        ),
    ]

# Generated for Story 4.2 post-review patch: DB-level uniqueness on
# (bank_statement_line, journal_entry) -- see BankStatementMatch.Meta's
# comment in models.py. Django's makemigrations also proposed an AlterField
# on JournalEntry.reversed_journal_entry (the same pre-existing help_text-only
# drift already noted and deliberately dropped in Story 4.1's 0009 migration
# comment) -- dropped here too so this migration stays scoped to this patch.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ledger', '0010_bank_statement_match'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='bankstatementmatch',
            unique_together={('bank_statement_line', 'journal_entry')},
        ),
    ]

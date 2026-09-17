"""Give every ambassador the key that opens their own dashboard.

Written by hand rather than generated, because a unique column cannot simply
appear on a table that already has rows: a callable default is evaluated once
by the schema editor, so every existing ambassador would receive the *same*
key and the unique index would refuse it. The documented shape is three steps
— add it nullable and non-unique, fill it in row by row, then tighten it —
and that is what this is.
"""

from django.db import migrations, models

import ambassadors.models


def fill_keys(apps, schema_editor):
    Ambassador = apps.get_model('ambassadors', 'Ambassador')
    for row in Ambassador.objects.filter(dashboard_key=''):
        row.dashboard_key = ambassadors.models.new_dashboard_key()
        row.save(update_fields=['dashboard_key'])


def clear_keys(apps, schema_editor):
    """Reversing drops the column anyway; this only keeps the step symmetric
    so the migration can be rolled back without complaint."""
    Ambassador = apps.get_model('ambassadors', 'Ambassador')
    Ambassador.objects.update(dashboard_key='')


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0003_ambassadorsignup_interaction_count'),
    ]

    operations = [
        migrations.AddField(
            model_name='ambassador',
            name='dashboard_key',
            field=models.CharField(default='', max_length=64),
        ),
        migrations.RunPython(fill_keys, clear_keys),
        migrations.AlterField(
            model_name='ambassador',
            name='dashboard_key',
            field=models.CharField(
                db_index=True,
                default=ambassadors.models.new_dashboard_key,
                max_length=64,
                unique=True,
            ),
        ),
    ]

# Generated manually for event image uploads.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0002_eventcomment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='image_url',
            field=models.TextField(blank=True, default=''),
        ),
    ]

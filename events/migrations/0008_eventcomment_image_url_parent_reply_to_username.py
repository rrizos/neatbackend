# Brings EventComment up to parity with PostComment (posts/migrations/0007
# and 0018) — replies, a comment picture, and the @username shown on a reply
# to a reply — so the app's comment sheet can treat the two identically.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0007_event_ingest'),
    ]

    operations = [
        migrations.AddField(
            model_name='eventcomment',
            name='image_url',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='eventcomment',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='replies',
                to='events.eventcomment',
            ),
        ),
        migrations.AddField(
            model_name='eventcomment',
            name='reply_to_username',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
    ]

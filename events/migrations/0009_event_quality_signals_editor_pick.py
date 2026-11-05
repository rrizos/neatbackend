# The events tab used to show every published event in the city. Now each one
# carries a score (events/ranking.py), what the sources said about how popular
# it is, and an editor's override, so the tab can show the city's best.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0008_eventcomment_image_url_parent_reply_to_username'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='quality',
            field=models.PositiveSmallIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name='event',
            name='signals',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='event',
            name='editor_pick',
            field=models.BooleanField(default=False),
        ),
    ]

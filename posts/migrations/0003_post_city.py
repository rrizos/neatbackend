# Generated manually for city-based post feeds.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0002_user_likes_comments'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='city',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
    ]

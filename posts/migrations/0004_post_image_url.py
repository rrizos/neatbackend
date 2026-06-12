# Generated manually for post image uploads.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0003_post_city'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='image_url',
            field=models.TextField(blank=True, default=''),
        ),
    ]

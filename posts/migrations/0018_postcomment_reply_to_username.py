from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0017_stagedupload'),
    ]

    operations = [
        migrations.AddField(
            model_name='postcomment',
            name='reply_to_username',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
    ]

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_searchhistory'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='last_active',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

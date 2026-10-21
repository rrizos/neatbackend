from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0004_ambassador_dashboard_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='ambassador',
            name='custom_slogan',
            field=models.CharField(blank=True, default='', max_length=60),
        ),
    ]

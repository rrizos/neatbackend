import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0006_alter_ambassadorsignup_claim_method_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='ambassadorcontent',
            name='stopped_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='ambassadorcontent',
            name='stopped_by',
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to=settings.AUTH_USER_MODEL),
        ),
    ]

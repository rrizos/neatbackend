import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0007_ambassadorcontent_stopped'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='ambassadorclick',
            name='ip_address',
            field=models.CharField(blank=True, db_index=True, default='', max_length=45),
        ),
        migrations.AddField(
            model_name='ambassadorsignup',
            name='ip_address',
            field=models.CharField(blank=True, db_index=True, default='', max_length=45),
        ),
        migrations.CreateModel(
            name='SignupAddress',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ip_address', models.CharField(db_index=True, max_length=45)),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                              related_name='signup_address',
                                              to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]

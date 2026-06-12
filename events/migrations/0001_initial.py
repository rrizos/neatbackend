from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('city', models.CharField(max_length=120)),
                ('event_type', models.CharField(choices=[('official', 'Official'), ('community', 'Community')], max_length=16)),
                ('title', models.CharField(max_length=180)),
                ('description', models.TextField(blank=True, default='')),
                ('location', models.CharField(blank=True, default='', max_length=200)),
                ('image_url', models.URLField(blank=True, default='')),
                ('organizer', models.CharField(blank=True, default='', max_length=150)),
                ('has_tickets', models.BooleanField(default=False)),
                ('tickets_url', models.URLField(blank=True, default='')),
                ('attendees', models.IntegerField(default=0)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_events', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-attendees', '-created']},
        ),
        migrations.CreateModel(
            name='EventAttendance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attendance_rows', to='events.event')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='event_attendance', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [
                    models.UniqueConstraint(fields=('event', 'user'), name='unique_event_attendance'),
                ],
            },
        ),
    ]

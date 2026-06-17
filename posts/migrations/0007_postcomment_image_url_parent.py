from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('posts', '0006_commentlike'),
    ]

    operations = [
        migrations.AddField(
            model_name='postcomment',
            name='image_url',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='postcomment',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='replies',
                to='posts.postcomment',
            ),
        ),
    ]

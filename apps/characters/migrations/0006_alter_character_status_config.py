# Generated by Django 5.1.6 on 2025-02-28 03:48

import apps.characters.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("characters", "0005_alter_character_avatar_alter_character_status_config"),
    ]

    operations = [
        migrations.AlterField(
            model_name="character",
            name="status_config",
            field=models.JSONField(
                blank=True, default=apps.characters.models.get_default_status_config
            ),
        ),
    ]

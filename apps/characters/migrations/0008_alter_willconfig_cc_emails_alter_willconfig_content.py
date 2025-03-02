# Generated by Django 5.1.6 on 2025-03-02 10:46

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("characters", "0007_willconfig"),
    ]

    operations = [
        migrations.AlterField(
            model_name="willconfig",
            name="cc_emails",
            field=models.JSONField(blank=True, default=list, help_text="抄送邮箱列表"),
        ),
        migrations.AlterField(
            model_name="willconfig",
            name="content",
            field=models.TextField(
                blank=True, default="", help_text="遗嘱内容", null=True
            ),
        ),
    ]

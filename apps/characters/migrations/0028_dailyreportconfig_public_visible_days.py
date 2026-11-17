from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('characters', '0027_llm_routing'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyreportconfig',
            name='public_visible_days',
            field=models.PositiveSmallIntegerField(
                choices=[(0, '全部'), (3, '近三日'), (7, '近七日')],
                default=0,
                help_text='公开日报的时间范围，0 表示全部，近三日和近七日均包含当天',
            ),
        ),
    ]

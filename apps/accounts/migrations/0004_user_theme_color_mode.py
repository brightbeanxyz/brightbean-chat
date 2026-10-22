from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_user_language"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="theme",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="user",
            name="color_mode",
            field=models.CharField(blank=True, default="", max_length=10),
        ),
    ]

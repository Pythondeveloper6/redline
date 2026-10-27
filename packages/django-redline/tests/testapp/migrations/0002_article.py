from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("testapp", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="Article",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("title", models.CharField(max_length=50)),
                ("body", models.TextField(default="")),
                ("meta", models.JSONField(default=dict)),
                ("cover", models.BinaryField(default=b"")),
                ("views", models.IntegerField(default=0)),
            ],
        ),
    ]

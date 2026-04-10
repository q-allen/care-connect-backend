from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("doctors", "0012_doctorprofile_commission_rate"),
    ]

    operations = [
        migrations.AlterField(
            model_name="doctorprofile",
            name="city",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="City used for location-based filtering.",
                max_length=100,
            ),
        ),
    ]

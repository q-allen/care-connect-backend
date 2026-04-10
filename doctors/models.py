"""
doctors/models.py

Expanded DoctorProfile + related models aligned with NowServing.ph-style
telemedicine platform for the Philippine market.

Schedule design (mirrors NowServing.ph):
  - weekly_schedule: JSON dict of recurring hours per weekday.
    e.g. {"monday": {"start": "09:00", "end": "17:00"}, "wednesday": {...}}
    Used by the slot-generation helper to auto-produce 30-min slots when
    no explicit DoctorAvailableSlot rows exist for a given date.

  - DoctorAvailableSlot: granular per-date slots.  Takes precedence over
    weekly_schedule.  is_available=False lets a doctor block a specific slot
    (e.g. lunch break, holiday) without deleting the row.
"""

import re

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone


class PatientHMO(models.Model):
    """HMO card registered by a patient for insurance coverage during booking."""

    VERIFICATION_CHOICES = [
        ("pending",  "Pending"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ]

    patient           = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hmo_cards"
    )
    provider          = models.CharField(max_length=100, help_text="e.g. Maxicare, Medicard")
    member_id         = models.CharField(max_length=100)
    card_image        = models.ImageField(upload_to="hmo_cards/", null=True, blank=True, max_length=500)
    verification_status = models.CharField(
        max_length=10, choices=VERIFICATION_CHOICES, default="pending"
    )
    coverage_percent  = models.PositiveSmallIntegerField(
        default=0, help_text="Admin-set coverage % applied to consultation fee"
    )
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.provider} — {self.patient}"


# ── PRC license format: 7 digits (e.g. 0123456) ──────────────────────────────
prc_license_validator = RegexValidator(
    regex=r"^\d{7}$",
    message="PRC license must be exactly 7 digits.",
)


class DoctorProfile(models.Model):
    """
    Core profile for a doctor user.  Mirrors the information shown on
    NowServing.ph doctor cards: specialty, fees, location, verification badge,
    on-demand availability, and rich bio content.
    """

    # ── PH specialty choices (granular, matching NowServing categories) ───────
    SPECIALTY_CHOICES = [
        ("General Medicine", "General Medicine"),
        ("Internal Medicine", "Internal Medicine"),
        ("Pediatrics", "Pediatrics"),
        ("OB-GYN", "OB-GYN"),
        ("Dermatology", "Dermatology"),
        ("Cardiology", "Cardiology"),
        ("Neurology", "Neurology"),
        ("Orthopedics", "Orthopedics"),
        ("ENT", "ENT (Ear, Nose & Throat)"),
        ("Ophthalmology", "Ophthalmology"),
        ("Psychiatry", "Psychiatry"),
        ("Pulmonology", "Pulmonology"),
        ("Gastroenterology", "Gastroenterology"),
        ("Endocrinology", "Endocrinology"),
        ("Urology", "Urology"),
        ("Nephrology", "Nephrology"),
        ("Oncology", "Oncology"),
        ("Rheumatology", "Rheumatology"),
        ("Surgery", "Surgery"),
        ("Dentistry", "Dentistry"),
        ("Other", "Other"),
    ]

    # City is a free-text field — accepts any Philippine city/municipality

    # ── Core relation ─────────────────────────────────────────────────────────
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="doctor_profile",
    )

    # ── Identity / credentials ────────────────────────────────────────────────
    specialty = models.CharField(
        max_length=100,
        choices=SPECIALTY_CHOICES,
        db_index=True,
        help_text="Primary specialty shown prominently on the profile card.",
    )
    sub_specialties = models.JSONField(
        default=list,
        blank=True,
        help_text='List of sub-specialty strings, e.g. ["Neonatology","Adolescent Medicine"].',
    )
    prc_license = models.CharField(
        max_length=20,
        unique=True,
        validators=[prc_license_validator],
        help_text="7-digit PRC license number. Admin verifies before setting is_verified=True.",
    )
    years_of_experience = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Years of clinical practice.",
    )

    # ── Profile content ───────────────────────────────────────────────────────
    profile_photo = models.ImageField(
        upload_to="doctor_photos/",
        null=True,
        blank=True,
        max_length=500,
        help_text="Headshot shown on patient-facing cards. Builds trust (NowServing pattern).",
    )
    bio = models.TextField(
        blank=True,
        help_text="About / professional summary shown on detail page.",
    )
    languages_spoken = models.JSONField(
        default=list,
        blank=True,
        help_text='e.g. ["Filipino","English","Cebuano"]',
    )

    # ── Clinic / location ─────────────────────────────────────────────────────
    clinic_name = models.CharField(max_length=200)
    clinic_address = models.TextField(
        blank=True,
        help_text="Full street address of primary clinic.",
    )
    city = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="City used for location-based filtering.",
    )
    clinic_lat = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        help_text="Latitude of clinic pin set via Google Maps.",
    )
    clinic_lng = models.DecimalField(
        max_digits=9, decimal_places=6,
        null=True, blank=True,
        help_text="Longitude of clinic pin set via Google Maps.",
    )

    # ── Fees (PHP) ────────────────────────────────────────────────────────────
    consultation_fee_online = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Online/video consultation fee in PHP.",
    )
    consultation_fee_in_person = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="In-clinic consultation fee in PHP.",
    )

    # ── Availability / on-demand ──────────────────────────────────────────────
    is_on_demand = models.BooleanField(
        default=False,
        help_text='Enables "Talk within 15 min" badge. Requires last_active_at within 10 min.',
    )
    last_active_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Updated by heartbeat ping from doctor app. Used for Available Now logic.",
    )

    # ── Weekly recurring schedule ─────────────────────────────────────────────
    # NowServing.ph pattern: doctor sets Mon-Fri 9AM-5PM once; the system
    # auto-generates 30-min slots for any requested date that falls on those days.
    # DoctorAvailableSlot rows override this for specific dates.
    #
    # Schema: {
    #   "monday":    {"start": "09:00", "end": "17:00"},
    #   "tuesday":   {"start": "09:00", "end": "17:00"},
    #   "wednesday": {"start": "09:00", "end": "12:00"},
    #   ...
    # }
    # Omitting a weekday means the doctor is not available that day.
    weekly_schedule = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Recurring weekly hours per weekday. "
            'e.g. {"monday": {"start": "09:00", "end": "17:00"}}. '
            "Used to auto-generate 30-min slots when no explicit slot rows exist."
        ),
    )

    # ── Verification / invite ─────────────────────────────────────────────────
    is_verified = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Admin sets True after manual PRC license verification. Required for public listing.",
    )
    invite_accepted = models.BooleanField(
        default=False,
        help_text="Set True when doctor completes activation (sets password via invite link).",
    )

    # ── Profile completion (NowServing pattern: wizard gate) ──────────────────
    # False until the doctor finishes the onboarding wizard after activation.
    # Doctor dashboard redirects to /doctor/profile/complete when False.
    is_profile_complete = models.BooleanField(
        default=False,
        help_text="Set True when the doctor finishes the onboarding wizard.",
    )

    # ── Commission ────────────────────────────────────────────────────────────
    # Platform takes 15% of every completed online/on-demand consultation fee.
    # In-clinic consultations are always 0% (doctor keeps 100%).
    # Admin can override per-doctor via the admin panel.
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=15.00,
        help_text="Platform commission % deducted from online consultation fees. Default: 15%.",
    )

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Doctor Profile"
        verbose_name_plural = "Doctor Profiles"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["specialty", "city"]),
            models.Index(fields=["is_verified", "invite_accepted"]),
        ]

    def __str__(self):
        first = getattr(self.user, "first_name", "") or ""
        last = getattr(self.user, "last_name", "") or ""
        name = f"{first} {last}".strip() or self.user.email
        return f"Dr. {name} — {self.specialty}"

    @property
    def is_available_now(self) -> bool:
        """True when doctor is on-demand AND pinged within the last 10 minutes."""
        if not self.is_on_demand or not self.last_active_at:
            return False
        return (timezone.now() - self.last_active_at).total_seconds() <= 600


class DoctorAvailableSlot(models.Model):
    """
    Granular per-date availability slot for a doctor.

    Priority over weekly_schedule:
      - If explicit rows exist for a date → use only those rows.
      - If no rows exist for a date → fall back to weekly_schedule auto-generation.

    is_available=False lets a doctor block a specific slot (e.g. lunch, holiday)
    without deleting the row — the slot will appear as unavailable to patients.

    NowServing.ph alignment: doctors can set recurring weekly hours AND override
    individual dates (e.g. block Dec 25, add a Saturday slot).
    """

    doctor     = models.ForeignKey(
        DoctorProfile,
        on_delete=models.CASCADE,
        related_name="available_slots",
    )
    date       = models.DateField(db_index=True)
    start_time = models.TimeField()
    end_time   = models.TimeField()
    is_available = models.BooleanField(
        default=True,
        help_text="False = doctor blocked this slot (e.g. lunch, holiday).",
    )
    # Tracks whether this slot was auto-generated from weekly_schedule
    # (informational; not used in business logic).
    is_recurring = models.BooleanField(
        default=False,
        help_text="True when created from a recurring weekly rule.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Doctor Available Slot"
        verbose_name_plural = "Doctor Available Slots"
        ordering = ["date", "start_time"]
        # Prevent duplicate slots for the same doctor/date/time
        unique_together = ("doctor", "date", "start_time")
        indexes = [
            models.Index(fields=["doctor", "date"], name="slot_doctor_date_idx"),
        ]

    def __str__(self):
        status = "✓" if self.is_available else "✗"
        return f"{status} {self.doctor} | {self.date} {self.start_time}–{self.end_time}"


class DoctorHospital(models.Model):
    """
    Additional hospitals/clinics where the doctor practices.
    A doctor may have a primary clinic on DoctorProfile plus affiliations here.
    """

    doctor = models.ForeignKey(
        DoctorProfile,
        on_delete=models.CASCADE,
        related_name="hospitals",
    )
    name = models.CharField(max_length=200, help_text="Hospital or clinic name.")
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)

    class Meta:
        verbose_name = "Doctor Hospital"
        verbose_name_plural = "Doctor Hospitals"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.doctor})"


class DoctorService(models.Model):
    """
    Services offered by the doctor, e.g. Medical Certificate, Follow-up Consult.
    Shown as chips on the patient-facing detail page (NowServing pattern).
    """

    SERVICE_CHOICES = [
        ("Medical Certificate", "Medical Certificate"),
        ("Follow-up Consult", "Follow-up Consult"),
        ("Prescription Renewal", "Prescription Renewal"),
        ("Lab Result Interpretation", "Lab Result Interpretation"),
        ("Sick Leave Certificate", "Sick Leave Certificate"),
        ("Referral Letter", "Referral Letter"),
        ("Annual Physical Exam", "Annual Physical Exam"),
        ("Teleconsult", "Teleconsult"),
        ("Home Visit", "Home Visit"),
        ("Other", "Other"),
    ]

    doctor = models.ForeignKey(
        DoctorProfile,
        on_delete=models.CASCADE,
        related_name="services",
    )
    name = models.CharField(
        max_length=100,
        choices=SERVICE_CHOICES,
        help_text="Service type offered by this doctor.",
    )

    class Meta:
        verbose_name = "Doctor Service"
        verbose_name_plural = "Doctor Services"
        unique_together = ("doctor", "name")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} — {self.doctor}"


class DoctorHMO(models.Model):
    """
    HMO plans accepted by the doctor.  Critical for PH market where Maxicare,
    Medicard, and PhilCare are the dominant providers.
    """

    HMO_CHOICES = [
        ("Maxicare", "Maxicare"),
        ("Medicard", "Medicard"),
        ("PhilCare", "PhilCare"),
        ("Intellicare", "Intellicare"),
        ("Caritas Health Shield", "Caritas Health Shield"),
        ("Pacific Cross", "Pacific Cross"),
        ("Insular Health Care", "Insular Health Care"),
        ("Avega", "Avega"),
        ("EastWest Healthcare", "EastWest Healthcare"),
        ("Other", "Other"),
    ]

    doctor = models.ForeignKey(
        DoctorProfile,
        on_delete=models.CASCADE,
        related_name="hmos",
    )
    name = models.CharField(
        max_length=100,
        choices=HMO_CHOICES,
        help_text="HMO provider accepted by this doctor.",
    )

    class Meta:
        verbose_name = "Doctor HMO"
        verbose_name_plural = "Doctor HMOs"
        unique_together = ("doctor", "name")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} — {self.doctor}"

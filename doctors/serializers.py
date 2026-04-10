"""
doctors/serializers.py

Serializers for the doctors app.  Four existing shapes + four new ones:
  - DoctorListSerializer        → compact card for search results
  - DoctorDetailSerializer      → full profile page (nested relations)
  - DoctorSelfUpdateSerializer  → doctor PATCH their own profile
  - InviteDoctorSerializer      → admin POST /doctors/invite
  - ActivateDoctorSerializer    → doctor sets password via invite link
  ── NEW ──
  - AvailabilityUpdateSerializer → PATCH /doctors/availability/
  - SlotSerializer               → read shape for DoctorAvailableSlot
  - SlotCreateSerializer         → POST /doctors/slots/
  - SlotUpdateSerializer         → PATCH /doctors/slots/<pk>/
  - MyScheduleSerializer         → GET /doctors/my-schedule/
"""

import re
from datetime import datetime, time

from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import serializers

from users.models import User
from users.serializers import validate_password_strength
from .models import DoctorAvailableSlot, DoctorHMO, DoctorHospital, DoctorProfile, DoctorService, PatientHMO

PHONE_REGEX = re.compile(r"^\+639\d{9}$")

# Valid weekday keys accepted in weekly_schedule
WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


# ── Nested relation serializers ───────────────────────────────────────────────

class DoctorHospitalSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorHospital
        fields = ["id", "name", "address", "city"]


class DoctorServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorService
        fields = ["id", "name"]


class DoctorHMOSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorHMO
        fields = ["id", "name"]


# ── PatientHMO serializers ────────────────────────────────────────────────────

class PatientHMOSerializer(serializers.ModelSerializer):
    class Meta:
        model  = PatientHMO
        fields = ["id", "provider", "member_id", "card_image", "verification_status", "coverage_percent", "created_at"]
        read_only_fields = ["id", "verification_status", "coverage_percent", "created_at"]


# ── List serializer (compact card) ────────────────────────────────────────────

def _schedule_accepts(weekly_schedule: dict, mode: str) -> bool:
    """
    Returns True if any enabled day in weekly_schedule has consultation_types
    matching `mode` ("online" or "in_clinic"), or if any day uses "both".
    Falls back to True when no schedule is set (don't block booking).
    """
    if not weekly_schedule:
        return False
    for day_cfg in weekly_schedule.values():
        ct = day_cfg.get("consultation_types", "both") if isinstance(day_cfg, dict) else "both"
        if ct == "both" or ct == mode:
            return True
    return False


class DoctorListSerializer(serializers.ModelSerializer):
    full_name        = serializers.SerializerMethodField()
    user_id          = serializers.IntegerField(read_only=True)
    is_available_now = serializers.SerializerMethodField()
    profile_photo    = serializers.SerializerMethodField()
    avg_rating       = serializers.SerializerMethodField()
    review_count     = serializers.SerializerMethodField()
    accepts_online   = serializers.SerializerMethodField()
    accepts_in_clinic = serializers.SerializerMethodField()

    class Meta:
        model = DoctorProfile
        fields = [
            "id", "user_id", "full_name", "specialty", "city", "clinic_name", "profile_photo",
            "consultation_fee_online", "consultation_fee_in_person",
            "is_on_demand", "is_available_now", "is_verified", "years_of_experience",
            "avg_rating", "review_count",
            "accepts_online", "accepts_in_clinic",
        ]

    def get_full_name(self, obj) -> str:
        return f"Dr. {obj.user.first_name or ''} {obj.user.last_name or ''}".strip()

    def get_is_available_now(self, obj) -> bool:
        return obj.is_available_now

    def get_profile_photo(self, obj):
        if not obj.profile_photo:
            return None
        url = obj.profile_photo.name if hasattr(obj.profile_photo, 'name') else str(obj.profile_photo)
        if url.startswith('http'):
            return url
        try:
            return obj.profile_photo.url
        except Exception:
            return None

    def get_avg_rating(self, obj):
        from appointments.models import Review
        from django.db.models import Avg
        result = Review.objects.filter(doctor=obj.user).aggregate(avg=Avg("rating"))
        return round(result["avg"], 2) if result["avg"] else None

    def get_review_count(self, obj):
        from appointments.models import Review
        return Review.objects.filter(doctor=obj.user).count()

    def get_accepts_online(self, obj) -> bool:
        return (obj.consultation_fee_online or 0) > 0 or _schedule_accepts(obj.weekly_schedule or {}, "online")

    def get_accepts_in_clinic(self, obj) -> bool:
        return (obj.consultation_fee_in_person or 0) > 0 or _schedule_accepts(obj.weekly_schedule or {}, "in_clinic")


# ── Detail serializer (full profile page) ────────────────────────────────────

class DoctorDetailSerializer(serializers.ModelSerializer):
    full_name         = serializers.SerializerMethodField()
    user_id           = serializers.IntegerField(read_only=True)
    email             = serializers.EmailField(source="user.email", read_only=True)
    phone             = serializers.CharField(source="user.phone", read_only=True)
    is_available_now  = serializers.SerializerMethodField()
    profile_photo     = serializers.SerializerMethodField()
    hospitals         = DoctorHospitalSerializer(many=True, read_only=True)
    services          = DoctorServiceSerializer(many=True, read_only=True)
    hmos              = DoctorHMOSerializer(many=True, read_only=True)
    avg_rating        = serializers.SerializerMethodField()
    review_count      = serializers.SerializerMethodField()
    recent_reviews    = serializers.SerializerMethodField()
    accepts_online    = serializers.SerializerMethodField()
    accepts_in_clinic = serializers.SerializerMethodField()

    class Meta:
        model = DoctorProfile
        fields = [
            "id", "user_id", "full_name", "email", "phone",
            "specialty", "sub_specialties", "prc_license", "years_of_experience",
            "profile_photo", "bio", "languages_spoken",
            "clinic_name", "clinic_address", "city", "clinic_lat", "clinic_lng",
            "consultation_fee_online", "consultation_fee_in_person",
            "is_on_demand", "is_available_now", "is_verified", "invite_accepted", "last_active_at",
            "weekly_schedule",
            "hospitals", "services", "hmos",
            "avg_rating", "review_count", "recent_reviews",
            "accepts_online", "accepts_in_clinic",
            "created_at", "updated_at",
        ]

    def get_full_name(self, obj) -> str:
        return f"Dr. {obj.user.first_name or ''} {obj.user.last_name or ''}".strip()

    def get_is_available_now(self, obj) -> bool:
        return obj.is_available_now

    def get_profile_photo(self, obj):
        if not obj.profile_photo:
            return None
        url = obj.profile_photo.name if hasattr(obj.profile_photo, 'name') else str(obj.profile_photo)
        if url.startswith('http'):
            return url
        try:
            return obj.profile_photo.url
        except Exception:
            return None

    def get_avg_rating(self, obj):
        from appointments.models import Review
        from django.db.models import Avg
        result = Review.objects.filter(doctor=obj.user).aggregate(avg=Avg("rating"))
        return round(result["avg"], 2) if result["avg"] else None

    def get_review_count(self, obj):
        from appointments.models import Review
        return Review.objects.filter(doctor=obj.user).count()

    def get_recent_reviews(self, obj):
        """Return the 10 most recent reviews with patient name, rating, comment, and doctor reply."""
        from appointments.models import Review
        qs = (
            Review.objects
            .filter(doctor=obj.user)
            .select_related("patient")
            .order_by("-created_at")[:10]
        )
        return [
            {
                "id":           r.pk,
                "appointment":  r.appointment_id,
                "patient_name": f"{r.patient.first_name} {r.patient.last_name}".strip(),
                "rating":       r.rating,
                "comment":      r.comment,
                "created_at":   r.created_at.isoformat(),
                "doctor_reply": r.doctor_reply or None,
                "reply_at":     r.reply_at.isoformat() if r.reply_at else None,
            }
            for r in qs
        ]

    def get_accepts_online(self, obj) -> bool:
        return (obj.consultation_fee_online or 0) > 0 or _schedule_accepts(obj.weekly_schedule or {}, "online")

    def get_accepts_in_clinic(self, obj) -> bool:
        return (obj.consultation_fee_in_person or 0) > 0 or _schedule_accepts(obj.weekly_schedule or {}, "in_clinic")


# ── Self-update serializer (doctor PATCH own profile) ────────────────────────

class DoctorSelfUpdateSerializer(serializers.ModelSerializer):
    """
    Fields a doctor can update themselves after invite_accepted=True.
    Excludes is_verified (admin-only) and prc_license (immutable after invite).
    """

    class Meta:
        model = DoctorProfile
        fields = [
            "profile_photo",
            "bio",
            "years_of_experience",
            "clinic_name",
            "clinic_address",
            "city",
            "consultation_fee_online",
            "consultation_fee_in_person",
            "languages_spoken",
            "sub_specialties",
            "is_on_demand",
        ]

    def validate_languages_spoken(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of language strings.")
        return value

    def validate_sub_specialties(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of sub-specialty strings.")
        return value


# ── Invite serializer (admin POST /doctors/invite) ────────────────────────────

class InviteDoctorSerializer(serializers.Serializer):
    """Admin-only: collect doctor details, create inactive user + profile, send invite."""

    firstName = serializers.CharField(source="first_name")
    middleName = serializers.CharField(
        source="middle_name", required=False, allow_blank=True, default=""
    )
    lastName = serializers.CharField(source="last_name")
    email = serializers.EmailField()
    phone = serializers.CharField()
    specialty = serializers.ChoiceField(choices=DoctorProfile.SPECIALTY_CHOICES)
    clinicName = serializers.CharField(source="clinic_name", max_length=200)
    prcLicense = serializers.CharField(source="prc_license", max_length=20)
    city = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_email(self, value):
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_phone(self, value):
        if not PHONE_REGEX.match(value):
            raise serializers.ValidationError(
                "Phone must be in E.164 format: +639XXXXXXXXX"
            )
        return value

    def validate_prcLicense(self, value):
        if DoctorProfile.objects.filter(prc_license=value).exists():
            raise serializers.ValidationError(
                "A doctor with this PRC license already exists."
            )
        return value


# ── Activate serializer (doctor sets password via invite link) ────────────────

class ActivateDoctorSerializer(serializers.Serializer):
    """Public: doctor sets their own password using the emailed token link."""

    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(min_length=8, write_only=True)
    password_confirm = serializers.CharField(write_only=True)

    def validate_password(self, value):
        return validate_password_strength(value)

    def validate(self, attrs):
        try:
            uid = force_str(urlsafe_base64_decode(attrs["uid"]))
            user = User.objects.get(pk=uid)
        except (User.DoesNotExist, ValueError, TypeError):
            raise serializers.ValidationError({"uid": "Invalid or expired invite link."})

        if not default_token_generator.check_token(user, attrs["token"]):
            raise serializers.ValidationError({"token": "Invalid or expired invite link."})

        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})

        profile = getattr(user, "doctor_profile", None)
        if profile and profile.invite_accepted:
            raise serializers.ValidationError({"token": "This invite has already been used."})

        attrs["user"] = user
        return attrs

    def save(self):
        user = self.validated_data["user"]
        user.set_password(self.validated_data["password"])
        user.is_active = True
        user.save(update_fields=["password", "is_active"])

        profile = getattr(user, "doctor_profile", None)
        if profile:
            profile.invite_accepted = True
            profile.save(update_fields=["invite_accepted"])

        return user


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Schedule / Availability serializers
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_time_str(value: str, field_name: str) -> str:
    """Validate HH:MM format and return the value unchanged."""
    try:
        datetime.strptime(value, "%H:%M")
    except (ValueError, TypeError):
        raise serializers.ValidationError(
            {field_name: f"'{value}' is not a valid time. Use HH:MM format (e.g. '09:00')."}
        )
    return value


CONSULTATION_TYPES = {"online", "in_clinic", "both"}


class WeeklyDayScheduleSerializer(serializers.Serializer):
    """
    Validates a single day entry in weekly_schedule.
    e.g. {"start": "09:00", "end": "17:00", "consultation_types": "both"}

    consultation_types: "online" | "in_clinic" | "both"  (default: "both")
    """
    start              = serializers.CharField()
    end                = serializers.CharField()
    consultation_types = serializers.ChoiceField(
        choices=["online", "in_clinic", "both"],
        default="both",
        required=False,
    )

    def validate_start(self, value):
        return _validate_time_str(value, "start")

    def validate_end(self, value):
        return _validate_time_str(value, "end")

    def validate(self, attrs):
        start = datetime.strptime(attrs["start"], "%H:%M").time()
        end   = datetime.strptime(attrs["end"],   "%H:%M").time()
        if end <= start:
            raise serializers.ValidationError("end time must be after start time.")
        if "consultation_types" not in attrs:
            attrs["consultation_types"] = "both"
        return attrs


class AvailabilityUpdateSerializer(serializers.Serializer):
    """
    PATCH /doctors/availability/

    Allows a doctor to:
      1. Toggle on-demand availability (is_on_demand).
      2. Set/replace their recurring weekly_schedule.

    Both fields are optional — send only what you want to change.

    weekly_schedule example:
    {
      "monday":    {"start": "09:00", "end": "17:00"},
      "wednesday": {"start": "09:00", "end": "12:00"},
      "friday":    {"start": "14:00", "end": "18:00"}
    }
    Omitting a weekday means the doctor is not available that day.
    """
    is_on_demand    = serializers.BooleanField(required=False)
    weekly_schedule = serializers.DictField(
        child=WeeklyDayScheduleSerializer(),
        required=False,
        allow_empty=True,
    )

    def validate_weekly_schedule(self, value: dict) -> dict:
        invalid_keys = set(value.keys()) - WEEKDAYS
        if invalid_keys:
            raise serializers.ValidationError(
                f"Invalid weekday key(s): {invalid_keys}. "
                f"Allowed: {WEEKDAYS}"
            )
        # Each value is already validated by WeeklyDayScheduleSerializer
        return value


# ── Slot serializers ──────────────────────────────────────────────────────────

class SlotSerializer(serializers.ModelSerializer):
    """
    Read serializer for DoctorAvailableSlot — returned on list/create/update.

    is_booked is computed via a single Appointment query per slot.  For list
    views with many slots, prefer SlotListSerializer which accepts a pre-built
    booked_set in context to avoid N+1 queries.
    """
    is_booked = serializers.SerializerMethodField()

    class Meta:
        model  = DoctorAvailableSlot
        fields = [
            "id", "doctor", "date", "start_time", "end_time",
            "is_available", "is_recurring", "is_booked",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_is_booked(self, obj) -> bool:
        """
        True if an active appointment exists that overlaps this slot's time window.
        Checks context["booked_set"] first (a set of "date|HH:MM" strings pre-built
        by the view) to avoid N+1 in list endpoints.
        """
        booked_set = self.context.get("booked_set")
        if booked_set is not None:
            key = f"{obj.date}|{obj.start_time.strftime('%H:%M')}"
            return key in booked_set

        from appointments.models import Appointment
        return Appointment.objects.filter(
            doctor=obj.doctor.user,
            date=obj.date,
            time__gte=obj.start_time,
            time__lt=obj.end_time,
        ).exclude(status__in=["cancelled", "no_show"]).exists()


class SlotCreateSerializer(serializers.Serializer):
    """
    POST /doctors/slots/

    Two modes:
      1. Single date slot:
         {"date": "2026-04-15", "start_time": "10:00", "end_time": "10:30"}

      2. Recurring weekly slot (creates rows for the next `weeks_ahead` weeks):
         {"weekday": 0, "start_time": "09:00", "end_time": "09:30", "is_recurring": true}
         weekday: 0=Monday … 6=Sunday (Python isoweekday - 1)

    is_available defaults to True.
    weeks_ahead: how many weeks to pre-generate for recurring slots (default 8, max 52).
    """
    date         = serializers.DateField(required=False)
    weekday      = serializers.IntegerField(min_value=0, max_value=6, required=False)
    start_time   = serializers.TimeField()
    end_time     = serializers.TimeField()
    is_available = serializers.BooleanField(default=True)
    is_recurring = serializers.BooleanField(default=False)
    weeks_ahead  = serializers.IntegerField(default=8, min_value=1, max_value=52, required=False)

    def validate_start_time(self, value: time) -> time:
        # Enforce 30-min boundary (00 or 30 minutes)
        if value.minute not in (0, 30) or value.second != 0:
            raise serializers.ValidationError(
                "Slots must start on a 30-minute boundary (e.g. 09:00 or 09:30)."
            )
        return value

    def validate_end_time(self, value: time) -> time:
        if value.minute not in (0, 30) or value.second != 0:
            raise serializers.ValidationError(
                "Slots must end on a 30-minute boundary (e.g. 09:30 or 10:00)."
            )
        return value

    def validate(self, attrs):
        is_recurring = attrs.get("is_recurring", False)

        if is_recurring:
            if attrs.get("weekday") is None:
                raise serializers.ValidationError(
                    {"weekday": "weekday is required for recurring slots."}
                )
        else:
            if not attrs.get("date"):
                raise serializers.ValidationError(
                    {"date": "date is required for non-recurring slots."}
                )

        if attrs["end_time"] <= attrs["start_time"]:
            raise serializers.ValidationError(
                {"end_time": "end_time must be after start_time."}
            )

        # Enforce exactly 30-minute duration
        start_mins = attrs["start_time"].hour * 60 + attrs["start_time"].minute
        end_mins   = attrs["end_time"].hour   * 60 + attrs["end_time"].minute
        if (end_mins - start_mins) != 30:
            raise serializers.ValidationError(
                {"end_time": "Slot duration must be exactly 30 minutes."}
            )

        return attrs


class SlotUpdateSerializer(serializers.Serializer):
    """
    PATCH /doctors/slots/<pk>/

    Allows updating start_time, end_time, and/or is_available.
    Cannot change date or doctor — create a new slot instead.
    """
    start_time   = serializers.TimeField(required=False)
    end_time     = serializers.TimeField(required=False)
    is_available = serializers.BooleanField(required=False)

    def validate(self, attrs):
        start = attrs.get("start_time")
        end   = attrs.get("end_time")

        if start and end:
            if end <= start:
                raise serializers.ValidationError(
                    {"end_time": "end_time must be after start_time."}
                )
            start_mins = start.hour * 60 + start.minute
            end_mins   = end.hour   * 60 + end.minute
            if (end_mins - start_mins) != 30:
                raise serializers.ValidationError(
                    {"end_time": "Slot duration must be exactly 30 minutes."}
                )

        for field in ("start_time", "end_time"):
            t = attrs.get(field)
            if t and (t.minute not in (0, 30) or t.second != 0):
                raise serializers.ValidationError(
                    {field: "Must be on a 30-minute boundary (e.g. 09:00 or 09:30)."}
                )

        return attrs


# ── Doctor Profile Completion serializer (onboarding wizard) ──────────────────────

class DoctorProfileCompletionSerializer(serializers.ModelSerializer):
    """
    PATCH /api/doctors/me/complete/

    Doctor onboarding wizard — partial update for DoctorProfile fields.
    Also accepts `services` (list of service name strings) and
    `hmos` (list of HMO name strings) to set the M2M-style related rows,
    and `clinic_lat`/`clinic_lng` from the Google Maps pin.
    """

    # Write-only helpers for services and HMOs
    services = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True
    )
    hmos = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True
    )

    class Meta:
        model = DoctorProfile
        fields = [
            "profile_photo",
            "bio",
            "languages_spoken",
            "clinic_name",
            "clinic_address",
            "city",
            "clinic_lat",
            "clinic_lng",
            "consultation_fee_online",
            "consultation_fee_in_person",
            "weekly_schedule",
            "is_on_demand",
            "specialty",
            "sub_specialties",
            "years_of_experience",
            "is_profile_complete",
            # write-only
            "services",
            "hmos",
        ]
        extra_kwargs = {
            f: {"required": False}
            for f in [
                "profile_photo", "bio", "languages_spoken",
                "clinic_name", "clinic_address", "city",
                "clinic_lat", "clinic_lng",
                "consultation_fee_online", "consultation_fee_in_person",
                "weekly_schedule", "is_on_demand",
                "specialty", "sub_specialties", "years_of_experience",
                "is_profile_complete",
            ]
        }

    def validate_languages_spoken(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of language strings.")
        return value

    def validate_sub_specialties(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Must be a list of sub-specialty strings.")
        return value

    def validate(self, attrs):
        if attrs.get("is_profile_complete"):
            instance = self.instance
            clinic_name = attrs.get("clinic_name") or getattr(instance, "clinic_name", "")
            specialty   = attrs.get("specialty")   or getattr(instance, "specialty",   "")
            fee_online  = attrs.get("consultation_fee_online")    or getattr(instance, "consultation_fee_online",    None)
            fee_person  = attrs.get("consultation_fee_in_person") or getattr(instance, "consultation_fee_in_person", None)

            errors = {}
            if not clinic_name:
                errors["clinic_name"] = "Clinic name is required to complete your profile."
            if not specialty:
                errors["specialty"] = "Specialty is required to complete your profile."
            if not fee_online and not fee_person:
                errors["consultation_fee_online"] = "At least one consultation fee is required."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs

    def update(self, instance, validated_data):
        # Pop write-only relation fields before saving model fields
        services_input = validated_data.pop("services", None)
        hmos_input     = validated_data.pop("hmos", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save(update_fields=list(validated_data.keys()) + ["updated_at"])

        # Replace services if provided
        if services_input is not None:
            from .models import DoctorService
            instance.services.all().delete()
            valid_names = {c[0] for c in DoctorService.SERVICE_CHOICES}
            for name in services_input:
                if name in valid_names:
                    DoctorService.objects.get_or_create(doctor=instance, name=name)

        # Replace HMOs if provided
        if hmos_input is not None:
            from .models import DoctorHMO
            instance.hmos.all().delete()
            valid_names = {c[0] for c in DoctorHMO.HMO_CHOICES}
            for name in hmos_input:
                if name in valid_names:
                    DoctorHMO.objects.get_or_create(doctor=instance, name=name)

        return instance


# ── My Schedule serializer ────────────────────────────────────────────────────

class ScheduleDashboardSlotSerializer(serializers.ModelSerializer):
    """
    Lightweight slot shape for the doctor's schedule dashboard.
    Omits doctor FK, created_at, updated_at — not needed on the schedule page.
    """
    is_booked = serializers.SerializerMethodField()

    class Meta:
        model  = DoctorAvailableSlot
        fields = ["id", "date", "start_time", "end_time", "is_available", "is_recurring", "is_booked"]
        read_only_fields = fields

    def get_is_booked(self, obj) -> bool:
        booked_set = self.context.get("booked_set")
        if booked_set is not None:
            key = f"{obj.date}|{obj.start_time.strftime('%H:%M')}"
            return key in booked_set
        from appointments.models import Appointment
        return Appointment.objects.filter(
            doctor=obj.doctor.user,
            date=obj.date,
            time__gte=obj.start_time,
            time__lt=obj.end_time,
        ).exclude(status__in=["cancelled", "no_show"]).exists()


class UpcomingAppointmentSerializer(serializers.Serializer):
    """Compact appointment shape used inside MyScheduleSerializer."""
    id           = serializers.IntegerField()
    patient_name = serializers.CharField()
    date         = serializers.DateField()
    time         = serializers.TimeField()
    type         = serializers.CharField()
    status       = serializers.CharField()


class MyScheduleSerializer(serializers.Serializer):
    """
    GET /doctors/my-schedule/

    Dashboard view for the doctor:
      - is_on_demand + is_available_now
      - weekly_schedule (recurring hours)
      - upcoming_slots: explicit DoctorAvailableSlot rows for next 7–30 days
      - upcoming_appointments: booked appointments for next 7–30 days
    """
    is_on_demand          = serializers.BooleanField()
    is_available_now      = serializers.BooleanField()
    weekly_schedule       = serializers.DictField()
    upcoming_slots        = ScheduleDashboardSlotSerializer(many=True)
    upcoming_appointments = UpcomingAppointmentSerializer(many=True)

"""
doctors/admin.py

Invite Doctor uses a Proxy model + ModelAdmin so Django admin renders
the form entirely through its own built-in machinery — no custom templates.
"""

import re
import threading
import logging

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.tokens import default_token_generator
from django.conf import settings
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.html import format_html
from django.utils.http import urlsafe_base64_encode

from users.models import User
from .models import DoctorHMO, DoctorHospital, DoctorProfile, DoctorService, PatientHMO

logger = logging.getLogger(__name__)

# ── Inlines ───────────────────────────────────────────────────────────────────

class DoctorHospitalInline(admin.TabularInline):
    model  = DoctorHospital
    extra  = 1
    fields = ("name", "address", "city")


class DoctorServiceInline(admin.TabularInline):
    model  = DoctorService
    extra  = 1
    fields = ("name",)


class DoctorHMOInline(admin.TabularInline):
    model  = DoctorHMO
    extra  = 1
    fields = ("name",)



# ── Proxy model for the Invite Doctor form ────────────────────────────────────

class DoctorInvite(DoctorProfile):
    """Proxy model used exclusively for the Invite Doctor admin form."""
    class Meta:
        proxy               = True
        verbose_name        = "Invite Doctor"
        verbose_name_plural = "Invite Doctor"


PHONE_REGEX = re.compile(r"^\+639\d{9}$")


class DoctorInviteForm(forms.ModelForm):
    first_name  = forms.CharField(max_length=150, label="First Name")
    middle_name = forms.CharField(max_length=150, label="Middle Name", required=False)
    last_name   = forms.CharField(max_length=150, label="Last Name")
    email       = forms.EmailField(label="Email Address")
    phone       = forms.CharField(max_length=20,  label="Phone (+639XXXXXXXXX)")
    specialty   = forms.ChoiceField(
        choices=[("", "— Select specialty —")] + DoctorProfile.SPECIALTY_CHOICES,
        label="Specialty",
    )
    clinic_name = forms.CharField(max_length=200, label="Clinic Name")
    prc_license = forms.CharField(max_length=20,  label="PRC License (7 digits)")
    city        = forms.CharField(
        max_length=100, label="City", required=False,
    )

    class Meta:
        model  = DoctorInvite
        fields = []

    def clean_email(self):
        value = self.cleaned_data["email"]
        if User.objects.filter(email=value).exists():
            raise ValidationError("A user with this email already exists.")
        return value

    def clean_phone(self):
        value = self.cleaned_data["phone"]
        if not PHONE_REGEX.match(value):
            raise ValidationError("Phone must be in format: +639XXXXXXXXX")
        return value

    def clean_prc_license(self):
        value = self.cleaned_data["prc_license"]
        if DoctorProfile.objects.filter(prc_license=value).exists():
            raise ValidationError("A doctor with this PRC license already exists.")
        return value

    def clean_specialty(self):
        value = self.cleaned_data["specialty"]
        if not value:
            raise ValidationError("Please select a specialty.")
        return value


@admin.register(DoctorInvite)
class DoctorInviteAdmin(admin.ModelAdmin):
    form = DoctorInviteForm

    def has_module_perms(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_staff

    def has_add_permission(self, request):
        return request.user.is_staff

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        return redirect(reverse("admin:doctors_doctorprofile_changelist"))

    fieldsets = (
        ("Personal Details", {
            "description": "Basic contact information for the new doctor.",
            "fields": ("first_name", "middle_name", "last_name", "email", "phone"),
        }),
        ("Professional Details", {
            "description": "Credentials and clinic information.",
            "fields": ("specialty", "prc_license", "clinic_name", "city"),
        }),
    )

    def log_addition(self, request, obj, message):
        pass  # skip — obj is an empty proxy with no user

    def save_model(self, request, obj, form, change):
        data = form.cleaned_data

        user = User(
            email       = data["email"],
            first_name  = data["first_name"],
            middle_name = data.get("middle_name", ""),
            last_name   = data["last_name"],
            phone       = data["phone"],
            role        = "doctor",
            is_active   = False,
        )
        user.set_unusable_password()
        user.save()

        DoctorProfile.objects.create(
            user        = user,
            specialty   = data["specialty"],
            clinic_name = data["clinic_name"],
            prc_license = data["prc_license"],
            city        = data.get("city", ""),
        )

        uid          = urlsafe_base64_encode(force_bytes(user.pk))
        token        = default_token_generator.make_token(user)
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        invite_link  = f"{frontend_url}/set-doctor-password?uid={uid}&token={token}"

        # Send synchronously so SMTP errors surface in logs/admin.
        try:
            _send_invite_email(user.email, user.first_name, invite_link)
        except Exception as exc:
            logger.exception("Doctor invite email failed for %s", user.email)
            self.message_user(
                request,
                "Invite created, but email failed to send. "
                "Check SMTP settings/network and resend from the Doctors list.",
                messages.ERROR,
            )
            return

        self.message_user(
            request,
            f"✅ Invite sent to {user.email}. The doctor will receive an activation email shortly.",
            messages.SUCCESS,
        )

    def response_add(self, request, obj, post_url_continue=None):
        from django.shortcuts import redirect
        return redirect(reverse("admin:doctors_doctorprofile_changelist"))


# ── DoctorProfile admin ───────────────────────────────────────────────────────

@admin.register(DoctorProfile)
class DoctorProfileAdmin(admin.ModelAdmin):
    list_display = (
        "full_name", "email", "specialty", "city", "prc_license",
        "verified_badge", "invite_badge", "on_demand_badge",
        "last_active_at", "created_at",
    )
    list_filter  = ("specialty", "city", "is_verified", "invite_accepted", "is_on_demand")
    search_fields = (
        "user__email", "user__first_name", "user__last_name",
        "prc_license", "clinic_name",
    )
    readonly_fields       = ("created_at", "updated_at", "invite_accepted", "last_active_at", "profile_photo_preview")
    ordering              = ("-created_at",)
    inlines               = [DoctorHospitalInline, DoctorServiceInline, DoctorHMOInline]
    list_per_page         = 25
    show_full_result_count = True

    fieldsets = (
        ("Identity", {
            "fields": ("user", "specialty", "sub_specialties", "prc_license", "years_of_experience"),
        }),
        ("Profile", {
            "fields": ("profile_photo_preview", "profile_photo", "bio", "languages_spoken"),
        }),
        ("Location", {
            "fields": ("clinic_name", "clinic_address", "city"),
        }),
        ("Fees (PHP)", {
            "fields": ("consultation_fee_online", "consultation_fee_in_person"),
        }),
        ("Status", {
            "fields": ("is_verified", "is_on_demand", "invite_accepted", "last_active_at"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    # ── Computed columns ──────────────────────────────────────────────────────

    @admin.display(description="Current Photo")
    def profile_photo_preview(self, obj):
        if not obj.profile_photo:
            return "No photo uploaded"
        url = obj.profile_photo.name
        if not url.startswith(("http://", "https://")):
            url = obj.profile_photo.url
        return format_html('<img src="{}" style="max-height:120px;border-radius:8px;" />', url)

    @admin.display(description="Name", ordering="user__last_name")
    def full_name(self, obj):
        name = f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.email
        return format_html('<strong style="color:#0f172a">{}</strong>', name)

    @admin.display(description="Email", ordering="user__email")
    def email(self, obj):
        return format_html('<a href="mailto:{0}" style="color:#0d9488">{0}</a>', obj.user.email)

    @admin.display(description="Verified", ordering="is_verified")
    def verified_badge(self, obj):
        if obj.is_verified:
            return format_html('<span class="badge-status badge-verified">{}</span>', "✓ Verified")
        return format_html('<span class="badge-status badge-pending">{}</span>', "⏳ Pending")

    @admin.display(description="Invite", ordering="invite_accepted")
    def invite_badge(self, obj):
        if obj.invite_accepted:
            return format_html('<span class="badge-status badge-active">{}</span>', "✓ Accepted")
        return format_html('<span class="badge-status badge-inactive">{}</span>', "⏳ Pending")

    @admin.display(description="On-Demand", ordering="is_on_demand")
    def on_demand_badge(self, obj):
        if obj.is_on_demand:
            return format_html('<span class="badge-status badge-on_demand">{}</span>', "● Live")
        return format_html('<span class="badge-status badge-inactive">{}</span>', "○ Off")

    # ── Actions ───────────────────────────────────────────────────────────────

    @admin.action(description="✅ Mark selected doctors as verified")
    def mark_verified(self, request, queryset):
        updated = queryset.update(is_verified=True)
        self.message_user(request, f"{updated} doctor(s) marked as verified.", messages.SUCCESS)

    @admin.action(description="📧 Resend activation email")
    def resend_invite(self, request, queryset):
        frontend_url = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
        sent = 0
        failed = 0
        for profile in queryset.select_related("user"):
            user = profile.user
            if profile.invite_accepted:
                self.message_user(request, f"{user.email} already activated — skipped.", messages.WARNING)
                continue
            uid   = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            link  = f"{frontend_url}/set-doctor-password?uid={uid}&token={token}"
            # Send synchronously so SMTP errors surface in logs/admin.
            try:
                _send_invite_email(user.email, user.first_name, link)
                sent += 1
            except Exception:
                failed += 1
                logger.exception("Resend invite email failed for %s", user.email)
                self.message_user(request, f"Failed to send invite to {user.email}.", messages.ERROR)
        if sent:
            self.message_user(request, f"Activation email resent to {sent} doctor(s).", messages.SUCCESS)
        if failed and not sent:
            self.message_user(request, "No invites were sent due to SMTP errors.", messages.ERROR)

    actions = ["mark_verified", "resend_invite"]


# ── Related model admins ──────────────────────────────────────────────────────

@admin.register(DoctorHospital)
class DoctorHospitalAdmin(admin.ModelAdmin):
    list_display  = ("name", "city", "doctor")
    search_fields = ("name", "doctor__user__last_name")
    list_filter   = ("city",)


@admin.register(DoctorService)
class DoctorServiceAdmin(admin.ModelAdmin):
    list_display  = ("name", "doctor")
    search_fields = ("name", "doctor__user__last_name")
    list_filter   = ("name",)


@admin.register(DoctorHMO)
class DoctorHMOAdmin(admin.ModelAdmin):
    list_display  = ("name", "doctor")
    search_fields = ("name", "doctor__user__last_name")
    list_filter   = ("name",)


# ── Email helper ──────────────────────────────────────────────────────────────

def _send_invite_email(email: str, first_name: str, invite_url: str) -> None:
    from django.core.mail import send_mail

    subject = "Welcome to CareConnect – Activate Your Doctor Account"
    plain = (
        f"Hi Dr. {first_name},\n\n"
        f"An administrator has created a CareConnect doctor account for you.\n\n"
        f"Click the link below to set your password and activate your account:\n"
        f"{invite_url}\n\n"
        f"This link expires in 3 days. If you did not expect this email, ignore it."
    )
    html = f"""
    <div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:auto;padding:32px;
                border:1px solid #e5e7eb;border-radius:12px;">
      <h2 style="color:#0d9488;margin-bottom:4px;">CareConnect</h2>
      <p style="color:#6b7280;font-size:14px;margin-top:0;">Healthcare, made simple.</p>
      <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
      <p style="font-size:15px;color:#111827;">Hi <strong>Dr. {first_name}</strong>,</p>
      <p style="font-size:14px;color:#374151;">
        An administrator has created a CareConnect doctor account for you.
        Click the button below to set your password and activate your account.
      </p>
      <div style="text-align:center;margin:28px 0;">
        <a href="{invite_url}"
           style="background:#0d9488;color:#fff;padding:12px 28px;border-radius:8px;
                  text-decoration:none;font-weight:600;font-size:15px;">
          Activate My Account
        </a>
      </div>
      <p style="font-size:12px;color:#9ca3af;text-align:center;">
        This link expires in <strong>3 days</strong>.
        If you did not expect this email, you can safely ignore it.
      </p>
    </div>
    """
    send_mail(
        subject=subject,
        message=plain,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        html_message=html,
        fail_silently=False,
    )


# ── PatientHMO admin ──────────────────────────────────────────────────────────

@admin.register(PatientHMO)
class PatientHMOAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "provider", "member_id", "status_badge", "coverage_percent", "created_at")
    list_filter   = ("verification_status", "provider")
    search_fields = ("patient__email", "provider", "member_id")
    ordering      = ("-created_at",)
    list_per_page = 25
    actions       = ["verify_cards", "reject_cards"]

    @admin.display(description="Status", ordering="verification_status")
    def status_badge(self, obj):
        mapping = {
            "verified": ("badge-verified", "✓ Verified"),
            "pending":  ("badge-pending",  "⏳ Pending"),
            "rejected": ("badge-rejected", "✗ Rejected"),
        }
        css, label = mapping.get(obj.verification_status, ("badge-inactive", obj.verification_status))
        return format_html('<span class="badge-status {}">{}</span>', css, label)

    @admin.action(description="✅ Verify selected HMO cards")
    def verify_cards(self, request, queryset):
        updated = queryset.filter(verification_status="pending").update(verification_status="verified")
        self.message_user(request, f"{updated} HMO card(s) verified.", messages.SUCCESS)

    @admin.action(description="❌ Reject selected HMO cards")
    def reject_cards(self, request, queryset):
        updated = queryset.filter(verification_status="pending").update(verification_status="rejected")
        self.message_user(request, f"{updated} HMO card(s) rejected.", messages.WARNING)

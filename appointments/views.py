"""
appointments/views.py

AppointmentViewSet + OnDemandView + ReviewView + MyDoctorsView
"""

import json
import logging
import secrets
import string
import threading
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from doctors.models import DoctorProfile
from notifications.models import Notification
from users.models import User
from .models import Appointment, AppointmentShare, FollowUpInvitation, PatientProfile, Review
from .serializers import (
    AppointmentCreateSerializer,
    AppointmentDetailSerializer,
    AppointmentListSerializer,
    AppointmentUpdateSerializer,
    CancelAppointmentSerializer,
    FollowUpInvitationSerializer,
    ReviewCreateSerializer,
    ReviewReplySerializer,
    ReviewSerializer,
)

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_doctor(user):  return user.role == "doctor"
def _is_patient(user): return user.role == "patient"
def _is_admin(user):   return user.role == "admin" or user.is_staff


def _get_jitsi_domain() -> str:
    return getattr(settings, "JITSI_DOMAIN", "meet.jit.si")


def _jitsi_url(room_name: str) -> str:
    return f"https://{_get_jitsi_domain()}/{room_name}"


def _generate_room_name(appointment_id: int) -> str:
    # Stable, deterministic room name — no random suffix.
    # Doctor and patient both derive the same URL from the appointment ID.
    # Using a random suffix was the root cause of doctor/patient being in
    # different rooms when start_video was called more than once.
    return f"careconnect-apt-{appointment_id}"


def _generate_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _broadcast_queue_update(doctor_id: int, target_date):
    """Broadcast queue updates to doctor group and each waiting patient."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        qs = (
            Appointment.objects
            .select_related("patient")
            .filter(
                doctor_id=doctor_id,
                date=target_date,
                status__in=["confirmed", "in_progress"],
            )
            .order_by("queue_number", "time")
        )
        now_serving = qs.filter(status="in_progress").first()
        waiting = [apt for apt in qs if apt.status == "confirmed"]

        payload = {
            "type": "queue.update",
            "doctor_id": doctor_id,
            "date": str(target_date),
            "now_serving": {
                "appointment_id": now_serving.pk if now_serving else None,
                "patient_name": f"{now_serving.patient.first_name} {now_serving.patient.last_name}".strip()
                if now_serving else None,
                "queue_number": now_serving.queue_number if now_serving else None,
                "status": now_serving.status if now_serving else None,
            },
            "waiting": [
                {
                    "appointment_id": apt.pk,
                    "patient_name": f"{apt.patient.first_name} {apt.patient.last_name}".strip(),
                    "queue_number": apt.queue_number,
                    "queue_position": apt.queue_position,
                    "estimated_wait_minutes": apt.estimated_wait_minutes,
                }
                for apt in waiting
            ],
        }

        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"queue_doctor_{doctor_id}",
            {**payload, "type": "queue.update"},
        )

        # Per-appointment broadcasts for patient UI
        for apt in qs:
            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type": "queue.update",
                    "appointment_id": apt.pk,
                    "queue_position": apt.queue_position,
                    "estimated_wait_minutes": apt.estimated_wait_minutes,
                    "now_serving_id": now_serving.pk if now_serving else None,
                },
            )
    except Exception as exc:
        logger.warning("Queue broadcast failed: %s", exc)


def _send_booking_under_review_email(appointment) -> None:
    """Send 'under review' email to CareConnect account holder."""
    from django.core.mail import send_mail
    patient = appointment.patient
    doctor = appointment.doctor
    date_str = appointment.date.strftime("%B %d, %Y")
    time_str = appointment.time.strftime("%I:%M %p").lstrip("0")
    
    # Patient name from booked_for_name or logged-in user
    patient_name = appointment.booked_for_name.strip() if appointment.booked_for_name else f"{patient.first_name} {patient.last_name}".strip()
    
    subject = f"Booking Under Review – Dr. {doctor.last_name} on {date_str}"
    plain = (
        f"Hi {patient.first_name},\n\n"
        f"Your booking is under review.\n\n"
        f"Patient: {patient_name}\n"
        f"Doctor: Dr. {doctor.first_name} {doctor.last_name}\n"
        f"Date: {date_str}\nTime: {time_str}\n\n"
        f"You will receive a confirmation email once the doctor accepts.\n\n"
        f"— CareConnect Team"
    )
    
    html = f"""
    <div style="font-family:Poppins,sans-serif;max-width:540px;margin:auto;padding:32px;border:1px solid #e5e7eb;border-radius:12px;">
      <h2 style="color:#0d9488;">CareConnect</h2>
      <div style="background:#fef3c7;border:1px solid #fbbf24;border-radius:10px;padding:14px;margin:16px 0;">
        <p style="margin:0;font-weight:700;color:#92400e;">⏳ Booking Under Review</p>
        <p style="margin:4px 0 0;font-size:13px;color:#78350f;">Please wait for your doctor's response.</p>
      </div>
      <p>Hi <strong>{patient.first_name}</strong>,</p>
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin:16px 0;">
        <table style="width:100%;">
          <tr><td style="color:#6b7280;padding:4px 0;">Patient</td><td style="font-weight:600;padding:4px 0;">{patient_name}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Doctor</td><td style="padding:4px 0;">Dr. {doctor.first_name} {doctor.last_name}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Date</td><td style="padding:4px 0;">{date_str}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Time</td><td style="padding:4px 0;">{time_str}</td></tr>
        </table>
      </div>
    </div>
    """
    
    recipients = [patient.email]
    
    try:
        send_mail(
            subject=subject,
            message=plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            html_message=html,
            fail_silently=False,
        )
    except Exception as exc:
        logger.warning("Under review email failed for apt #%s: %s", appointment.pk, exc)


def _send_appointment_confirmed_email(appointment) -> None:
    """Send confirmation email when doctor accepts booking."""
    from django.core.mail import send_mail
    patient = appointment.patient
    doctor = appointment.doctor
    doctor_name = f"Dr. {doctor.first_name} {doctor.last_name}".strip()
    date_str = appointment.date.strftime("%B %d, %Y")
    time_str = appointment.time.strftime("%I:%M %p").lstrip("0")
    ref_number = f"APT-{str(appointment.pk).zfill(8).upper()}"
    apt_url = f"{settings.FRONTEND_URL}/patient/appointments/{appointment.pk}"

    patient_name = appointment.booked_for_name or f"{patient.first_name} {patient.last_name}".strip()

    apt_type_label = {
        "online": "Online / Video",
        "in_clinic": "In-Clinic",
        "on_demand": "On-Demand",
    }.get(appointment.type, appointment.type.replace("_", " ").title())

    if appointment.type == "in_clinic":
        snap = appointment.clinic_info_snapshot or {}
        profile = getattr(doctor, "doctor_profile", None)
        clinic_name = snap.get("clinic_name", "") or (getattr(profile, "clinic_name", "") if profile else "")
        clinic_address = snap.get("clinic_address", "") or (getattr(profile, "clinic_address", "") if profile else "")
        city = snap.get("city", "") or (getattr(profile, "city", "") if profile else "")
        location_line = ", ".join(filter(None, [clinic_name, clinic_address, city]))
        detail_html = (
            "<tr>"
            f"<td style='padding:5px 0;color:#6b7280;font-size:13px;width:130px;'>Clinic</td>"
            f"<td style='padding:5px 0;font-size:13px;font-weight:600;color:#111827;'>{location_line}</td>"
            "</tr>"
            "<tr>"
            "<td style='padding:5px 0;color:#6b7280;font-size:13px;'>Payment</td>"
            "<td style='padding:5px 0;font-size:13px;color:#111827;'>Pay at clinic upon arrival</td>"
            "</tr>"
        )
    else:
        fee_display = f"₱{appointment.effective_fee:,.2f}" if appointment.effective_fee else (
            f"₱{appointment.fee:,.2f}" if appointment.fee else "—"
        )
        detail_html = (
            "<tr>"
            "<td style='padding:5px 0;color:#6b7280;font-size:13px;width:130px;'>Fee Paid</td>"
            f"<td style='padding:5px 0;font-size:13px;font-weight:600;color:#0d9488;'>{fee_display}</td>"
            "</tr>"
        )

    plain = (
        f"Hi {patient.first_name},\n\n"
        f"Your booking with {doctor_name} has been confirmed!\n\n"
        f"Patient: {patient_name}\n"
        f"Doctor: {doctor_name}\n"
        f"Date: {date_str}\nTime: {time_str}\n"
        f"Type: {apt_type_label}\n"
        f"Reference: {ref_number}\n\n"
        f"View: {apt_url}\n\n"
        f"— CareConnect Team"
    )

    html = f"""
    <div style="font-family:Poppins,sans-serif;max-width:540px;margin:auto;padding:32px;border:1px solid #e5e7eb;border-radius:12px;">
      <h2 style="color:#0d9488;">CareConnect</h2>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;padding:14px;margin:16px 0;">
        <p style="margin:0;font-weight:700;color:#15803d;">✓ Booking Confirmed!</p>
        <p style="margin:4px 0 0;font-size:13px;color:#166534;">Your booking with <strong>{doctor_name}</strong> has been confirmed.</p>
      </div>
      <p>Hi <strong>{patient.first_name}</strong>,</p>
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin:16px 0;">
        <table style="width:100%;">
          <tr><td style="color:#6b7280;padding:4px 0;width:130px;">Reference</td><td style="font-weight:700;color:#0d9488;font-family:monospace;padding:4px 0;">{ref_number}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Patient</td><td style="font-weight:600;padding:4px 0;">{patient_name}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Doctor</td><td style="font-weight:600;padding:4px 0;">{doctor_name}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Date</td><td style="padding:4px 0;">{date_str}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Time</td><td style="padding:4px 0;">{time_str}</td></tr>
          <tr><td style="color:#6b7280;padding:4px 0;">Type</td><td style="padding:4px 0;">{apt_type_label}</td></tr>
          {detail_html}
        </table>
      </div>
      <div style="text-align:center;margin:20px 0;">
        <a href="{apt_url}" style="background:#0d9488;color:#fff;padding:11px 28px;border-radius:8px;text-decoration:none;font-weight:600;">View Appointment</a>
      </div>
    </div>
    """

    recipients = [patient.email]

    try:
        send_mail(
            subject=f"Your booking with {doctor_name} has been confirmed",
            message=plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            html_message=html,
            fail_silently=False,
        )
        logger.info("Confirmation email sent to %s for apt #%s", recipients, appointment.pk)
    except Exception as exc:
        logger.warning("Confirmation email failed for apt #%s: %s", appointment.pk, exc)


def _notify(user, title, message, notif_type="appointment", data=None):
    try:
        Notification.objects.create(
            user=user, type=notif_type, title=title, message=message, data=data or {}
        )
    except Exception as exc:
        logger.warning("Notification creation failed: %s", exc)


def _issue_paymongo_refund(payment_id: str, amount) -> tuple[bool, str | None]:
    """
    Issue a full refund via PayMongo Refunds API (LIVE MODE).
    https://developers.paymongo.com/reference/create-a-refund

    Returns (success: bool, error_message: str | None).
    Amount must be in PHP (will be converted to centavos).

    PayMongo refund timeline:
      - GCash:        instant
      - Credit/Debit: 3–7 business days

    PRODUCTION SAFETY:
      - Never logs full payment_id or sensitive data
      - Handles network errors, insufficient balance, card declined
      - Returns user-friendly error messages
      - Checks if payment was already refunded before attempting
    """
    import requests
    from decimal import Decimal

    secret_key = getattr(settings, "PAYMONGO_SECRET_KEY", "")
    if not secret_key or len(secret_key) < 10:
        logger.warning("PayMongo secret key not configured — skipping refund.")
        return False, "Payment gateway not configured."

    if not payment_id:
        return False, "No payment ID on record."

    try:
        amount_centavos = int(Decimal(str(amount or 0)) * 100)
        if amount_centavos <= 0:
            return False, "Refund amount is zero."

        logger.info("[REFUND] Attempting refund: payment_id=...%s, amount=₱%.2f (%d centavos)",
                    payment_id[-8:] if len(payment_id) > 8 else "****", float(amount), amount_centavos)

        import base64
        token = base64.b64encode(f"{secret_key}:".encode()).decode()
        
        # STEP 1: Check if payment was already refunded by fetching payment details
        try:
            payment_resp = requests.get(
                f"https://api.paymongo.com/v1/payments/{payment_id}",
                headers={
                    "Authorization": f"Basic {token}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if payment_resp.status_code == 200:
                payment_data = payment_resp.json().get("data", {}).get("attributes", {})
                payment_amount = payment_data.get("amount", 0)
                refunded_amount = payment_data.get("refunded_amount", 0)
                payment_status = payment_data.get("status", "")
                
                logger.info("[REFUND] Payment status: %s, original=₱%.2f, refunded=₱%.2f",
                           payment_status, payment_amount / 100, refunded_amount / 100)
                
                # Check if already fully refunded
                if refunded_amount >= payment_amount:
                    logger.warning("[REFUND] Payment already fully refunded")
                    return False, "This payment has already been fully refunded."
                
                # Check if requested amount exceeds remaining refundable amount
                remaining_refundable = payment_amount - refunded_amount
                if amount_centavos > remaining_refundable:
                    logger.warning("[REFUND] Requested amount (₱%.2f) exceeds remaining refundable (₱%.2f)",
                                 amount_centavos / 100, remaining_refundable / 100)
                    # Adjust to refund only the remaining amount
                    amount_centavos = remaining_refundable
                    logger.info("[REFUND] Adjusted refund amount to ₱%.2f", amount_centavos / 100)
        except Exception as check_exc:
            logger.warning("[REFUND] Could not verify payment status: %s (proceeding anyway)", check_exc)
        
        # STEP 2: Issue the refund
        resp = requests.post(
            "https://api.paymongo.com/v1/refunds",
            json={
                "data": {
                    "attributes": {
                        "amount":     amount_centavos,
                        "payment_id": payment_id,
                        "reason":     "requested_by_customer",
                        "notes":      "CareConnect cancellation refund",
                    }
                }
            },
            headers={
                "Authorization": f"Basic {token}",
                "Content-Type":  "application/json",
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info("[REFUND SUCCESS] PayMongo refund successful for payment ending ...%s",
                       payment_id[-8:] if len(payment_id) > 8 else "****")
            return True, None

        # Parse PayMongo error response
        try:
            error_data = resp.json()
            errors = error_data.get("errors", [])
            if errors:
                error_detail = errors[0].get("detail", "")
                error_code = errors[0].get("code", "")
                logger.warning("[REFUND ERROR] PayMongo refund failed [%s]: %s", error_code, error_detail[:200])
                
                # Map common PayMongo error codes to user-friendly messages
                if "already_refunded" in error_code or "already refunded" in error_detail.lower():
                    return False, "This payment has already been refunded."
                elif "parameter_above_maximum" in error_code or "greater than the remaining" in error_detail.lower():
                    return False, "Refund amount exceeds the remaining refundable value. The payment may have been partially refunded already."
                elif "insufficient" in error_detail.lower():
                    return False, "Refund failed: insufficient balance in merchant account."
                elif "not_found" in error_code:
                    return False, "Payment not found or cannot be refunded."
                else:
                    return False, error_detail[:200]
            else:
                logger.warning("[REFUND ERROR] PayMongo refund failed [%s]: %s", resp.status_code, resp.text[:200])
                return False, f"Refund failed (HTTP {resp.status_code}). Please contact support."
        except Exception:
            logger.warning("[REFUND ERROR] PayMongo refund failed [%s]: %s", resp.status_code, resp.text[:200])
            return False, f"Refund failed (HTTP {resp.status_code}). Please contact support."

    except requests.Timeout:
        logger.warning("[REFUND ERROR] PayMongo refund timeout for payment ending ...%s",
                      payment_id[-8:] if len(payment_id) > 8 else "****")
        return False, "Refund request timed out. Please try again or contact support."
    except requests.ConnectionError:
        logger.warning("[REFUND ERROR] PayMongo refund connection error for payment ending ...%s",
                      payment_id[-8:] if len(payment_id) > 8 else "****")
        return False, "Could not connect to payment gateway. Please check your internet connection."
    except Exception as exc:
        logger.exception("[REFUND ERROR] PayMongo refund exception: %s", str(exc)[:200])
        return False, "An unexpected error occurred. Please contact support."


def _apply_hmo(patient, doctor_id, apt_type, base_fee):
    """Return (hmo_provider, coverage_percent, effective_fee)."""
    from doctors.models import DoctorHMO, PatientHMO
    hmo_card = (
        PatientHMO.objects
        .filter(patient=patient, verification_status="verified")
        .order_by("-coverage_percent")
        .first()
    )
    if not hmo_card:
        return "", 0, base_fee

    doctor_accepts = DoctorHMO.objects.filter(
        doctor__user_id=doctor_id, name__iexact=hmo_card.provider
    ).exists()
    if not doctor_accepts:
        return "", 0, base_fee

    pct = hmo_card.coverage_percent
    effective = round(base_fee * (1 - pct / 100), 2) if base_fee else base_fee
    return hmo_card.provider, pct, effective


class StandardPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# ── AppointmentViewSet ────────────────────────────────────────────────────────

class AppointmentViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]

    def _base_qs(self, user):
        qs = Appointment.objects.select_related("patient", "doctor", "doctor__doctor_profile")
        if _is_patient(user):
            return qs.filter(patient=user)
        if _is_doctor(user):
            return qs.filter(doctor=user)
        return qs

    def _get_appointment(self, pk, user):
        try:
            return self._base_qs(user).get(pk=pk)
        except Appointment.DoesNotExist:
            return None

    # LIST
    def list(self, request):
        qs = self._base_qs(request.user)
        for param, field in [("status", "status"), ("type", "type"), ("date", "date"), ("doctor", "doctor_id")]:
            val = request.query_params.get(param)
            if val:
                qs = qs.filter(**{field: val})
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(AppointmentListSerializer(page, many=True).data)

    # CREATE
    def create(self, request):
        """Create appointment with patient profile fields from frontend."""
        if not _is_patient(request.user):
            return Response({"detail": "Only patients can book appointments."}, status=status.HTTP_403_FORBIDDEN)

        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # On-demand: force type=online, date=today, time=now
        if data["type"] == "on_demand":
            data["type"] = "online"
            data["date"] = timezone.localdate()
            data["time"] = timezone.localtime().time()

        doctor = User.objects.select_related("doctor_profile").get(pk=data["doctor_id"])
        profile: DoctorProfile = doctor.doctor_profile

        base_fee = (
            profile.consultation_fee_online
            if data["type"] in ("online", "on_demand")
            else None  # in_clinic: fee paid in person, not charged online
        )

        # Snapshot clinic details at booking time
        clinic_snapshot = {}
        if data["type"] == "in_clinic":
            clinic_snapshot = {
                "clinic_name":    profile.clinic_name or "",
                "clinic_address": profile.clinic_address or "",
                "city":           profile.city or "",
            }

        hmo_provider, hmo_pct, _ = _apply_hmo(request.user, data["doctor_id"], data["type"], base_fee or 0)

        # ── Build booked_for_name from Patient Profile fields ──────────────────
        # Combine firstName + middleName + lastName into full name.
        # If all are empty, default to logged-in user's name.
        first_name = data.get("firstName", "").strip()
        middle_name = data.get("middleName", "").strip()
        last_name = data.get("lastName", "").strip()
        
        # Build full name from provided fields
        name_parts = [first_name, middle_name, last_name]
        full_name = " ".join(part for part in name_parts if part).strip()
        
        # booked_for_name stores the manually entered name.
        # If empty, it means booking for self (logged-in user).
        # We store the empty value as-is; serializers will display logged-in user's name when empty.
        
        # Use reasonForConsultation if provided, otherwise fall back to symptoms
        symptoms_text = data.get("reasonForConsultation", "").strip() or data.get("symptoms", "").strip()

        with transaction.atomic():
            existing = (
                Appointment.objects
                .select_for_update()
                .filter(doctor=doctor, date=data["date"])
                .exclude(status__in=["cancelled", "no_show"])
            )
            next_queue = existing.count() + 1

            paymongo_payment_id = data.get("paymongo_payment_id", "").strip()
            is_online_type = data["type"] in ("online", "on_demand")
            if is_online_type and paymongo_payment_id:
                payment_status = "paid"
            elif is_online_type:
                payment_status = "awaiting"
            else:
                # in_clinic: no online payment — fee is None, payment handled at clinic
                payment_status = "pending"
                base_fee = None

            # ── Create PatientProfile from submitted form fields ──────────────
            patient_profile = None
            date_of_birth = data.get("dateOfBirth")
            email_field = data.get("email", "").strip()
            sex_field = data.get("sex", "").strip()
            home_address = data.get("homeAddress", "").strip()
            if first_name and last_name:
                patient_profile = PatientProfile.objects.create(
                    account_owner=request.user,
                    first_name=first_name,
                    middle_name=middle_name,
                    last_name=last_name,
                    date_of_birth=date_of_birth or request.user.birthdate,
                    email=email_field or request.user.email,
                    sex=sex_field,
                    home_address=home_address,
                )

            appointment = Appointment.objects.create(
                patient=request.user,
                doctor=doctor,
                date=data["date"],
                time=data["time"],
                type=data["type"],
                symptoms=symptoms_text,
                notes=data.get("notes", ""),
                fee=base_fee,
                is_on_demand=profile.is_on_demand,
                queue_number=next_queue,
                payment_status=payment_status,
                paymongo_payment_id=paymongo_payment_id,
                hmo_provider=hmo_provider,
                hmo_coverage_percent=hmo_pct,
                pre_consult_files=data.get("pre_consult_files", []),
                clinic_info_snapshot=clinic_snapshot,
                booked_for_name=full_name if full_name else "",
                patient_profile=patient_profile,
            )

        # Notify doctor
        _notify(
            doctor,
            title="New Appointment Request",
            message=(
                f"{full_name} (booked by {request.user.first_name} {request.user.last_name}) "
                f"booked a {data['type']} appointment on {data['date']} at {data['time']}."
            ),
            data={"appointment_id": appointment.pk},
        )

        threading.Thread(
            target=_send_booking_under_review_email,
            args=(appointment,),
            daemon=True,
        ).start()

        # If payment was already confirmed at booking time, fire receipt + doctor notification
        if paymongo_payment_id and payment_status == "paid":
            try:
                from notifications.tasks import (
                    send_patient_payment_receipt,
                    send_doctor_payment_notification,
                )
                send_patient_payment_receipt.delay(appointment.pk)
                send_doctor_payment_notification.delay(appointment.pk)
            except Exception as exc:
                logger.warning("Payment notification tasks failed for apt #%s: %s", appointment.pk, exc)

        return Response(AppointmentDetailSerializer(appointment).data, status=status.HTTP_201_CREATED)

    # RETRIEVE
    def retrieve(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(AppointmentDetailSerializer(apt).data)

    # PARTIAL UPDATE
    def partial_update(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if _is_patient(request.user):
            return Response({"detail": "Use /cancel/ action."}, status=status.HTTP_403_FORBIDDEN)
        serializer = AppointmentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(apt, field, value)
        apt.save()
        return Response(AppointmentDetailSerializer(apt).data)

    # CONFIRM PAYMENT (patient only — store PayMongo payment ID, mark as paid)
    @action(detail=True, methods=["post"], url_path="confirm_payment")
    def confirm_payment(self, request, pk=None):
        """
        Called by the frontend after PayMongo checkout succeeds.
        Stores the paymongo_payment_id and sets payment_status=paid.
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_patient(request.user):
            return Response({"detail": "Only patients can confirm payment."}, status=status.HTTP_403_FORBIDDEN)
        if apt.type not in ("online", "on_demand"):
            return Response({"detail": "Payment confirmation is only for online appointments."}, status=status.HTTP_400_BAD_REQUEST)
        if apt.payment_status == "paid":
            return Response(AppointmentDetailSerializer(apt).data)

        payment_id = (request.data.get("paymongo_payment_id") or "").strip()
        if not payment_id:
            return Response({"detail": "paymongo_payment_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        apt.paymongo_payment_id = payment_id
        apt.payment_status = "paid"
        apt.save(update_fields=["paymongo_payment_id", "payment_status", "updated_at"])

        # Fire Celery tasks: patient receipt email + doctor payment notification
        # NowServing alignment: both sides are informed immediately after payment.
        try:
            from notifications.tasks import (
                send_patient_payment_receipt,
                send_doctor_payment_notification,
            )
            send_patient_payment_receipt.delay(apt.pk)
            send_doctor_payment_notification.delay(apt.pk)
        except Exception as exc:
            logger.warning("Payment notification tasks failed for apt #%s: %s", apt.pk, exc)

        return Response(AppointmentDetailSerializer(apt).data)

    # ACCEPT
    @action(detail=True, methods=["post"], url_path="accept")
    def accept(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can accept."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status != "pending":
            return Response({"detail": f"Cannot accept status '{apt.status}'."}, status=status.HTTP_400_BAD_REQUEST)

        apt.status = "confirmed"
        apt.save(update_fields=["status", "updated_at"])

        # Auto-create Jitsi room for on_demand/online on accept
        if apt.type in ("online", "on_demand") and not apt.video_room_id:
            room_id = _generate_room_name(apt.id)
            apt.video_room_id = room_id
            apt.video_password = _generate_password()
            apt.video_link = _jitsi_url(room_id)
            apt.save(update_fields=["video_room_id", "video_password", "video_link", "updated_at"])

        _notify(
            apt.patient,
            title="Appointment Confirmed",
            message=f"Dr. {request.user.first_name} {request.user.last_name} confirmed your appointment on {apt.date} at {apt.time}.",
            data={"appointment_id": apt.pk, "video_link": apt.video_link},
        )

        # Send "Your booking with Dr. X has been confirmed" email
        threading.Thread(
            target=_send_appointment_confirmed_email,
            args=(apt,),
            daemon=True,
        ).start()

        return Response(AppointmentDetailSerializer(apt).data)

    # REJECT
    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can reject."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status not in ("pending", "confirmed"):
            return Response({"detail": f"Cannot reject status '{apt.status}'."}, status=status.HTTP_400_BAD_REQUEST)

        reason = request.data.get("rejection_reason", "").strip()

        refund_issued = False
        refund_error = None
        if apt.status == "confirmed" and apt.payment_status == "paid" and apt.paymongo_payment_id:
            refund_issued, refund_error = _issue_paymongo_refund(
                apt.paymongo_payment_id, apt.effective_fee or apt.fee
            )

        apt.status = "cancelled"
        apt.rejection_reason = reason
        apt.cancelled_by = request.user
        if refund_issued:
            apt.payment_status = "refunded"
            apt.refunded_at = timezone.now()
        apt.save(update_fields=["status", "rejection_reason", "cancelled_by", "payment_status", "refunded_at", "updated_at"])

        _notify(
            apt.patient,
            title="Appointment Not Accepted",
            message=f"Dr. {request.user.first_name} {request.user.last_name} could not accept your appointment."
                    + (f" Reason: {reason}" if reason else "")
                    + (" A refund has been issued." if refund_issued else ""),
            data={"appointment_id": apt.pk},
        )
        return Response(AppointmentDetailSerializer(apt).data)

    # START VIDEO CONSULTATION (doctor only)
    @action(detail=True, methods=["post"], url_path="start_video")
    def start_video_consultation(self, request, pk=None):
        """
        NowServing pattern: Doctor clicks "Start Video" → room is created →
        patient receives a real-time Channels push (video.started) with the
        Jitsi room name + password so their browser can auto-join.

        Returns: { room_name, password, jitsi_domain, video_room_url, appointment }
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can start video consultations."}, status=status.HTTP_403_FORBIDDEN)
        if apt.type not in ("online", "on_demand"):
            return Response({"detail": "Video is only for online/on-demand appointments."}, status=status.HTTP_400_BAD_REQUEST)
        if apt.status not in ("confirmed", "pending", "in_progress"):
            return Response({"detail": f"Cannot start video from status '{apt.status}'."}, status=status.HTTP_400_BAD_REQUEST)

        today = timezone.localdate()
        if apt.date != today:
            return Response(
                {"detail": f"Video consultation can only be started on the scheduled date ({apt.date})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        jitsi_domain = _get_jitsi_domain()

        # Re-use existing room if already in_progress (doctor rejoined after refresh)
        if apt.status == "in_progress" and apt.video_room_id:
            room_name = apt.video_room_id
            password  = apt.video_password
        else:
            room_name = _generate_room_name(apt.id)
            password  = _generate_password()
            apt.video_room_id    = room_name
            apt.video_password   = password
            apt.video_link       = _jitsi_url(room_name)
            apt.video_started_at = timezone.now()
            apt.status           = "in_progress"
            apt.save(update_fields=[
                "video_room_id", "video_password", "video_link",
                "video_started_at", "status", "updated_at",
            ])

        video_room_url = _jitsi_url(room_name)

        # Notify patient via DB notification (shows in notification bell)
        _notify(
            apt.patient,
            title="Your Consultation Has Started! 🎥",
            message=(
                f"Dr. {request.user.first_name} {request.user.last_name} has started "
                f"your video consultation. Tap 'Join Now' to connect."
            ),
            notif_type="appointment",
            data={
                "appointment_id": apt.pk,
                "video_room_url": video_room_url,
                "room_name":      room_name,
                "password":       password,
                "jitsi_domain":   jitsi_domain,
            },
        )

        # Real-time push via Django Channels — patient's browser receives
        # video.started and immediately shows the "Join Now" button / Jitsi iframe.
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()

            # video.started carries full room credentials so patient can join
            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type":           "video.started",
                    "appointment_id": apt.pk,
                    "room_name":      room_name,
                    "password":       password,
                    "jitsi_domain":   jitsi_domain,
                    "video_room_url": video_room_url,
                },
            )
            # Also send status.changed so any status-watching component updates
            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type":           "status.changed",
                    "appointment_id": apt.pk,
                    "status":         "in_progress",
                },
            )
        except Exception as exc:
            logger.warning("Channels broadcast failed (start_video): %s", exc)

        _broadcast_queue_update(apt.doctor_id, apt.date)

        return Response({
            "room_name":      room_name,
            "password":       password,
            "jitsi_domain":   jitsi_domain,
            "video_room_url": video_room_url,
            "appointment":    AppointmentDetailSerializer(apt).data,
        })

    # SHARE DOCUMENT (doctor only)
    @action(detail=True, methods=["post"], url_path="share_document")
    def share_document(self, request, pk=None):
        """
        Share a document during an active consult.
        Payload:
          { doc_type: "prescription"|"certificate"|"lab", ...fields }
        Creates document, attaches to appointment, notifies patient, broadcasts via Channels.
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can share documents."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status not in ("in_progress", "completed"):
            return Response({"detail": "Documents can be shared during or after consultation."}, status=status.HTTP_400_BAD_REQUEST)

        doc_type = (request.data.get("doc_type") or "").strip()
        if doc_type not in ("prescription", "certificate", "lab"):
            return Response({"detail": "Invalid doc_type."}, status=status.HTTP_400_BAD_REQUEST)

        pdf_url = None
        with transaction.atomic():
            if doc_type == "prescription":
                from records.models import Prescription
                from datetime import date as date_type
                diagnosis = (request.data.get("diagnosis") or "").strip()
                medications_raw = request.data.get("medications") or []
                instructions = (request.data.get("instructions") or "").strip()
                valid_until = request.data.get("valid_until")
                follow_up_date_raw = request.data.get("follow_up_date") or request.data.get("followUpDate")
                follow_up_date = None
                follow_up_date_str = None
                if follow_up_date_raw:
                    try:
                        follow_up_date = date_type.fromisoformat(str(follow_up_date_raw))
                        follow_up_date_str = follow_up_date.isoformat()
                    except ValueError:
                        return Response(
                            {"detail": "Invalid follow_up_date. Use YYYY-MM-DD."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                remarks = request.data.get("remarks") or ""

                if not diagnosis:
                    return Response({"detail": "diagnosis is required."}, status=status.HTTP_400_BAD_REQUEST)

                medications = []
                if isinstance(medications_raw, str):
                    trimmed = medications_raw.strip()
                    if trimmed.startswith("{") or trimmed.startswith("["):
                        try:
                            parsed = json.loads(trimmed)
                            if isinstance(parsed, dict):
                                medications = parsed.get("medications") or []
                                meta = parsed.get("meta") or {}
                                if follow_up_date_str:
                                    meta["follow_up_date"] = follow_up_date_str
                                if remarks:
                                    meta["remarks"] = remarks
                                if meta:
                                    medications.append({"_meta": meta})
                            elif isinstance(parsed, list):
                                medications = parsed
                        except Exception:
                            medications = [m.strip() for m in trimmed.split(",") if m.strip()]
                    else:
                        medications = [m.strip() for m in trimmed.split(",") if m.strip()]
                elif isinstance(medications_raw, list):
                    medications = medications_raw
                else:
                    medications = []

                if follow_up_date_str or remarks:
                    meta = {"follow_up_date": follow_up_date_str, "remarks": remarks}
                    medications.append({"_meta": {k: v for k, v in meta.items() if v}})

                if not valid_until:
                    # Default: 30 days validity if not provided
                    from datetime import date as date_type
                    valid_until = date_type.today() + timedelta(days=30)
                rx = Prescription.objects.create(
                    appointment=apt,
                    patient=apt.patient,
                    doctor=request.user,
                    diagnosis=diagnosis,
                    medications=medications,
                    instructions=instructions,
                    valid_until=valid_until,
                    is_digital=True,
                )
                follow_up_invitation = None
                if follow_up_date:
                    follow_up_invitation = FollowUpInvitation.objects.create(
                        appointment=apt,
                        prescription=rx,
                        patient=apt.patient,
                        follow_up_date=follow_up_date,
                    )
                    try:
                        from notifications.tasks import send_follow_up_invitation_notification
                        transaction.on_commit(
                            lambda: send_follow_up_invitation_notification.delay(follow_up_invitation.pk)
                        )
                    except Exception as exc:
                        logger.warning("Follow-up invitation notification task failed: %s", exc)
                # Generate PDF (best effort)
                try:
                    from records.views import generate_prescription_pdf
                    generate_prescription_pdf(rx, request=request)
                    rx.refresh_from_db()
                except Exception as exc:
                    logger.warning("PDF generation failed for Rx #%s: %s", rx.pk, exc)
                if rx.pdf_file:
                    try:
                        pdf_url = request.build_absolute_uri(rx.pdf_file.url)
                    except Exception:
                        pdf_url = rx.pdf_file.url
                title = "Prescription"
                summary = diagnosis[:120]
                document_id = rx.pk

            elif doc_type == "certificate":
                from records.models import MedicalCertificate
                purpose = (request.data.get("purpose") or "").strip()
                diagnosis = (request.data.get("diagnosis") or "").strip()
                rest_days = int(request.data.get("rest_days") or 0)
                valid_from = request.data.get("valid_from")
                valid_until = request.data.get("valid_until")
                if not purpose or not diagnosis or not valid_from or not valid_until:
                    return Response({"detail": "purpose, diagnosis, valid_from, valid_until are required."}, status=status.HTTP_400_BAD_REQUEST)
                cert = MedicalCertificate.objects.create(
                    appointment=apt,
                    patient=apt.patient,
                    doctor=request.user,
                    purpose=purpose,
                    diagnosis=diagnosis,
                    rest_days=rest_days,
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
                title = "Medical Certificate"
                summary = purpose[:120]
                document_id = cert.pk

            else:
                from records.models import LabResult
                test_name = (request.data.get("test_name") or "").strip()
                test_type = (request.data.get("test_type") or "General").strip()
                notes = (request.data.get("notes") or "").strip()
                if not test_name:
                    return Response({"detail": "test_name is required."}, status=status.HTTP_400_BAD_REQUEST)
                lab = LabResult.objects.create(
                    appointment=apt,
                    patient=apt.patient,
                    doctor=request.user,
                    test_name=test_name,
                    test_type=test_type,
                    notes=notes,
                    status="pending",
                )
                title = "Lab Request"
                summary = test_name[:120]
                document_id = lab.pk

            share = AppointmentShare.objects.create(
                appointment=apt,
                doc_type=doc_type,
                document_id=document_id,
                title=title,
                summary=summary,
                created_by=request.user,
            )

        _notify(
            apt.patient,
            title="New document shared",
            message=f"Dr. {request.user.first_name} {request.user.last_name} shared a {title.lower()} during your consult.",
            data={"appointment_id": apt.pk, "doc_type": doc_type, "document_id": document_id, "pdf_url": pdf_url},
        )

        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type": "document.shared",
                    "appointment_id": apt.pk,
                    "doc_type": doc_type,
                    "document_id": document_id,
                    "title": title,
                    "summary": summary,
                    "created_at": share.created_at.isoformat(),
                    "pdf_url": pdf_url,
                },
            )
        except Exception as exc:
            logger.warning("Document share broadcast failed: %s", exc)

        return Response({
            "share": {
                "id": share.pk,
                "doc_type": share.doc_type,
                "document_id": share.document_id,
                "title": share.title,
                "summary": share.summary,
                "created_at": share.created_at,
                "pdf_url": pdf_url,
            },
            "appointment": AppointmentDetailSerializer(apt).data,
        }, status=status.HTTP_201_CREATED)

    # START CONSULT
    @action(detail=True, methods=["post"], url_path="start_consult")
    def start_consult(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can start consultations."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status != "confirmed":
            return Response({"detail": f"Must be confirmed. Current: '{apt.status}'."}, status=status.HTTP_400_BAD_REQUEST)

        apt.status = "in_progress"
        if apt.type in ("online", "on_demand"):
            if not apt.video_room_id:
                room_id = _generate_room_name(apt.id)
                apt.video_room_id = room_id
                apt.video_password = apt.video_password or _generate_password()
                apt.video_link = _jitsi_url(room_id)
            apt.chat_room_id = apt.chat_room_id or uuid.uuid4()

        apt.save(update_fields=["status", "video_room_id", "video_password", "video_link", "chat_room_id", "updated_at"])

        msg = f"Dr. {request.user.first_name} {request.user.last_name} has started your consultation."
        if apt.video_link:
            msg += f" Join: {apt.video_link}"
        _notify(apt.patient, title="Consultation Started", message=msg,
                data={"appointment_id": apt.pk, "video_link": apt.video_link})
        return Response(AppointmentDetailSerializer(apt).data)

    # COMPLETE
    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        """
        NowServing pattern: Doctor clicks "End Consultation" →
        - Saves duration, notes, summary, transcript stub
        - Marks appointment completed
        - Broadcasts consultation.ended via Channels so patient UI closes the room
        - Notifies patient they can now leave a review
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (_is_doctor(request.user) or _is_admin(request.user)):
            return Response({"detail": "Only doctors/admins can complete."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status not in ("in_progress", "confirmed"):
            return Response({"detail": "Appointment must be in_progress or confirmed."}, status=status.HTTP_400_BAD_REQUEST)

        transcript       = request.data.get("transcript", "").strip()
        consult_notes    = request.data.get("consult_notes", "").strip()
        consult_summary  = request.data.get("consult_summary", "").strip()
        participants     = request.data.get("participants")
        duration_seconds = request.data.get("duration_seconds")

        apt.status         = "completed"
        apt.video_ended_at = timezone.now()

        # Back-calculate video_started_at if not already set (e.g. on-demand)
        if duration_seconds and not apt.video_started_at:
            try:
                apt.video_started_at = apt.video_ended_at - timedelta(seconds=int(duration_seconds))
            except (TypeError, ValueError):
                pass

        if isinstance(participants, list):
            apt.video_participants = participants
        if transcript:
            apt.consult_transcript = transcript
        if consult_notes:
            apt.consult_notes = consult_notes
        if consult_summary:
            apt.consult_summary = consult_summary

        apt.save(update_fields=[
            "status", "consult_transcript", "video_started_at",
            "video_ended_at", "video_participants",
            "consult_notes", "consult_summary", "updated_at",
        ])

        # Persist transcript in records app for longitudinal patient history
        try:
            from records.models import ConsultTranscript
            ConsultTranscript.objects.update_or_create(
                appointment=apt,
                defaults={
                    "patient":          apt.patient,
                    "doctor":           apt.doctor,
                    "notes":            consult_notes or transcript,
                    "summary":          consult_summary,
                    "duration_seconds": apt.video_duration_seconds or 0,
                },
            )
        except Exception as exc:
            logger.warning("ConsultTranscript save failed: %s", exc)

        duration_min = round((apt.video_duration_seconds or 0) / 60, 1)

        _notify(
            apt.patient,
            title="Consultation Completed ✅",
            message=(
                f"Your {duration_min}-minute consultation with "
                f"Dr. {apt.doctor.first_name} {apt.doctor.last_name} is complete. "
                f"You can now leave a review."
            ),
            data={"appointment_id": apt.pk},
        )

        # Broadcast two events:
        # 1. consultation.ended  — patient Jitsi iframe closes gracefully
        # 2. status.changed      — any status-watching component refreshes
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()

            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type":             "consultation.ended",
                    "appointment_id":   apt.pk,
                    "duration_seconds": apt.video_duration_seconds or 0,
                    "duration_minutes": duration_min,
                },
            )
            async_to_sync(channel_layer.group_send)(
                f"appointment_{apt.pk}",
                {
                    "type":           "status.changed",
                    "appointment_id": apt.pk,
                    "status":         "completed",
                },
            )
        except Exception as exc:
            logger.warning("Channels broadcast failed (complete): %s", exc)

        _broadcast_queue_update(apt.doctor_id, apt.date)
        return Response(AppointmentDetailSerializer(apt).data)

    # CALL NEXT (doctor only)
    @action(detail=True, methods=["post"], url_path="call_next")
    def call_next(self, request, pk=None):
        """
        Advance queue to this appointment and notify waiting patients.
        Sets status -> in_progress for the selected appointment.
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can call next."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status not in ("confirmed", "pending"):
            return Response({"detail": f"Cannot call next from status '{apt.status}'."}, status=status.HTTP_400_BAD_REQUEST)

        if apt.type in ("online", "on_demand") and not apt.video_room_id:
            room_id = _generate_room_name(apt.id)
            apt.video_room_id = room_id
            apt.video_password = apt.video_password or _generate_password()
            apt.video_link = _jitsi_url(room_id)

        apt.status = "in_progress"
        apt.save(update_fields=["status", "video_room_id", "video_password", "video_link", "updated_at"])

        _notify(
            apt.patient,
            title="You are now being called",
            message=f"Dr. {request.user.first_name} {request.user.last_name} is ready for your consultation.",
            data={"appointment_id": apt.pk},
        )

        _broadcast_queue_update(apt.doctor_id, apt.date)
        return Response(AppointmentDetailSerializer(apt).data)

    # CANCEL (patient only — pending → auto refund; confirmed → must message doctor)
    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """
        NowServing pattern:
        - pending:   patient cancels directly → PayMongo refund if paid → status=cancelled
        - confirmed: patient cannot cancel directly → 403 with message to contact doctor
        - in_progress/completed/cancelled: blocked
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (_is_patient(request.user) or _is_admin(request.user)):
            return Response({"detail": "Only patient or admin can cancel."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status in ("completed", "cancelled", "no_show", "in_progress"):
            return Response(
                {"detail": f"Cannot cancel an appointment with status '{apt.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Confirmed → patient must go through doctor
        if apt.status == "confirmed" and _is_patient(request.user):
            return Response(
                {
                    "detail": "This appointment has already been confirmed by your doctor. "
                              "Please message your doctor to request cancellation and refund.",
                    "action_required": "message_doctor",
                    "doctor_id": str(apt.doctor_id),
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = CancelAppointmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data["reason"]

        refund_issued = False
        refund_error  = None

        # Auto-refund if payment was made (pending status = not yet accepted)
        if apt.payment_status == "paid" and apt.paymongo_payment_id:
            refund_issued, refund_error = _issue_paymongo_refund(
                apt.paymongo_payment_id, apt.effective_fee or apt.fee
            )

        with transaction.atomic():
            apt.status        = "cancelled"
            apt.cancelled_by  = request.user
            apt.cancel_reason = reason
            if refund_issued:
                apt.payment_status = "refunded"
                apt.refunded_at    = timezone.now()
            apt.save(update_fields=[
                "status", "cancelled_by", "cancel_reason",
                "payment_status", "refunded_at", "updated_at",
            ])

        profile      = apt.patient_profile
        patient_name = (
            (f"{profile.first_name} {profile.last_name}".strip() if profile else "") or
            apt.booked_for_name or
            f"{apt.patient.first_name} {apt.patient.last_name}".strip()
        )
        patient_age    = profile.age if profile else ""
        patient_gender = profile.sex if profile else ""
        patient_info   = patient_name
        if patient_age:
            patient_info += f", {patient_age} years old"
        if patient_gender:
            patient_info += f", {patient_gender.capitalize()}"
        doctor_name  = f"Dr. {apt.doctor.first_name} {apt.doctor.last_name}".strip()

        # Notify doctor
        _notify(
            apt.doctor,
            title="Appointment Cancelled",
            message=f"{patient_info} cancelled their appointment on {apt.date} at {apt.time}."
                    + (f" Reason: {reason}" if reason else ""),
            data={"appointment_id": apt.pk},
        )

        # Notify patient with clear refund timeline
        refund_timeline = (
            "Your refund has been processed. GCash/Maya: typically instant. Credit/Debit cards: 3–7 business days."
            if refund_issued else
            (refund_error if refund_error else "No refund applicable (not paid online).")
        )
        _notify(
            apt.patient,
            title="Appointment Cancelled" + (" & Refunded" if refund_issued else ""),
            message=(
                f"Your appointment for {patient_info} with {doctor_name} on {apt.date} has been cancelled."
                + (f" {refund_timeline}" if refund_issued or refund_error else "")
            ),
            data={"appointment_id": apt.pk, "refund_issued": refund_issued},
        )

        # Celery task for email notifications
        # TESTING: To send emails synchronously (bypass Celery), uncomment the next 2 lines and comment out the .delay() calls:
        # send_appointment_cancelled_email(apt.pk, refund_issued, reason or "")
        # send_doctor_cancellation_notification(apt.pk, reason or "")
        try:
            from notifications.tasks import send_appointment_cancelled_email, send_doctor_cancellation_notification
            send_appointment_cancelled_email.delay(apt.pk, refund_issued, reason or "")
            send_doctor_cancellation_notification.delay(apt.pk, reason or "")
        except Exception as exc:
            logger.warning("Cancellation notification task failed: %s", exc)

        return Response({
            **AppointmentDetailSerializer(apt).data,
            "refund_issued": refund_issued,
            "refund_note": refund_timeline,
        })

    # REFUND & CANCEL (doctor only — confirmed → trigger PayMongo refund)
    @action(detail=True, methods=["post"], url_path="refund")
    def refund(self, request, pk=None):
        """
        NowServing pattern: doctor approves patient's cancellation request.
        - Only callable on confirmed appointments
        - Issues PayMongo refund if payment_status=paid
        - Sets status=cancelled, payment_status=refunded
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (_is_doctor(request.user) or _is_admin(request.user)):
            return Response({"detail": "Only doctors can approve refunds."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status != "confirmed":
            return Response(
                {"detail": f"Can only refund confirmed appointments. Current status: '{apt.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = CancelAppointmentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        reason = ser.validated_data["reason"]

        refund_issued = False
        refund_error  = None

        if apt.payment_status == "paid" and apt.paymongo_payment_id:
            refund_issued, refund_error = _issue_paymongo_refund(
                apt.paymongo_payment_id, apt.effective_fee or apt.fee
            )

        with transaction.atomic():
            apt.status        = "cancelled"
            apt.cancelled_by  = request.user
            apt.cancel_reason = reason
            if refund_issued:
                apt.payment_status = "refunded"
                apt.refunded_at    = timezone.now()
            apt.save(update_fields=[
                "status", "cancelled_by", "cancel_reason",
                "payment_status", "refunded_at", "updated_at",
            ])

        doctor_name  = f"Dr. {request.user.first_name} {request.user.last_name}".strip()
        profile      = apt.patient_profile
        patient_name = (
            (f"{profile.first_name} {profile.last_name}".strip() if profile else "") or
            apt.booked_for_name or
            f"{apt.patient.first_name} {apt.patient.last_name}".strip()
        )
        patient_age    = profile.age if profile else ""
        patient_gender = profile.sex if profile else ""
        patient_info   = patient_name
        if patient_age:
            patient_info += f", {patient_age} years old"
        if patient_gender:
            patient_info += f", {patient_gender.capitalize()}"

        # Notify patient with clear refund timeline
        refund_timeline = (
            "Your payment has been refunded to your original payment method. "
            "GCash/Maya: typically instant. Credit/Debit cards: 3–7 business days."
            if refund_issued else
            (refund_error if refund_error else "No refund applicable (not paid online).")
        )
        _notify(
            apt.patient,
            title="Appointment Cancelled & Refunded" if refund_issued else "Appointment Cancelled",
            message=(
                f"{doctor_name} has cancelled the appointment for {patient_info} on {apt.date}."
                + (f" {refund_timeline}" if refund_issued or refund_error else "")
                + (f" Reason: {reason}" if reason else "")
            ),
            data={"appointment_id": apt.pk, "refund_issued": refund_issued},
        )
        _notify(
            apt.doctor,
            title="Refund Processed" if refund_issued else "Appointment Cancelled",
            message=f"You cancelled {patient_info}'s appointment on {apt.date}."
                    + (" Refund issued to patient." if refund_issued else ""),
            data={"appointment_id": apt.pk},
        )

        # Celery task for email notifications
        # TESTING: To send emails synchronously (bypass Celery), uncomment the next 2 lines and comment out the .delay() calls:
        # send_appointment_cancelled_email(apt.pk, refund_issued, reason or "")
        # send_doctor_cancellation_notification(apt.pk, reason or "")
        try:
            from notifications.tasks import send_appointment_cancelled_email, send_doctor_cancellation_notification
            send_appointment_cancelled_email.delay(apt.pk, refund_issued, reason or "", cancelled_by_doctor=True)
            send_doctor_cancellation_notification.delay(apt.pk, reason or "")
        except Exception as exc:
            logger.warning("Refund notification task failed: %s", exc)

        return Response({
            **AppointmentDetailSerializer(apt).data,
            "refund_issued": refund_issued,
            "refund_note": refund_timeline,
        })

    # SUBMIT REVIEW (patient only — completed appointments, one per appointment)
    @action(detail=True, methods=["post"], url_path="review")
    def submit_review(self, request, pk=None):
        """
        NowServing pattern: patient leaves a star rating + optional comment
        after a completed appointment. One review per appointment enforced.
        POST /appointments/<id>/review/
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_patient(request.user):
            return Response({"detail": "Only patients can submit reviews."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status != "completed":
            return Response({"detail": "You can only review completed appointments."}, status=status.HTTP_400_BAD_REQUEST)
        if hasattr(apt, "review"):
            return Response({"detail": "You have already reviewed this appointment."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ReviewCreateSerializer(data={**request.data, "appointment_id": int(pk)}, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        review = Review.objects.create(
            appointment=apt,
            patient=request.user,
            doctor=apt.doctor,
            rating=data["rating"],
            comment=data.get("comment", ""),
        )
        _notify(
            apt.doctor,
            title="New Review Received ⭐",
            message=f"{request.user.first_name} {request.user.last_name} left a {data['rating']}-star review for your consultation on {apt.date}.",
            data={"appointment_id": apt.pk, "review_id": review.pk},
        )
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)

    # REPLY TO REVIEW (doctor only — public reply visible on profile)
    @action(detail=True, methods=["patch"], url_path="review/reply")
    def reply_to_review(self, request, pk=None):
        """
        NowServing pattern: doctor publicly replies to a patient review.
        PATCH /appointments/<id>/review/reply/
        """
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can reply to reviews."}, status=status.HTTP_403_FORBIDDEN)
        if not hasattr(apt, "review"):
            return Response({"detail": "No review found for this appointment."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ReviewReplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        review = apt.review
        review.doctor_reply = serializer.validated_data["reply"]
        review.reply_at = timezone.now()
        review.save(update_fields=["doctor_reply", "reply_at"])
        _notify(
            apt.patient,
            title="Your doctor replied to your review",
            message=f"Dr. {request.user.first_name} {request.user.last_name} replied to your review.",
            data={"appointment_id": apt.pk, "review_id": review.pk},
        )
        return Response(ReviewSerializer(review).data)

    # NO SHOW
    @action(detail=True, methods=["post"], url_path="no_show")
    def no_show(self, request, pk=None):
        apt = self._get_appointment(pk, request.user)
        if not apt:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (_is_doctor(request.user) or _is_admin(request.user)):
            return Response({"detail": "Only doctors/admins."}, status=status.HTTP_403_FORBIDDEN)
        if apt.status not in ("confirmed", "in_progress"):
            return Response({"detail": "Must be confirmed or in_progress."}, status=status.HTTP_400_BAD_REQUEST)
        apt.status = "no_show"
        apt.save(update_fields=["status", "updated_at"])
        _broadcast_queue_update(apt.doctor_id, apt.date)
        return Response(AppointmentDetailSerializer(apt).data)

    # TODAY'S QUEUE
    @action(detail=False, methods=["get"], url_path="queue/today")
    def queue_today(self, request):
        if not (_is_doctor(request.user) or _is_admin(request.user)):
            return Response({"detail": "Doctors only."}, status=status.HTTP_403_FORBIDDEN)
        today = timezone.localdate()
        queue = (
            Appointment.objects
            .select_related("patient", "doctor", "doctor__doctor_profile")
            .filter(doctor=request.user, date=today, status__in=["confirmed", "in_progress", "pending"])
            .order_by("queue_number")
        )
        return Response(AppointmentListSerializer(queue, many=True).data)

    # UPCOMING
    @action(detail=False, methods=["get"], url_path="upcoming")
    def upcoming(self, request):
        today = timezone.localdate()
        qs = (
            self._base_qs(request.user)
            .filter(date__gte=today, date__lte=today + timedelta(days=7))
            .exclude(status__in=["cancelled", "no_show", "completed"])
            .order_by("date", "time")
        )
        return Response(AppointmentListSerializer(qs, many=True).data)

    # AVAILABLE SLOTS
    @action(detail=False, methods=["get"], url_path=r"slots/(?P<doctor_id>\d+)", permission_classes=[AllowAny])
    def available_slots(self, request, doctor_id=None):
        """
        Returns 30-min availability slots for a doctor on a given date.

        Slot resolution order (NowServing.ph pattern):
          1. Explicit DoctorAvailableSlot rows for the date → use those.
          2. Otherwise → auto-generate from profile.weekly_schedule.
          3. Slots overlapping active Appointments are marked is_booked=True
             and is_available=False.

        get_effective_slots_for_date() returns dicts with keys:
          time, end_time, is_available, is_booked, slot_id
        """
        from datetime import date as date_type
        from doctors.models import DoctorAvailableSlot
        from doctors.utils import get_effective_slots_for_date

        date_str = request.query_params.get("date")
        if not date_str:
            return Response({"detail": "date query param required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            target_date = date_type.fromisoformat(date_str)
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        # Accept either user ID or profile ID
        doctor = None
        try:
            doctor = User.objects.select_related("doctor_profile").get(pk=doctor_id, role="doctor", is_active=True)
        except User.DoesNotExist:
            pass

        profile = getattr(doctor, "doctor_profile", None) if doctor else None

        # Fallback: treat doctor_id as a profile ID
        if not profile:
            try:
                profile = DoctorProfile.objects.select_related("user").get(pk=doctor_id, user__is_active=True)
                doctor = profile.user
            except DoctorProfile.DoesNotExist:
                return Response({"detail": "Doctor not found."}, status=status.HTTP_404_NOT_FOUND)

        if not profile.is_verified:
            return Response({"detail": "Doctor not accepting bookings."}, status=status.HTTP_400_BAD_REQUEST)

        # get_effective_slots_for_date returns list of dicts (already includes is_booked)
        slots = get_effective_slots_for_date(profile, target_date)

        return Response({
            "doctor_id":        doctor.pk,
            "doctor_name":      f"Dr. {doctor.first_name} {doctor.last_name}".strip(),
            "is_on_demand":     profile.is_on_demand,
            "is_available_now": profile.is_available_now,
            "date":             date_str,
            "slots":            slots,
        })


# ── Follow-up Invitations ────────────────────────────────────────────────────

class FollowUpInvitationListView(APIView):
    """
    GET /appointments/follow-up-invitations/
    Returns all follow-up invitations for the authenticated patient.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_patient(request.user):
            return Response({"detail": "Patients only."}, status=status.HTTP_403_FORBIDDEN)
        invitations = (
            FollowUpInvitation.objects
            .select_related(
                "appointment",
                "appointment__doctor",
                "appointment__doctor__doctor_profile",
                "appointment__patient_profile",
            )
            .filter(patient=request.user)
            .order_by("-created_at")
        )
        return Response(FollowUpInvitationSerializer(invitations, many=True, context={"request": request}).data)


class FollowUpInvitationDetailView(APIView):
    """
    GET /appointments/follow-up-invitations/<id>/
    Returns the follow-up invitation detail for patient (or doctor/admin).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            invitation = FollowUpInvitation.objects.select_related(
                "appointment",
                "prescription",
                "patient",
                "appointment__doctor",
                "appointment__doctor__doctor_profile",
                "appointment__patient_profile",
            ).get(pk=pk)
        except FollowUpInvitation.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.user not in (invitation.patient, invitation.appointment.doctor) and not _is_admin(request.user):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        return Response(FollowUpInvitationSerializer(invitation, context={"request": request}).data)


class FollowUpInvitationIgnoreView(APIView):
    """
    POST /appointments/follow-up-invitations/<id>/ignore/
    Marks the invitation as ignored (patient-only).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            invitation = FollowUpInvitation.objects.select_related(
                "appointment",
                "patient",
            ).get(pk=pk)
        except FollowUpInvitation.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if request.user != invitation.patient and not _is_admin(request.user):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)

        if invitation.status != "ignored":
            invitation.status = "ignored"
            invitation.ignored_at = timezone.now()
            invitation.save(update_fields=["status", "ignored_at", "updated_at"])

        return Response(FollowUpInvitationSerializer(invitation, context={"request": request}).data)


# ── PayMongo Webhook for Appointments ────────────────────────────────────────

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import AllowAny

@method_decorator(csrf_exempt, name="dispatch")
class AppointmentPaymongoWebhookView(APIView):
    """
    POST /api/appointments/paymongo/webhook
    Fallback for when the patient closes the tab before the success redirect
    completes. Creates the appointment if it doesn't exist yet and marks
    payment_status=paid.
    Register in PayMongo dashboard alongside the pharmacy webhook.
    """
    permission_classes     = [AllowAny]
    authentication_classes = []

    def post(self, request):
        import hashlib, hmac as _hmac, json as _json
        raw_body  = request.body
        signature = request.headers.get("Paymongo-Signature", "")
        if not _verify_apt_webhook_sig(raw_body, signature):
            logger.warning("Appointment webhook: invalid signature")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = _json.loads(raw_body)
        except _json.JSONDecodeError:
            return Response({"detail": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)

        event_type = event.get("data", {}).get("attributes", {}).get("type", "")
        logger.info("Appointment webhook: %s", event_type)

        if event_type == "checkout_session.payment.paid":
            return self._handle_paid(event)
        return Response({"detail": "Ignored."}, status=status.HTTP_200_OK)

    def _handle_paid(self, event):
        try:
            session_data  = event["data"]["attributes"]["data"]
            session_attrs = session_data["attributes"]
            checkout_id   = session_data["id"]
            metadata      = session_attrs.get("metadata") or {}
            payments      = session_attrs.get("payments") or []
            payment_id    = payments[0]["id"] if payments else checkout_id
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("Appointment webhook (paid): malformed payload — %s", exc)
            return Response({"detail": "Malformed payload."}, status=status.HTTP_400_BAD_REQUEST)

        # If appointment already exists and is paid, skip (idempotent)
        existing = Appointment.objects.filter(paymongo_payment_id=payment_id).first()
        if existing:
            if existing.payment_status == "paid":
                return Response({"detail": "Already processed."}, status=status.HTTP_200_OK)
            existing.payment_status = "paid"
            existing.save(update_fields=["payment_status", "updated_at"])
            logger.info("Appointment webhook: marked existing apt #%s as paid", existing.pk)
            return Response({"detail": "OK"}, status=status.HTTP_200_OK)

        # Appointment not yet created (patient closed tab) — create it now
        doctor_id   = metadata.get("doctorId") or metadata.get("doctor_id")
        patient_id  = metadata.get("patientId") or metadata.get("patient_id")
        consult_type = metadata.get("consultationType", "online")

        if not doctor_id or not patient_id:
            logger.error("Appointment webhook: missing doctorId/patientId in metadata")
            return Response({"detail": "Missing metadata."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            doctor  = User.objects.get(pk=doctor_id)
            patient = User.objects.get(pk=patient_id)
        except User.DoesNotExist as exc:
            logger.error("Appointment webhook: user not found — %s", exc)
            return Response({"detail": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        # Use date/time from metadata if available, fall back to today/now
        apt_date_str = metadata.get("date", "")
        apt_time_str = metadata.get("time", "")
        try:
            from datetime import date as date_type, time as time_type
            apt_date = date_type.fromisoformat(apt_date_str) if apt_date_str else timezone.localdate()
        except ValueError:
            apt_date = timezone.localdate()
        try:
            apt_time = time_type.fromisoformat(apt_time_str) if apt_time_str else timezone.localtime().time()
        except ValueError:
            apt_time = timezone.localtime().time()

        amount_centavos = session_attrs.get("amount", 0)
        fee = round(amount_centavos / 100, 2) if amount_centavos else None

        with transaction.atomic():
            existing_count = (
                Appointment.objects
                .select_for_update()
                .filter(doctor=doctor, date=today)
                .exclude(status__in=["cancelled", "no_show"])
                .count()
            )
            apt = Appointment.objects.create(
                patient=patient,
                doctor=doctor,
                date=apt_date,
                time=apt_time,
                type="online" if consult_type in ("online", "on_demand") else "in_clinic",
                fee=fee,
                queue_number=existing_count + 1,
                payment_status="paid",
                paymongo_payment_id=payment_id,
            )

        logger.info("Appointment webhook: created apt #%s for patient %s (paid via webhook)", apt.pk, patient_id)
        _notify(doctor, "New Appointment (Payment Received)",
                f"{patient.first_name} {patient.last_name} paid for a consultation.",
                data={"appointment_id": apt.pk})
        return Response({"detail": "OK"}, status=status.HTTP_200_OK)


def _verify_apt_webhook_sig(raw_body: bytes, signature_header: str) -> bool:
    import hashlib, hmac as _hmac
    secret = getattr(settings, "PAYMONGO_APPOINTMENT_WEBHOOK_SECRET", "") or getattr(settings, "PAYMONGO_WEBHOOK_SECRET", "")
    if not secret:
        return False
    parts = {}
    for seg in signature_header.split(","):
        if "=" in seg:
            k, v = seg.split("=", 1)
            parts[k.strip()] = v.strip()
    timestamp     = parts.get("t", "")
    received_hmac = parts.get("li") or parts.get("te", "")
    if not timestamp or not received_hmac:
        return False
    message  = f"{timestamp}.{raw_body.decode('utf-8')}"
    expected = _hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, received_hmac)


# ── On-Demand / Instant Consult ───────────────────────────────────────────────

class OnDemandView(APIView):
    """
    GET  /appointments/on-demand/  — list available on-demand doctors sorted by wait time
    POST /appointments/on-demand/  — patient requests instant consult (auto-matches doctor)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        cutoff = timezone.now() - timedelta(minutes=10)
        profiles = (
            DoctorProfile.objects
            .select_related("user")
            .filter(is_on_demand=True, is_verified=True, last_active_at__gte=cutoff, user__is_active=True)
            .prefetch_related("hmos", "services")
        )
        from doctors.serializers import DoctorListSerializer
        return Response(DoctorListSerializer(profiles, many=True, context={"request": request}).data)

    def post(self, request):
        if not _is_patient(request.user):
            return Response({"detail": "Only patients can request on-demand consults."}, status=status.HTTP_403_FORBIDDEN)

        symptoms = request.data.get("symptoms", "").strip()
        if not symptoms:
            return Response({"detail": "symptoms is required."}, status=status.HTTP_400_BAD_REQUEST)

        doctor_id = request.data.get("doctor_id")

        cutoff = timezone.now() - timedelta(minutes=10)
        qs = (
            DoctorProfile.objects
            .select_related("user")
            .filter(is_on_demand=True, is_verified=True, last_active_at__gte=cutoff, user__is_active=True)
        )

        if doctor_id:
            qs = qs.filter(user_id=doctor_id)

        profile = qs.order_by("last_active_at").first()
        if not profile:
            return Response({"detail": "No on-demand doctors available right now."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        doctor = profile.user
        today = timezone.localdate()
        now_time = timezone.localtime().time()
        base_fee = profile.consultation_fee_online or 0
        hmo_provider, hmo_pct, _ = _apply_hmo(request.user, doctor.pk, "on_demand", base_fee)

        with transaction.atomic():
            existing = (
                Appointment.objects
                .select_for_update()
                .filter(doctor=doctor, date=today)
                .exclude(status__in=["cancelled", "no_show"])
            )
            next_queue = existing.count() + 1

            appointment = Appointment.objects.create(
                patient=request.user,
                doctor=doctor,
                date=today,
                time=now_time,
                type="on_demand",
                symptoms=symptoms,
                fee=base_fee,
                is_on_demand=True,
                queue_number=next_queue,
                payment_status="awaiting",
                chat_room_id=uuid.uuid4(),
                hmo_provider=hmo_provider,
                hmo_coverage_percent=hmo_pct,
            )

            room_id = _generate_room_name(appointment.id)
            appointment.video_room_id = room_id
            appointment.video_password = _generate_password()
            appointment.video_link = _jitsi_url(room_id)
            appointment.save(update_fields=["video_room_id", "video_password", "video_link", "updated_at"])

        _notify(
            doctor,
            title="Instant Consult Request",
            message=f"{request.user.first_name} {request.user.last_name} wants an instant consult. Symptoms: {symptoms[:100]}",
            data={"appointment_id": appointment.pk},
        )
        return Response(AppointmentDetailSerializer(appointment).data, status=status.HTTP_201_CREATED)


# ── Reviews ───────────────────────────────────────────────────────────────────

class ReviewView(APIView):
    """
    POST /appointments/reviews/       — patient submits review
    GET  /appointments/reviews/?doctor_id=X — list reviews for a doctor
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        doctor_id = request.query_params.get("doctor_id")
        qs = Review.objects.select_related("patient", "doctor")
        if doctor_id:
            qs = qs.filter(doctor_id=doctor_id)
        elif _is_patient(request.user):
            qs = qs.filter(patient=request.user)
        elif _is_doctor(request.user):
            qs = qs.filter(doctor=request.user)
        paginator = StandardPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(ReviewSerializer(page, many=True).data)

    def post(self, request):
        if not _is_patient(request.user):
            return Response({"detail": "Only patients can submit reviews."}, status=status.HTTP_403_FORBIDDEN)
        serializer = ReviewCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        apt = Appointment.objects.get(pk=data["appointment_id"])
        review = Review.objects.create(
            appointment=apt,
            patient=request.user,
            doctor=apt.doctor,
            rating=data["rating"],
            comment=data.get("comment", ""),
        )
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)


# ── My Doctors ────────────────────────────────────────────────────────────────

class MyDoctorsView(APIView):
    """
    GET /patients/my-doctors/
    Returns distinct doctors the patient has consulted, with last appointment
    date, upcoming count, and aggregated stats.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_patient(request.user):
            return Response({"detail": "Patients only."}, status=status.HTTP_403_FORBIDDEN)

        doctor_ids = (
            Appointment.objects
            .filter(patient=request.user)
            .exclude(status="cancelled")
            .values_list("doctor_id", flat=True)
            .distinct()
        )

        today = timezone.localdate()
        results = []
        for doc_id in doctor_ids:
            try:
                profile = DoctorProfile.objects.select_related("user").get(user_id=doc_id)
            except DoctorProfile.DoesNotExist:
                continue

            apts = Appointment.objects.filter(patient=request.user, doctor_id=doc_id)
            last_apt = apts.exclude(status="cancelled").order_by("-date").first()
            upcoming = apts.filter(date__gte=today).exclude(status__in=["cancelled", "completed", "no_show"]).count()

            from doctors.serializers import DoctorListSerializer
            doc_data = DoctorListSerializer(profile, context={"request": request}).data
            doc_data["last_appointment_date"] = last_apt.date if last_apt else None
            doc_data["upcoming_appointments"] = upcoming
            doc_data["total_consultations"] = apts.filter(status="completed").count()
            results.append(doc_data)

        return Response(results)

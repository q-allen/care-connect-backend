import logging
from datetime import date, timedelta

from django.conf import settings
from django.core.files.base import ContentFile

from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications.models import Notification
from .models import CertificateRequest, LabResult, MedicalCertificate, Prescription
from .serializers import (
    ApproveCertificateSerializer,
    CertificateRequestCreateSerializer,
    CertificateRequestSerializer,
    CreatePrescriptionSerializer,
    LabResultSerializer,
    MedicalCertificateSerializer,
    PrescriptionSerializer,
)
from .utils import generate_certificate_pdf

logger = logging.getLogger(__name__)


def _is_doctor(user):
    return user.role == "doctor"

def _broadcast_document_shared(appointment_id, payload: dict) -> None:
    """Best-effort Channels broadcast for newly shared documents."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"appointment_{appointment_id}",
            payload,
        )
    except Exception as exc:
        logger.warning("Document shared broadcast failed: %s", exc)


def _normalize_medications(medications):
    """
    Strip meta entries and normalize medications into dicts.
    Returns (clean_meds, meta).
    """
    clean_meds = []
    meta = {}
    for entry in medications or []:
        if isinstance(entry, dict) and (entry.get("_meta") or entry.get("meta")):
            meta.update(entry.get("_meta") or entry.get("meta") or {})
            continue
        if isinstance(entry, dict) and not entry.get("name"):
            continue
        if isinstance(entry, str):
            entry = {"name": entry}
        clean_meds.append(entry)
    return clean_meds, meta


def _format_sig_line(med: dict) -> str:
    if med.get("sig"):
        return med.get("sig")
    parts = []
    if med.get("dose"):
        parts.append(str(med.get("dose")))
    if med.get("frequency"):
        parts.append(str(med.get("frequency")))
    if med.get("route"):
        parts.append(str(med.get("route")))
    if med.get("duration"):
        parts.append(f"for {med.get('duration')}")
    return " ".join(part for part in parts if part).strip() or "Take as directed"


def _build_prescription_pdf_bytes(prescription) -> bytes:
    """
    Build prescription PDF bytes using ReportLab.
    Uses a unique style-name prefix to avoid collisions with the global stylesheet registry.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER
    import io

    appointment = prescription.appointment
    patient_profile = None
    if appointment and appointment.patient_profile_id:
        try:
            patient_profile = appointment.patient_profile
        except Exception:
            pass
    doctor_profile = getattr(prescription.doctor, "doctor_profile", None)
    clean_meds, meta = _normalize_medications(prescription.medications)

    patient_name = (
        getattr(appointment, "booked_for_name", "") if appointment else ""
    ) or (
        patient_profile.full_name if patient_profile else ""
    ) or f"{prescription.patient.first_name} {prescription.patient.last_name}".strip()

    patient_age  = str(patient_profile.age) if patient_profile and patient_profile.age is not None else ""
    patient_sex  = patient_profile.sex.capitalize() if patient_profile and patient_profile.sex else ""
    patient_addr = (patient_profile.home_address if patient_profile else "") or "—"
    doctor_name  = f"Dr. {prescription.doctor.first_name} {prescription.doctor.last_name}".strip()
    specialty    = getattr(doctor_profile, "specialty", "") or ""
    clinic_name  = getattr(doctor_profile, "clinic_name", "") or ""
    prc          = getattr(doctor_profile, "prc_license", "") or ""
    ptr          = getattr(doctor_profile, "ptr_license", "") or ""
    apt_date     = appointment.date.strftime("%B %d, %Y") if appointment else ""

    TEAL  = colors.HexColor("#0f766e")
    LIGHT = colors.HexColor("#f0fdfa")
    GRAY  = colors.HexColor("#6b7280")
    BLACK = colors.HexColor("#111827")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=20*mm)
    import uuid as _uuid
    base = getSampleStyleSheet()["Normal"]
    _uid = _uuid.uuid4().hex
    # Unique suffix per call to avoid ReportLab global stylesheet collisions
    def S(name, **kw): return ParagraphStyle(f"rx_{name}_{_uid}", parent=base, **kw)

    sub_s    = S("sub",    fontSize=9,  textColor=GRAY,  spaceAfter=1)
    right_s  = S("right",  fontSize=9,  textColor=GRAY,  alignment=TA_RIGHT)
    label_s  = S("lbl",    fontSize=8,  textColor=GRAY,  fontName="Helvetica-Bold", spaceAfter=1)
    sec_s    = S("sec",    fontSize=8,  textColor=TEAL,  fontName="Helvetica-Bold", spaceAfter=4)
    body_s   = S("body",   fontSize=10, textColor=BLACK)
    footer_s = S("footer", fontSize=8,  textColor=GRAY,  alignment=TA_CENTER)

    story = []
    header_data = [[
        Paragraph(f"<b>CareConnect · E-Prescription</b><br/><font size='14'><b>{doctor_name}</b></font><br/>{specialty + (f' · {clinic_name}' if clinic_name else '')}", sub_s),
        Paragraph(f"Prescription No.<br/><font size='13' color='#0f766e'><b>RX-{prescription.pk:06d}</b></font><br/>Date: {apt_date or prescription.date.strftime('%B %d, %Y')}", right_s),
    ]]
    header_tbl = Table(header_data, colWidths=[110*mm, 55*mm])
    header_tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LINEBELOW",(0,0),(-1,0),1.5,TEAL),("BOTTOMPADDING",(0,0),(-1,0),8)]))
    story += [header_tbl, Spacer(1, 8)]

    story.append(Paragraph("PATIENT INFORMATION", sec_s))
    age_sex = " / ".join(filter(None, [patient_age, patient_sex]))
    pt_tbl = Table([
        [Paragraph(f"<b>Patient Name</b><br/>{patient_name or '—'}", body_s), Paragraph(f"<b>Age / Sex</b><br/>{age_sex or '—'}", body_s)],
        [Paragraph(f"<b>Address</b><br/>{patient_addr}", body_s), Paragraph(f"<b>Consultation Date</b><br/>{apt_date or '—'}", body_s)],
    ], colWidths=[82*mm, 82*mm])
    pt_tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f9fafb")),("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#e5e7eb")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#e5e7eb")),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [pt_tbl, Spacer(1, 10)]

    story.append(Paragraph("℞  PRESCRIPTION", sec_s))
    med_rows = [[Paragraph("#", label_s), Paragraph("Medicine", label_s), Paragraph("Sig / Directions", label_s), Paragraph("Qty", label_s)]]
    for i, med in enumerate(clean_meds, 1):
        name = med.get("name") or "Medication"
        strength = med.get("strength") or med.get("dosage") or ""
        form = med.get("form") or ""
        route = med.get("route") or ""
        generic = med.get("generic") or med.get("generic_name") or ""
        sig = _format_sig_line(med)
        duration = med.get("duration") or ""
        qty = med.get("quantity") or med.get("qty") or "1"
        refills = med.get("refills") or ""
        display = f"{name} {strength}".strip()
        sub_info = " · ".join(filter(None, [form, route, f"Generic: {generic}" if generic else ""]))
        sig_full = sig + (f"\nDuration: {duration}" if duration else "")
        med_html = f"<b>{display}</b>" + (f"<br/><font size='8' color='#6b7280'>{sub_info}</font>" if sub_info else "")
        qty_html = f"<b>{qty}</b>" + (f"<br/><font size='8' color='#6b7280'>Refills: {refills}</font>" if refills else "")
        med_rows.append([Paragraph(str(i), body_s), Paragraph(med_html, body_s), Paragraph(sig_full.replace("\n", "<br/>"), body_s), Paragraph(qty_html, body_s)])
    if not clean_meds:
        med_rows.append([Paragraph("—", body_s), Paragraph("No medications listed.", body_s), "", ""])
    med_tbl = Table(med_rows, colWidths=[8*mm, 72*mm, 68*mm, 17*mm])
    med_tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),LIGHT),("TEXTCOLOR",(0,0),(-1,0),TEAL),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),8),("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#e5e7eb")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#eef2f7")),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [med_tbl, Spacer(1, 10)]

    follow_up = meta.get("follow_up_date") or meta.get("followUpDate") or "—"
    remarks = prescription.instructions or meta.get("remarks") or "—"
    notes_tbl = Table([
        [Paragraph(f"<b>DIAGNOSIS</b><br/>{prescription.diagnosis or '—'}", body_s), Paragraph(f"<b>FOLLOW-UP DATE</b><br/>{follow_up}", body_s)],
        [Paragraph(f"<b>REMARKS / INSTRUCTIONS</b><br/>{remarks}", body_s), Paragraph(f"<b>VALID UNTIL</b><br/>{prescription.valid_until.strftime('%B %d, %Y')}", body_s)],
    ], colWidths=[82*mm, 82*mm])
    notes_tbl.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#e5e7eb")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#e5e7eb")),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [notes_tbl, Spacer(1, 14)]

    creds = " · ".join(filter(None, [f"PRC: {prc}" if prc else "", f"PTR: {ptr}" if ptr else ""]))
    sig_tbl = Table([[Paragraph(f"<b>{doctor_name}</b><br/><font size='8' color='#6b7280'>{creds or 'Digitally signed via CareConnect'}</font>", body_s), Paragraph("Digitally verified prescription<br/>issued via CareConnect", footer_s)]], colWidths=[100*mm, 65*mm])
    sig_tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"BOTTOM"),("ALIGN",(1,0),(1,0),"CENTER")]))
    story += [sig_tbl, Spacer(1, 8), Paragraph("This electronic prescription is valid when verified by the prescribing physician.", footer_s)]

    doc.build(story)
    return buf.getvalue()


def generate_prescription_pdf(prescription, request=None) -> bool:
    """
    Generate prescription PDF using ReportLab and save to pdf_file field.
    """
    try:
        import reportlab  # noqa — ensure installed
    except ImportError:
        logger.warning("ReportLab not installed — skipping PDF generation for Rx #%s", prescription.pk)
        return False
    try:
        pdf_bytes = _build_prescription_pdf_bytes(prescription)
        filename = f"prescription_{prescription.pk}.pdf"
        prescription.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)
        logger.info("PDF generated for prescription #%s → %s", prescription.pk, filename)
        return True
    except Exception as exc:
        logger.error("PDF generation error for prescription #%s: %s", prescription.pk, exc, exc_info=True)
        return False


# ── Prescriptions ─────────────────────────────────────────────────────────────

class PrescriptionListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Prescription.objects.select_related('doctor', 'patient').filter(doctor=request.user) if _is_doctor(request.user) \
            else Prescription.objects.select_related('doctor', 'patient').filter(patient=request.user)
        return Response(PrescriptionSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can issue prescriptions."}, status=status.HTTP_403_FORBIDDEN)
        serializer = CreatePrescriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from users.models import User
        from appointments.models import Appointment

        patient = User.objects.get(pk=data["patient_id"])
        appointment = None
        if data.get("appointment_id"):
            try:
                appointment = Appointment.objects.get(pk=data["appointment_id"])
            except Appointment.DoesNotExist:
                pass

        rx = Prescription.objects.create(
            appointment=appointment,
            patient=patient,
            doctor=request.user,
            diagnosis=data["diagnosis"],
            medications=data["medications"],
            instructions=data.get("instructions", ""),
            valid_until=date.today() + timedelta(days=data["valid_days"]),
        )

        # Generate PDF (best effort)
        try:
            generate_prescription_pdf(rx, request=request)
            rx.refresh_from_db()
        except Exception as exc:
            logger.warning("PDF generation failed for prescription #%s: %s", rx.pk, exc)

        # Attach to appointment share + broadcast if created during a consult
        pdf_url = None
        if rx.pdf_file:
            try:
                pdf_url = request.build_absolute_uri(rx.pdf_file.url)
            except Exception:
                pdf_url = rx.pdf_file.url

        if appointment:
            try:
                from appointments.models import AppointmentShare
                share = AppointmentShare.objects.create(
                    appointment=appointment,
                    doc_type="prescription",
                    document_id=rx.pk,
                    title="Prescription",
                    summary=rx.diagnosis[:120],
                    created_by=request.user,
                )
                _broadcast_document_shared(
                    appointment.pk,
                    {
                        "type": "document.shared",
                        "appointment_id": appointment.pk,
                        "doc_type": "prescription",
                        "document_id": rx.pk,
                        "title": "Prescription",
                        "summary": rx.diagnosis[:120],
                        "created_at": share.created_at.isoformat(),
                        "pdf_url": pdf_url,
                    },
                )
            except Exception as exc:
                logger.warning("AppointmentShare creation failed for Rx #%s: %s", rx.pk, exc)

        Notification.objects.create(
            user=patient, type="prescription", title="New Prescription",
            message=f"Dr. {request.user.first_name} {request.user.last_name} issued you a prescription.",
            data={"prescription_id": rx.pk, "pdf_url": pdf_url},
        )
        return Response(PrescriptionSerializer(rx, context={"request": request}).data, status=status.HTTP_201_CREATED)


class PrescriptionDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            rx = Prescription.objects.get(pk=pk)
        except Prescription.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if request.user not in (rx.patient, rx.doctor) and not request.user.is_staff:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        # If PDF is missing (legacy records), generate on demand
        if not rx.pdf_file:
            try:
                generate_prescription_pdf(rx, request=request)
                rx.refresh_from_db()
            except Exception as exc:
                logger.warning("On-demand PDF generation failed for Rx #%s: %s", rx.pk, exc)
        return Response(PrescriptionSerializer(rx, context={"request": request}).data)


class PrescriptionPdfProxyView(APIView):
    """Regenerate prescription PDF on-the-fly with ReportLab and stream bytes directly.
    This bypasses Cloudinary entirely — no read from cloud storage needed."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            rx = Prescription.objects.get(pk=pk)
        except Prescription.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if request.user not in (rx.patient, rx.doctor) and not request.user.is_staff:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        try:
            from django.http import HttpResponse
            pdf_bytes = _build_prescription_pdf_bytes(rx)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'inline; filename="prescription_{rx.pk}.pdf"'
            response["Content-Length"] = len(pdf_bytes)
            return response
        except Exception as exc:
            logger.error("PDF proxy generation failed for Rx #%s: %s", rx.pk, exc)
            return Response({"detail": "Could not generate PDF."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ── Lab Results ───────────────────────────────────────────────────────────────

class LabResultListView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        qs = LabResult.objects.select_related('doctor', 'patient').filter(doctor=request.user) if _is_doctor(request.user) \
            else LabResult.objects.select_related('doctor', 'patient').filter(patient=request.user)
        return Response(LabResultSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can upload lab results."}, status=status.HTTP_403_FORBIDDEN)
        from users.models import User
        try:
            patient = User.objects.get(pk=request.data.get("patient_id"))
        except User.DoesNotExist:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        from appointments.models import Appointment
        appointment = None
        if request.data.get("appointment_id"):
            try:
                appointment = Appointment.objects.get(pk=request.data["appointment_id"])
            except Appointment.DoesNotExist:
                pass
        import json
        results = request.data.get("results", [])
        if isinstance(results, str):
            results = json.loads(results)
        lab = LabResult.objects.create(
            patient=patient,
            doctor=request.user,
            appointment=appointment,
            test_name=request.data.get("test_name", ""),
            test_type=request.data.get("test_type", ""),
            laboratory=request.data.get("laboratory", ""),
            results=results,
            notes=request.data.get("notes", ""),
            status=request.data.get("status", "pending"),
            file=request.FILES.get("file"),
        )
        Notification.objects.create(
            user=patient, type="lab_result", title="New Lab Request",
            message=f"Dr. {request.user.first_name} {request.user.last_name} sent you a lab request: {lab.test_name}.",
            data={"lab_result_id": lab.pk},
        )
        return Response(LabResultSerializer(lab, context={"request": request}).data, status=status.HTTP_201_CREATED)

class LabResultDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes     = [MultiPartParser, FormParser, JSONParser]

    def _get(self, pk, user):
        try:
            lab = LabResult.objects.get(pk=pk)
        except LabResult.DoesNotExist:
            return None
        if user not in (lab.patient, lab.doctor) and not user.is_staff:
            return None
        return lab

    def get(self, request, pk):
        lab = self._get(pk, request.user)
        if not lab:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(LabResultSerializer(lab, context={"request": request}).data)

    def patch(self, request, pk):
        lab = self._get(pk, request.user)
        if not lab:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can update lab results."}, status=status.HTTP_403_FORBIDDEN)

        if "file" in request.FILES:
            lab.file = request.FILES["file"]
        if "results" in request.data:
            import json
            lab.results = json.loads(request.data["results"])
        if "notes" in request.data:
            lab.notes = request.data["notes"]
        if "status" in request.data:
            lab.status = request.data["status"]
        lab.save()

        if lab.status == "completed":
            Notification.objects.create(
                user=lab.patient, type="lab_result", title="Lab Results Ready",
                message=f"Your {lab.test_name} results are now available.",
                data={"lab_result_id": lab.pk},
            )
        return Response(LabResultSerializer(lab, context={"request": request}).data)


# ── Medical Certificates ──────────────────────────────────────────────────────

class CertificateDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            cert = MedicalCertificate.objects.select_related('doctor', 'patient').get(pk=pk)
        except MedicalCertificate.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if request.user not in (cert.patient, cert.doctor) and not request.user.is_staff:
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        # Generate PDF on-demand if missing
        if not cert.pdf_file:
            try:
                generate_certificate_pdf(cert, request=request)
                cert.refresh_from_db()
            except Exception as exc:
                logger.warning("On-demand cert PDF failed for #%s: %s", cert.pk, exc)
        return Response(MedicalCertificateSerializer(cert, context={"request": request}).data)


class CertificateListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = list(
            MedicalCertificate.objects.select_related('doctor', 'patient').filter(doctor=request.user)
            if _is_doctor(request.user)
            else MedicalCertificate.objects.select_related('doctor', 'patient').filter(patient=request.user)
        )
        for cert in qs:
            if not cert.pdf_file:
                try:
                    generate_certificate_pdf(cert, request=request)
                    cert.refresh_from_db()
                except Exception as exc:
                    logger.warning("On-demand cert PDF failed for #%s: %s", cert.pk, exc)
        return Response(MedicalCertificateSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can issue certificates."}, status=status.HTTP_403_FORBIDDEN)
        from users.models import User
        try:
            patient = User.objects.get(pk=request.data.get("patient_id"), role="patient")
        except User.DoesNotExist:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)

        rest_days = int(request.data.get("rest_days", 0))
        today = date.today()
        cert = MedicalCertificate.objects.create(
            patient=patient, doctor=request.user,
            purpose=request.data.get("purpose", ""),
            diagnosis=request.data.get("diagnosis", ""),
            rest_days=rest_days,
            valid_from=today,
            valid_until=today + timedelta(days=rest_days),
        )
        _generate_and_notify_cert(cert, request)
        return Response(MedicalCertificateSerializer(cert, context={"request": request}).data, status=status.HTTP_201_CREATED)


# ── Certificate Requests ──────────────────────────────────────────────────────

class CertificateRequestListView(APIView):
    """
    GET  /records/certificates/request/  — list requests
    POST /records/certificates/request/  — patient submits request
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if _is_doctor(request.user):
            qs = CertificateRequest.objects.filter(doctor=request.user)
        else:
            qs = CertificateRequest.objects.filter(patient=request.user)
        return Response(CertificateRequestSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if request.user.role != "patient":
            return Response({"detail": "Only patients can request certificates."}, status=status.HTTP_403_FORBIDDEN)
        serializer = CertificateRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from users.models import User
        from appointments.models import Appointment
        doctor = User.objects.get(pk=data["doctor_id"])
        appointment = None
        if data.get("appointment_id"):
            try:
                appointment = Appointment.objects.get(pk=data["appointment_id"])
            except Appointment.DoesNotExist:
                pass

        cert_req = CertificateRequest.objects.create(
            patient=request.user, doctor=doctor,
            appointment=appointment,
            purpose=data["purpose"],
            notes=data.get("notes", ""),
        )
        Notification.objects.create(
            user=doctor, type="system", title="Certificate Request",
            message=f"{request.user.first_name} {request.user.last_name} requested a medical certificate: {data['purpose']}",
            data={"cert_request_id": cert_req.pk},
        )
        return Response(CertificateRequestSerializer(cert_req, context={"request": request}).data, status=status.HTTP_201_CREATED)


class CertificateRequestDetailView(APIView):
    """
    POST /records/certificates/request/<pk>/approve/  — doctor approves + generates PDF
    POST /records/certificates/request/<pk>/reject/   — doctor rejects
    """
    permission_classes = [IsAuthenticated]

    def _get_req(self, pk, user):
        try:
            req = CertificateRequest.objects.select_related("patient", "doctor").get(pk=pk)
        except CertificateRequest.DoesNotExist:
            return None
        if user not in (req.patient, req.doctor) and not user.is_staff:
            return None
        return req

    def post(self, request, pk, action_name):
        req = self._get_req(pk, request.user)
        if not req:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not _is_doctor(request.user):
            return Response({"detail": "Only doctors can approve/reject."}, status=status.HTTP_403_FORBIDDEN)
        if req.status != "pending":
            return Response({"detail": f"Request already {req.status}."}, status=status.HTTP_400_BAD_REQUEST)

        if action_name == "approve":
            serializer = ApproveCertificateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            d = serializer.validated_data
            cert = MedicalCertificate.objects.create(
                patient=req.patient, doctor=request.user,
                purpose=req.purpose,
                diagnosis=d["diagnosis"],
                rest_days=d["rest_days"],
                valid_from=d["valid_from"],
                valid_until=d["valid_until"],
            )
            req.status = "approved"
            req.certificate = cert
            req.save(update_fields=["status", "certificate", "updated_at"])
            _generate_and_notify_cert(cert, request)
            return Response(CertificateRequestSerializer(req, context={"request": request}).data)

        elif action_name == "reject":
            req.status = "rejected"
            req.save(update_fields=["status", "updated_at"])
            Notification.objects.create(
                user=req.patient, type="system", title="Certificate Request Rejected",
                message=f"Dr. {request.user.first_name} {request.user.last_name} rejected your certificate request.",
                data={"cert_request_id": req.pk},
            )
            return Response(CertificateRequestSerializer(req, context={"request": request}).data)

        return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)


def _generate_and_notify_cert(cert, request):
    """Generate PDF for cert and notify patient."""
    try:
        generate_certificate_pdf(cert, request=request)
        cert.refresh_from_db()
    except Exception as exc:
        logger.warning("PDF generation failed for cert #%s: %s", cert.pk, exc)

    pdf_url = None
    if cert.pdf_file:
        try:
            pdf_url = request.build_absolute_uri(cert.pdf_file.url)
        except Exception:
            pass

    Notification.objects.create(
        user=cert.patient, type="system", title="Medical Certificate Ready",
        message=f"Your medical certificate from Dr. {cert.doctor.first_name} {cert.doctor.last_name} is ready.",
        data={"certificate_id": cert.pk, "pdf_url": pdf_url},
    )

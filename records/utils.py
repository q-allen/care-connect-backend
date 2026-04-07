"""
records/utils.py

PDF generation for MedicalCertificate using ReportLab (pure Python, no system deps).
Mirrors the same approach used for Prescription PDFs in views.py.
"""

import logging
from io import BytesIO

from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


def _build_certificate_pdf_bytes(cert) -> bytes:
    """
    Build certificate PDF bytes using ReportLab (no file save).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER

    doctor_profile  = getattr(cert.doctor, "doctor_profile", None)
    appointment     = getattr(cert, "appointment", None)
    patient_profile = getattr(appointment, "patient_profile", None) if appointment else None

    patient_name = (
        getattr(appointment, "booked_for_name", "") if appointment else ""
    ) or (
        patient_profile.full_name if patient_profile else ""
    ) or f"{cert.patient.first_name} {cert.patient.last_name}".strip()

    patient_age  = str(patient_profile.age)         if patient_profile and patient_profile.age  is not None else ""
    patient_sex  = patient_profile.sex.capitalize()  if patient_profile and patient_profile.sex  else ""
    patient_addr = (patient_profile.home_address     if patient_profile else "") or "—"
    doctor_name  = f"Dr. {cert.doctor.first_name} {cert.doctor.last_name}".strip()
    specialty    = getattr(doctor_profile, "specialty",   "") or ""
    clinic_name  = getattr(doctor_profile, "clinic_name", "") or ""
    prc          = getattr(doctor_profile, "prc_license", "") or ""
    ptr          = getattr(doctor_profile, "ptr_license", "") or ""
    apt_date     = appointment.date.strftime("%B %d, %Y") if appointment else cert.date.strftime("%B %d, %Y")

    TEAL  = colors.HexColor("#0f766e")
    LIGHT = colors.HexColor("#f0fdfa")
    GRAY  = colors.HexColor("#6b7280")
    BLACK = colors.HexColor("#111827")

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm, topMargin=18*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()

    import uuid as _uuid
    _uid = _uuid.uuid4().hex
    def S(name, **kw): return ParagraphStyle(f"cert_{name}_{_uid}", parent=styles["Normal"], **kw)

    sub_s    = S("sub",    fontSize=9,  textColor=GRAY,  spaceAfter=1)
    right_s  = S("right",  fontSize=9,  textColor=GRAY,  alignment=TA_RIGHT)
    sec_s    = S("sec",    fontSize=8,  textColor=TEAL,  fontName="Helvetica-Bold", spaceAfter=4)
    body_s   = S("body",   fontSize=10, textColor=BLACK)
    footer_s = S("footer", fontSize=8,  textColor=GRAY,  alignment=TA_CENTER)
    label_s  = S("lbl",    fontSize=8,  textColor=GRAY,  fontName="Helvetica-Bold", spaceAfter=1)

    story = []
    header_tbl = Table([[
        Paragraph(f"<b>CareConnect · Medical Certificate</b><br/><font size='14'><b>{doctor_name}</b></font><br/>{specialty + (f' · {clinic_name}' if clinic_name else '')}", sub_s),
        Paragraph(f"Certificate No.<br/><font size='13' color='#0f766e'><b>CERT-{cert.pk:06d}</b></font><br/>Date: {apt_date}", right_s),
    ]], colWidths=[110*mm, 55*mm])
    header_tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LINEBELOW",(0,0),(-1,0),1.5,TEAL),("BOTTOMPADDING",(0,0),(-1,0),8)]))
    story += [header_tbl, Spacer(1, 8)]

    story.append(Paragraph("PATIENT INFORMATION", sec_s))
    age_sex = " / ".join(filter(None, [patient_age, patient_sex]))
    pt_tbl = Table([
        [Paragraph(f"<b>Patient Name</b><br/>{patient_name or '—'}", body_s), Paragraph(f"<b>Age / Sex</b><br/>{age_sex or '—'}", body_s)],
        [Paragraph(f"<b>Address</b><br/>{patient_addr}", body_s), Paragraph(f"<b>Consultation Date</b><br/>{apt_date}", body_s)],
    ], colWidths=[82*mm, 82*mm])
    pt_tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#f9fafb")),("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#e5e7eb")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#e5e7eb")),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [pt_tbl, Spacer(1, 10)]

    story.append(Paragraph("MEDICAL CERTIFICATE DETAILS", sec_s))
    cert_tbl = Table([
        [Paragraph("<b>PURPOSE</b>",     label_s), Paragraph(cert.purpose    or "—", body_s)],
        [Paragraph("<b>DIAGNOSIS</b>",   label_s), Paragraph(cert.diagnosis  or "—", body_s)],
        [Paragraph("<b>REST DAYS</b>",   label_s), Paragraph(str(cert.rest_days),    body_s)],
        [Paragraph("<b>VALID FROM</b>",  label_s), Paragraph(cert.valid_from.strftime("%B %d, %Y"),  body_s)],
        [Paragraph("<b>VALID UNTIL</b>", label_s), Paragraph(cert.valid_until.strftime("%B %d, %Y"), body_s)],
    ], colWidths=[40*mm, 124*mm])
    cert_tbl.setStyle(TableStyle([("BACKGROUND",(0,0),(0,-1),LIGHT),("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#e5e7eb")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#e5e7eb")),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),("VALIGN",(0,0),(-1,-1),"TOP")]))
    story += [cert_tbl, Spacer(1, 14)]

    creds = " · ".join(filter(None, [f"PRC: {prc}" if prc else "", f"PTR: {ptr}" if ptr else ""]))
    sig_tbl = Table([[Paragraph(f"<b>{doctor_name}</b><br/><font size='8' color='#6b7280'>{creds or 'Digitally signed via CareConnect'}</font>", body_s), Paragraph("Digitally verified certificate<br/>issued via CareConnect", footer_s)]], colWidths=[100*mm, 65*mm])
    sig_tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"BOTTOM"),("ALIGN",(1,0),(1,0),"CENTER")]))
    story += [sig_tbl, Spacer(1, 8), Paragraph("This medical certificate is valid when verified by the issuing physician.", footer_s)]

    doc.build(story)
    return buf.getvalue()


def generate_certificate_pdf(cert, request=None) -> bool:
    """
    Generate a medical certificate PDF using ReportLab and save to cert.pdf_file.
    Returns True on success, False on failure.
    """
    try:
        import reportlab  # noqa
    except ImportError:
        logger.warning("ReportLab not installed — skipping PDF generation for cert #%s", cert.pk)
        return False
    try:
        pdf_bytes = _build_certificate_pdf_bytes(cert)
        filename = f"certificate_{cert.pk}.pdf"
        cert.pdf_file.save(filename, ContentFile(pdf_bytes), save=True)
        logger.info("PDF generated for certificate #%s → %s", cert.pk, filename)
        return True
    except Exception as exc:
        logger.error("PDF generation error for certificate #%s: %s", cert.pk, exc)
        return False

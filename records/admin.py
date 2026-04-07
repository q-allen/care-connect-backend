from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, Http404
from django.utils.html import format_html
from django.urls import path

from .models import CertificateRequest, LabResult, MedicalCertificate, Prescription


@staff_member_required
def admin_prescription_pdf(request, pk):
    try:
        rx = Prescription.objects.get(pk=pk)
    except Prescription.DoesNotExist:
        raise Http404
    from .views import _build_prescription_pdf_bytes
    pdf_bytes = _build_prescription_pdf_bytes(rx)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="prescription_{rx.pk}.pdf"'
    return response


@staff_member_required
def admin_certificate_pdf(request, pk):
    try:
        cert = MedicalCertificate.objects.get(pk=pk)
    except MedicalCertificate.DoesNotExist:
        raise Http404
    from .utils import _build_certificate_pdf_bytes
    pdf_bytes = _build_certificate_pdf_bytes(cert)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="certificate_{cert.pk}.pdf"'
    return response


@admin.register(Prescription)
class PrescriptionAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "doctor", "diagnosis", "date", "valid_until", "pdf_link")
    search_fields = ("patient__email", "doctor__email", "diagnosis")
    ordering      = ("-created_at",)
    readonly_fields = ("pdf_link",)

    def get_urls(self):
        return [
            path("<int:pk>/pdf/", self.admin_site.admin_view(admin_prescription_pdf), name="prescription-pdf"),
        ] + super().get_urls()

    @admin.display(description="PDF")
    def pdf_link(self, obj):
        url = f"/admin/records/prescription/{obj.pk}/pdf/"
        return format_html('<a href="{}" target="_blank">View PDF</a>', url)


@admin.register(LabResult)
class LabResultAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "doctor", "test_name", "status", "date")
    list_filter   = ("status",)
    search_fields = ("patient__email", "test_name")
    ordering      = ("-created_at",)
    actions       = ["mark_completed"]

    @admin.action(description="✅ Mark selected lab results as Completed")
    def mark_completed(self, request, queryset):
        updated = queryset.exclude(status="completed").update(status="completed")
        self.message_user(request, f"{updated} lab result(s) marked as completed.", messages.SUCCESS)


@admin.register(MedicalCertificate)
class MedicalCertificateAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "doctor", "purpose", "date", "valid_until", "pdf_link")
    search_fields = ("patient__email", "doctor__email")
    ordering      = ("-created_at",)
    readonly_fields = ("created_at", "pdf_link")

    def get_urls(self):
        return [
            path("<int:pk>/pdf/", self.admin_site.admin_view(admin_certificate_pdf), name="certificate-pdf"),
        ] + super().get_urls()

    @admin.display(description="PDF")
    def pdf_link(self, obj):
        url = f"/admin/records/medicalcertificate/{obj.pk}/pdf/"
        return format_html('<a href="{}" target="_blank">View PDF</a>', url)


@admin.register(CertificateRequest)
class CertificateRequestAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "doctor", "purpose", "status", "created_at")
    list_filter   = ("status",)
    search_fields = ("patient__email", "doctor__email", "purpose")
    ordering      = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

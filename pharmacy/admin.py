from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.urls import path
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import Medicine, Order, PharmacyPrescriptionUpload


@staff_member_required
def admin_rx_upload_file(request, pk):
    import requests as req
    import cloudinary
    import cloudinary.api
    import cloudinary.utils
    from django.conf import settings as djsettings
    from django.http import HttpResponse
    try:
        upload = PharmacyPrescriptionUpload.objects.get(pk=pk)
    except PharmacyPrescriptionUpload.DoesNotExist:
        raise Http404
    if not upload.file:
        raise Http404

    stored = upload.file.name
    cld = djsettings.CLOUDINARY_STORAGE
    cloudinary.config(
        cloud_name=cld["CLOUD_NAME"],
        api_key=cld["API_KEY"],
        api_secret=cld["API_SECRET"],
    )

    # Resolve public_id and resource_type
    if stored.startswith("http"):
        # Legacy: full URL stored — extract public_id and resource_type from URL
        resource_type = "raw" if "/raw/upload/" in stored else "image"
        after_upload = stored.split("/upload/")[-1]
        # Strip version segment (v1234567/...)
        parts = after_upload.split("/")
        if parts[0].startswith("v") and parts[0][1:].isdigit():
            parts = parts[1:]
        public_id = "/".join(parts)
        # Strip extension for public_id
        if "." in public_id.split("/")[-1]:
            fmt = public_id.rsplit(".", 1)[-1]
            public_id = public_id.rsplit(".", 1)[0]
        else:
            fmt = "pdf"
    else:
        # New: public_id stored — find resource_type via API
        resource_type = None
        fmt = "pdf"
        for rt in ("raw", "image"):
            try:
                info = cloudinary.api.resource(stored, resource_type=rt)
                resource_type = rt
                fmt = info.get("format", "pdf")
                break
            except Exception:
                pass
        if not resource_type:
            raise Http404
        public_id = stored

    try:
        dl_url = cloudinary.utils.private_download_url(
            public_id, fmt, resource_type=resource_type, type="upload", attachment=True
        )
        r = req.get(dl_url, timeout=15)
        r.raise_for_status()
    except Exception:
        raise Http404

    content_type = r.headers.get("Content-Type", "application/octet-stream")
    filename = public_id.split("/")[-1] + "." + fmt
    response = HttpResponse(r.content, content_type=content_type)
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


# ── Medicine Admin ────────────────────────────────────────────────────────────

@admin.register(Medicine)
class MedicineAdmin(admin.ModelAdmin):
    list_display  = ("name", "generic_name", "category", "price_display", "stock_badge", "quantity", "requires_prescription")
    list_filter   = ("category", "in_stock", "requires_prescription")
    search_fields = ("name", "generic_name")
    list_per_page = 25
    fieldsets = (
        (None, {"fields": ("name", "generic_name", "category", "dosage_form", "manufacturer", "pharmacy_partner")}),
        ("Pricing & Stock", {"fields": ("price", "quantity", "in_stock")}),
        ("Prescription", {"fields": ("requires_prescription", "prescription_note")}),
        ("Media", {"fields": ("image", "description")}),
    )

    @admin.display(description="Price (PHP)", ordering="price")
    def price_display(self, obj):
        return format_html('<span style="font-weight:600;color:#0f172a">₱{}</span>', f"{obj.price:,.2f}")

    @admin.display(description="Stock", ordering="in_stock")
    def stock_badge(self, obj):
        if obj.in_stock:
            return format_html('<span class="badge-status badge-in_stock">✓ In Stock</span>')
        return format_html('<span class="badge-status badge-out_stock">✗ Out of Stock</span>')


# ── Prescription Upload Admin ─────────────────────────────────────────────────

@admin.action(description=_("Approve selected prescriptions"))
def approve_prescriptions(modeladmin, request, queryset):
    updated = queryset.update(status="approved")
    messages.success(request, f"{updated} prescription(s) approved.")


@admin.action(description=_("Reject selected prescriptions"))
def reject_prescriptions(modeladmin, request, queryset):
    updated = queryset.update(status="rejected")
    messages.warning(request, f"{updated} prescription(s) rejected.")


@admin.action(description=_("Re-upload selected files to Cloudinary (fix broken URLs)"))
def reupload_to_raw(modeladmin, request, queryset):
    import requests as req
    from django.core.files.base import ContentFile
    ok = 0
    for upload in queryset.exclude(file=""):
        old_url = upload.file.url
        try:
            r = req.get(old_url, timeout=15)
            r.raise_for_status()
        except Exception:
            messages.error(request, f"Upload #{upload.pk}: could not fetch file.")
            continue
        content_type = r.headers.get("Content-Type", "")
        filename = upload.file.name.split("/")[-1]
        if "." not in filename:
            filename += ".pdf" if "pdf" in content_type else ".jpg"
        upload.file.save(filename, ContentFile(r.content), save=True)
        ok += 1
    messages.success(request, f"{ok} file(s) re-uploaded successfully.")


@admin.register(PharmacyPrescriptionUpload)
class PharmacyPrescriptionUploadAdmin(admin.ModelAdmin):
    list_display  = ("id", "patient", "order_ref_link", "status_badge", "file_preview", "created_at")
    list_filter   = ("status",)
    search_fields = ("patient__email", "order__order_ref")
    readonly_fields = ("patient", "order", "file_preview_large", "created_at", "updated_at")
    fields        = ("patient", "order", "file_preview_large", "status", "notes", "created_at", "updated_at")
    actions       = [approve_prescriptions, reject_prescriptions, reupload_to_raw]
    list_per_page = 25

    def get_urls(self):
        return [
            path("<int:pk>/file/", self.admin_site.admin_view(admin_rx_upload_file), name="pharmacy-rx-upload-file"),
        ] + super().get_urls()

    def _proxy_url(self, obj):
        return f"/admin/pharmacy/pharmacyprescriptionupload/{obj.pk}/file/"

    @admin.display(description="Order")
    def order_ref_link(self, obj):
        if obj.order:
            return format_html(
                '<a href="/admin/pharmacy/order/{}/change/">{}</a>',
                obj.order.pk, obj.order.order_ref,
            )
        return "—"

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {"pending": "#f59e0b", "approved": "#10b981", "rejected": "#ef4444"}
        color = colors.get(obj.status, "#6b7280")
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            color, obj.get_status_display(),
        )

    def _is_pdf(self, obj):
        name = obj.file.name.lower()
        return name.endswith(".pdf") or not any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))

    @admin.display(description="File")
    def file_preview(self, obj):
        if not obj.file:
            return "—"
        url = self._proxy_url(obj)
        if self._is_pdf(obj):
            return format_html('<a href="{}" target="_blank">📄 View PDF</a>', url)
        return format_html('<a href="{}" target="_blank"><img src="{}" style="height:40px;border-radius:4px"></a>', url, url)

    @admin.display(description="Prescription File")
    def file_preview_large(self, obj):
        if not obj.file:
            return "No file uploaded."
        url = self._proxy_url(obj)
        if self._is_pdf(obj):
            return format_html(
                '<a href="{}" target="_blank" style="font-size:14px">📄 Open PDF in new tab</a>', url
            )
        return format_html(
            '<a href="{}" target="_blank">'
            '<img src="{}" style="max-width:400px;max-height:300px;border-radius:8px;border:1px solid #e2e8f0">'
            '</a>', url, url,
        )


# ── Order actions ─────────────────────────────────────────────────────────────

@admin.action(description=_("Mark selected orders as Processing"))
def mark_processing(modeladmin, request, queryset):
    queryset.filter(status__in=["pending", "confirmed"]).update(status="processing")


@admin.action(description=_("Mark selected orders as Shipped"))
def mark_shipped(modeladmin, request, queryset):
    import time
    updated = 0
    for order in queryset.exclude(status__in=["delivered", "cancelled"]):
        if not order.tracking_number:
            order.tracking_number = f"TRK-{int(time.time() * 1000) % 1000000000:09d}"
        order.status = "shipped"
        order.save(update_fields=["status", "tracking_number"])
        updated += 1
    messages.success(request, f"{updated} order(s) marked as shipped with tracking numbers.")


@admin.action(description=_("Mark selected orders as Out for Delivery"))
def mark_out_for_delivery(modeladmin, request, queryset):
    queryset.exclude(status__in=["delivered", "cancelled"]).update(
        status="out_for_delivery"
    )


@admin.action(description=_("Mark selected orders as Delivered"))
def mark_delivered(modeladmin, request, queryset):
    queryset.exclude(status="cancelled").update(status="delivered")


# ── Order Admin ───────────────────────────────────────────────────────────────

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display    = (
        "order_ref", "patient", "total_display",
        "status_badge", "tracking_display",
        "payment_badge", "payment_method", "rx_upload_link", "from_prescription", "created_at",
    )
    list_filter     = ("status", "payment_method", "payment_status", "from_prescription")
    search_fields   = ("order_ref", "patient__email", "paymongo_checkout_id", "tracking_number")
    ordering        = ("-created_at",)
    list_per_page   = 25
    readonly_fields = (
        "order_ref", "paymongo_checkout_id",
        "payment_status", "payment_method_type",
        "prescription_image_preview",
        "created_at", "updated_at",
    )
    fieldsets = (
        ("Order Info", {
            "fields": ("patient", "order_ref", "items", "total_amount", "from_prescription"),
        }),
        ("Prescription", {
            "fields": ("prescription", "prescription_image_preview"),
        }),
        ("Delivery", {
            "fields": ("delivery_address", "status", "tracking_number"),
        }),
        ("Payment", {
            "fields": ("payment_method", "paymongo_checkout_id", "payment_status", "payment_method_type"),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    actions = [mark_processing, mark_shipped, mark_out_for_delivery, mark_delivered]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('patient', 'prescription', 'prescription_upload')

    @admin.display(description="Total (PHP)", ordering="total_amount")
    def total_display(self, obj):
        return format_html('<span style="font-weight:600;color:#0f172a">₱{}</span>', f"{obj.total_amount:,.2f}")

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        mapping = {
            "pending":          ("badge-pending",     "⏳ Pending"),
            "confirmed":        ("badge-confirmed",   "✓ Confirmed"),
            "processing":       ("badge-in_progress", "⚙ Processing"),
            "shipped":          ("badge-online",      "🚚 Shipped"),
            "out_for_delivery": ("badge-online",      "🛵 Out for Delivery"),
            "delivered":        ("badge-completed",   "✔ Delivered"),
            "cancelled":        ("badge-cancelled",   "✗ Cancelled"),
        }
        css, label = mapping.get(obj.status, ("badge-inactive", obj.status))
        return format_html('<span class="badge-status {}">{}</span>', css, label)

    @admin.display(description="Payment", ordering="payment_status")
    def payment_badge(self, obj):
        if obj.payment_status == "paid":
            return format_html('<span class="badge-status badge-paid">✓ Paid</span>')
        if obj.payment_status == "pending":
            return format_html('<span class="badge-status badge-pending">⏳ Pending</span>')
        return format_html('<span class="badge-status badge-cancelled">✗ {}</span>', obj.payment_status.title())

    @admin.display(description="Tracking Number", ordering="tracking_number")
    def tracking_display(self, obj):
        return obj.tracking_number if obj.tracking_number else "—"

    @admin.display(description="Rx Upload")
    def rx_upload_link(self, obj):
        try:
            upload = obj.prescription_upload
            return format_html(
                '<a href="/admin/pharmacy/pharmacyprescriptionupload/{}/change/">'
                '<span style="color:{};font-weight:600">{}</span></a>',
                upload.pk,
                {"pending": "#f59e0b", "approved": "#10b981", "rejected": "#ef4444"}.get(upload.status, "#6b7280"),
                upload.get_status_display(),
            )
        except PharmacyPrescriptionUpload.DoesNotExist:
            return "—"

    @admin.display(description="Uploaded Prescription")
    def prescription_image_preview(self, obj):
        try:
            upload = obj.prescription_upload
            if not upload.file:
                return "No file."
            url = f"/admin/pharmacy/pharmacyprescriptionupload/{upload.pk}/file/"
            name = upload.file.name.lower()
            is_pdf = name.endswith(".pdf") or not any(name.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
            if is_pdf:
                return format_html('<a href="{}" target="_blank">📄 Open PDF</a>', url)
            return format_html(
                '<a href="{}" target="_blank">'
                '<img src="{}" style="max-width:350px;max-height:250px;border-radius:6px;border:1px solid #e2e8f0">'
                '</a>', url, url,
            )
        except PharmacyPrescriptionUpload.DoesNotExist:
            return "No prescription uploaded."

import hashlib
import hmac
import json
import logging
import time

import requests
from django.conf import settings
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Medicine, Order, PharmacyPrescriptionUpload
from .serializers import (
    ONLINE_PAYMENT_METHODS, MedicineSerializer, OrderSerializer,
    PlaceOrderSerializer, OrderFromPrescriptionSerializer, AdminOrderStatusSerializer,
    PrescriptionUploadSerializer, PrescriptionUploadWriteSerializer,
)

logger = logging.getLogger(__name__)

PAYMONGO_BASE = "https://api.paymongo.com/v1"


# ── PayMongo API helpers (LIVE MODE) ─────────────────────────────────────────

def _paymongo_auth_header() -> dict:
    """
    Build PayMongo Basic Auth header from secret key.
    PRODUCTION SAFETY: Never logs the secret key.
    """
    import base64
    secret_key = settings.PAYMONGO_SECRET_KEY
    if not secret_key:
        raise ValueError("PAYMONGO_SECRET_KEY not configured")
    token = base64.b64encode(f"{secret_key}:".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json",
    }


def _build_line_items(items: list) -> list:
    """
    Convert order items to PayMongo line_items.
    price is in PHP (decimal) → multiply by 100 for centavos.
    description = name + dosage_form if present.
    """
    line_items = []
    for item in items:
        description = item["name"]
        if item.get("dosage_form"):
            description = f"{item['name']} {item['dosage_form']}"
        elif item.get("generic_name"):
            description = f"{item['name']} ({item['generic_name']})"

        # amount = unit price × quantity in centavos
        unit_centavos = int(round(float(item["price"]) * 100))

        line_items.append({
            "currency":    "PHP",
            "amount":      unit_centavos,
            "name":        item["name"],
            "quantity":    int(item["quantity"]),
            "description": description,
        })
    return line_items


def _create_paymongo_checkout(order: Order, payment_method_type: str) -> dict:
    """
    Create a PayMongo Checkout Session (LIVE MODE) and return the full session data dict.
    Raises requests.HTTPError on a non-2xx response.

    PRODUCTION SAFETY:
      - Never logs customer PII (name, email, phone, card numbers)
      - Handles network errors, API errors, rate limits
      - Returns user-friendly error messages
    """
    frontend_base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000")

    # Build billing info - sanitize phone number
    patient_name = f"{order.patient.first_name} {order.patient.last_name}".strip() or order.patient.email
    patient_phone = getattr(order.patient, "phone", "") or ""
    # Remove non-numeric characters from phone
    patient_phone = "".join(c for c in patient_phone if c.isdigit())

    payload = {
        "data": {
            "attributes": {
                "billing": {
                    "name":  patient_name,
                    "email": order.patient.email,
                    "phone": patient_phone,
                },
                "line_items":           _build_line_items(order.items),
                "payment_method_types": [payment_method_type] if payment_method_type in ("gcash", "card") else ["gcash", "card"],
                "success_url":          f"{frontend_base}/patient/pharmacy/orders/success?ref={order.order_ref}",
                "cancel_url":           f"{frontend_base}/patient/pharmacy/orders/cancel?ref={order.order_ref}",
                "reference_number":     order.order_ref,
                "description":          f"CareConnect Order {order.order_ref}",
                "metadata": {
                    # Stored so the webhook can look up the order without
                    # relying solely on the checkout session id.
                    "order_id":  str(order.id),
                    "order_ref": order.order_ref,
                },
            }
        }
    }

    try:
        resp = requests.post(
            f"{PAYMONGO_BASE}/checkout_sessions",
            headers=_paymongo_auth_header(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("PayMongo checkout created for order %s", order.order_ref)
        return resp.json()["data"]
    except requests.Timeout:
        logger.error("PayMongo checkout timeout for order %s", order.order_ref)
        raise requests.HTTPError("Payment gateway timeout. Please try again.")
    except requests.ConnectionError:
        logger.error("PayMongo checkout connection error for order %s", order.order_ref)
        raise requests.HTTPError("Could not connect to payment gateway. Please check your internet connection.")
    except requests.HTTPError as exc:
        # Parse PayMongo error response for user-friendly messages
        try:
            error_data = exc.response.json()
            errors = error_data.get("errors", [])
            if errors:
                error_detail = errors[0].get("detail", "")
                error_code = errors[0].get("code", "")
                logger.error(
                    "PayMongo checkout failed for order %s [%s]: %s",
                    order.order_ref, error_code, error_detail[:200]
                )
                # Map common errors to user-friendly messages
                if "invalid_amount" in error_code:
                    raise requests.HTTPError("Invalid payment amount. Please contact support.")
                elif "rate_limit" in error_code:
                    raise requests.HTTPError("Too many requests. Please wait a moment and try again.")
                elif "authentication" in error_code:
                    raise requests.HTTPError("Payment gateway authentication failed. Please contact support.")
                else:
                    raise requests.HTTPError(f"Payment gateway error: {error_detail[:100]}")
        except (ValueError, KeyError):
            pass
        logger.error(
            "PayMongo checkout failed for order %s [%s]: %s",
            order.order_ref, exc.response.status_code, exc.response.text[:200]
        )
        raise


# ── Notification helper ───────────────────────────────────────────────────────

def _notify_order_status(order: Order) -> None:
    """Create an in-app notification for the patient on order status change."""
    try:
        from notifications.models import Notification
        messages = {
            "confirmed":        ("Order Confirmed ✓",          f"Your order {order.order_ref} has been confirmed and is being prepared."),
            "processing":       ("Order Being Processed ⚙",    f"Your order {order.order_ref} is now being processed."),
            "shipped":          ("Order Shipped 🚚",            f"Your order {order.order_ref} is on its way!"),
            "out_for_delivery": ("Out for Delivery 🛵",         f"Your order {order.order_ref} is out for delivery. Expect it today!"),
            "delivered":        ("Order Delivered ✔",           f"Your order {order.order_ref} has been delivered. Enjoy!"),
            "cancelled":        ("Order Cancelled",             f"Your order {order.order_ref} has been cancelled."),
        }
        if order.status in messages:
            title, message = messages[order.status]
            Notification.objects.create(
                user=order.patient, type="pharmacy", title=title, message=message,
                data={"order_ref": order.order_ref, "order_id": order.pk, "status": order.status},
            )
            if order.status == "delivered":
                _send_delivery_email(order)
    except Exception as exc:
        logger.warning("Failed to create order notification for %s: %s", order.order_ref, exc)


def _send_delivery_email(order: Order) -> None:
    """Send a delivery confirmation email to the patient."""
    try:
        from django.core.mail import send_mail
        patient = order.patient
        name = f"{patient.first_name} {patient.last_name}".strip() or patient.email
        subject = f"Your CareConnect order {order.order_ref} has been delivered!"
        plain = (
            f"Hi {name},\n\n"
            f"Great news! Your order {order.order_ref} has been delivered.\n"
            f"Total: ₱{float(order.total_amount):,.2f}\n\n"
            "Thank you for choosing CareConnect!"
        )
        html = f"""
        <div style="font-family:Poppins,sans-serif;max-width:480px;margin:auto;padding:32px;border:1px solid #e5e7eb;border-radius:12px;">
          <h2 style="color:#0d9488;margin-bottom:4px;">CareConnect</h2>
          <p style="color:#6b7280;font-size:14px;margin-top:0;">Healthcare, made simple.</p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
          <p style="font-size:15px;color:#111827;">Hi {name},</p>
          <p style="font-size:15px;color:#111827;">Your order has been <strong style="color:#0d9488;">delivered</strong>! 🎉</p>
          <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;margin:16px 0;">
            <p style="margin:0 0 8px;font-size:13px;color:#6b7280;">Order Reference</p>
            <p style="margin:0;font-size:18px;font-weight:700;color:#0d9488;font-family:monospace;">{order.order_ref}</p>
          </div>
          <p style="font-size:14px;color:#374151;">Total paid: <strong>₱{float(order.total_amount):,.2f}</strong></p>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
          <p style="font-size:12px;color:#9ca3af;text-align:center;">Thank you for choosing CareConnect!</p>
        </div>
        """
        send_mail(
            subject=subject,
            message=plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[patient.email],
            html_message=html,
            fail_silently=True,
        )
        logger.info("Delivery confirmation email sent for order %s", order.order_ref)
    except Exception as exc:
        logger.warning("Failed to send delivery email for %s: %s", order.order_ref, exc)


# ── Prescription Upload view ──────────────────────────────────────────────────

class PrescriptionUploadView(APIView):
    """
    POST /api/pharmacy/prescriptions/upload/
    Accepts multipart/form-data with 'file' and optional 'order_id'.
    Returns the created PharmacyPrescriptionUpload record.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = PrescriptionUploadWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        order = None
        if d.get("order_id"):
            try:
                order = Order.objects.get(pk=d["order_id"], patient=request.user)
            except Order.DoesNotExist:
                return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        upload = PharmacyPrescriptionUpload.objects.create(
            patient=request.user,
            file=d["file"],
            order=order,
        )
        return Response(
            PrescriptionUploadSerializer(upload, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

    def get(self, request):
        qs = PharmacyPrescriptionUpload.objects.filter(patient=request.user)
        return Response(PrescriptionUploadSerializer(qs, many=True, context={"request": request}).data)


# ── Medicine views ────────────────────────────────────────────────────────────

class MedicineListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs       = Medicine.objects.filter(in_stock=True)
        query    = request.query_params.get("q")
        category = request.query_params.get("category")
        if query:
            qs = qs.filter(name__icontains=query) | qs.filter(generic_name__icontains=query)
        if category and category != "All":
            qs = qs.filter(category=category)
        return Response(MedicineSerializer(qs, many=True, context={"request": request}).data)

    def post(self, request):
        if not (request.user.is_staff or getattr(request.user, "role", "") == "admin"):
            return Response({"detail": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        serializer = MedicineWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        medicine = serializer.save()
        return Response(MedicineSerializer(medicine, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MedicineDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            med = Medicine.objects.get(pk=pk)
        except Medicine.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(MedicineSerializer(med, context={"request": request}).data)


# ── Order views ───────────────────────────────────────────────────────────────

class OrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Order.objects.filter(patient=request.user).select_related("prescription")
        return Response(OrderSerializer(qs, many=True).data)

    @transaction.atomic
    def post(self, request):
        serializer = PlaceOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ── Resolve optional records.Prescription ────────────────────────────
        prescription = None
        if data.get("prescription_id"):
            from records.models import Prescription
            try:
                prescription = Prescription.objects.get(
                    pk=data["prescription_id"], patient=request.user
                )
            except Prescription.DoesNotExist:
                pass

        order_ref = f"ORD-{int(time.time() * 1000) % 100_000_000:08d}"

        order = Order.objects.create(
            patient          = request.user,
            items            = data["items"],
            total_amount     = data["total_amount"],
            delivery_address = data["delivery_address"],
            payment_method   = data["payment_method"],
            prescription     = prescription,
            order_ref        = order_ref,
        )

        # ── Link uploaded prescription to this order ──────────────────────────
        if data.get("prescription_upload_id"):
            PharmacyPrescriptionUpload.objects.filter(
                pk=data["prescription_upload_id"], patient=request.user, order__isnull=True
            ).update(order=order)

        # ── COD: confirm immediately, notify, return ──────────────────────────
        if order.is_cod:
            order.status = "confirmed"
            order.save(update_fields=["status"])
            _notify_order_status(order)
            return Response(
                OrderSerializer(order).data,
                status=status.HTTP_201_CREATED,
            )

        # ── Online payment: create PayMongo Checkout Session (LIVE MODE) ──────────────────
        try:
            session = _create_paymongo_checkout(order, data["payment_method"])
        except ValueError as exc:
            # Configuration error (missing secret key)
            order.delete()
            logger.error("PayMongo configuration error for order %s: %s", order_ref, exc)
            return Response(
                {"detail": "Payment gateway not configured. Please contact support."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except requests.HTTPError as exc:
            order.delete()
            # Error message is already user-friendly from _create_paymongo_checkout
            error_msg = str(exc) if str(exc) else "Payment gateway error. Please try again."
            logger.error("PayMongo checkout failed for order %s: %s", order_ref, error_msg)
            return Response(
                {"detail": error_msg},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except requests.Timeout:
            order.delete()
            logger.error("PayMongo checkout timeout for order %s", order_ref)
            return Response(
                {"detail": "Payment gateway timeout. Please try again."},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException as exc:
            order.delete()
            logger.error("PayMongo network error for order %s: %s", order_ref, exc)
            return Response(
                {"detail": "Could not reach payment gateway. Please check your internet connection and try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        checkout_url = session["attributes"]["checkout_url"]

        order.paymongo_checkout_id = session["id"]
        order.payment_method_type  = data["payment_method"]
        order.save(update_fields=["paymongo_checkout_id", "payment_method_type"])

        return Response(
            OrderSerializer(order, context={"checkout_url": checkout_url}).data,
            status=status.HTTP_201_CREATED,
        )


class OrderDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            order = Order.objects.get(pk=pk, patient=request.user)
        except Order.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(OrderSerializer(order).data)


class OrderFromPrescriptionView(APIView):
    """
    POST /pharmacy/orders/from-prescription/

    NowServing pattern: one-tap "Order These Medicines" after consultation.
    Reads the prescription's medications list, matches them to Medicine catalogue
    by name (case-insensitive), and pre-fills the cart as a confirmed order.

    Payload:
        {
          "prescription_id": 42,
          "delivery_address": "123 Makati Ave, Bel-Air, Makati City",
          "payment_method": "cod"   // or gcash, card, etc.
        }

    Response: same as OrderSerializer + checkout_url if online payment.
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        serializer = OrderFromPrescriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        from records.models import Prescription
        try:
            prescription = Prescription.objects.get(
                pk=data["prescription_id"], patient=request.user
            )
        except Prescription.DoesNotExist:
            return Response(
                {"detail": "Prescription not found or does not belong to you."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Build order items from prescription.medications
        # medications is a JSONField — each entry may be a string ("Amoxicillin 500mg")
        # or a dict with at least a "name" key.
        items = []
        total = 0
        for med_entry in prescription.medications:
            med_name = med_entry if isinstance(med_entry, str) else med_entry.get("name", "")
            if not med_name:
                continue
            # Try to match against the Medicine catalogue
            medicine_obj = Medicine.objects.filter(
                name__iexact=med_name, in_stock=True
            ).first() or Medicine.objects.filter(
                name__icontains=med_name.split()[0], in_stock=True
            ).first()

            qty = 1
            if isinstance(med_entry, dict):
                try:
                    qty = max(1, int(med_entry.get("quantity", 1)))
                except (TypeError, ValueError):
                    qty = 1

            if medicine_obj:
                unit_price = float(medicine_obj.price)
                items.append({
                    "medicine_id":  medicine_obj.pk,
                    "name":         medicine_obj.name,
                    "generic_name": medicine_obj.generic_name,
                    "dosage_form":  medicine_obj.dosage_form,
                    "quantity":     qty,
                    "price":        unit_price,
                })
                total += unit_price * qty
            else:
                # Medicine not in catalogue — include as unmatched placeholder
                # so the patient can see what was prescribed even if not orderable
                items.append({
                    "medicine_id":  None,
                    "name":         med_name,
                    "generic_name": "",
                    "dosage_form":  "",
                    "quantity":     qty,
                    "price":        0,
                    "not_in_catalogue": True,
                })

        orderable = [i for i in items if not i.get("not_in_catalogue")]
        if not orderable:
            return Response(
                {
                    "detail": "None of the prescribed medicines are currently in stock.",
                    "prescribed_items": items,
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        order_ref = f"RX-{int(time.time() * 1000) % 100_000_000:08d}"
        order = Order.objects.create(
            patient          = request.user,
            items            = orderable,
            total_amount     = round(total, 2),
            delivery_address = data["delivery_address"],
            payment_method   = data["payment_method"],
            prescription     = prescription,
            order_ref        = order_ref,
            from_prescription= True,
        )

        if order.is_cod:
            return Response(
                {**OrderSerializer(order).data, "unmatched_items": [i["name"] for i in items if i.get("not_in_catalogue")]},
                status=status.HTTP_201_CREATED,
            )

        # Online payment — create PayMongo checkout (LIVE MODE)
        try:
            session = _create_paymongo_checkout(order, data["payment_method"])
        except ValueError as exc:
            order.delete()
            logger.error("PayMongo configuration error for order %s: %s", order_ref, exc)
            return Response(
                {"detail": "Payment gateway not configured. Please contact support."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        except requests.HTTPError as exc:
            order.delete()
            error_msg = str(exc) if str(exc) else "Payment gateway error. Please try again."
            return Response(
                {"detail": error_msg},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except requests.Timeout:
            order.delete()
            return Response(
                {"detail": "Payment gateway timeout. Please try again."},
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )
        except requests.RequestException:
            order.delete()
            return Response(
                {"detail": "Could not reach payment gateway. Please check your internet connection."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        checkout_url = session["attributes"]["checkout_url"]
        order.paymongo_checkout_id = session["id"]
        order.payment_method_type  = data["payment_method"]
        order.save(update_fields=["paymongo_checkout_id", "payment_method_type"])

        return Response(
            {
                **OrderSerializer(order, context={"checkout_url": checkout_url}).data,
                "unmatched_items": [i["name"] for i in items if i.get("not_in_catalogue")],
            },
            status=status.HTTP_201_CREATED,
        )


class AdminOrderStatusView(APIView):
    """
    PATCH /pharmacy/orders/<pk>/status/

    Admin-only: update order status, delivery_status, and tracking_number.
    NowServing pattern: admin/pharmacy staff advances the delivery pipeline.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not (request.user.is_staff or getattr(request.user, "role", "") == "admin"):
            return Response({"detail": "Admin only."}, status=status.HTTP_403_FORBIDDEN)
        try:
            order = Order.objects.get(pk=pk)
        except Order.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = AdminOrderStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        update_fields = ["updated_at"]
        if "status" in d:
            order.status = d["status"]
            update_fields.append("status")
        if "tracking_number" in d:
            order.tracking_number = d["tracking_number"]
            update_fields.append("tracking_number")

        order.save(update_fields=list(set(update_fields)))
        _notify_order_status(order)
        return Response(OrderSerializer(order).data)


class CancelOrderView(APIView):
    """
    PATCH /pharmacy/orders/<pk>/cancel/
    Patient can cancel only their own pending order with a pending payment.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            order = Order.objects.get(pk=pk, patient=request.user)
        except Order.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if order.status != "pending":
            return Response(
                {"detail": f"Cannot cancel an order with status '{order.status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order.payment_status != "pending":
            return Response(
                {"detail": f"Cannot cancel — payment is already '{order.payment_status}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        order.status         = "cancelled"
        order.payment_status = "cancelled"
        order.save(update_fields=["status", "payment_status"])
        _notify_order_status(order)
        return Response(OrderSerializer(order).data)


# ── PayMongo Webhook ──────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class PayMongoWebhookView(APIView):
    """
    POST /pharmacy/paymongo/webhook/
    Registered in the PayMongo dashboard.
    Handles: payment.paid, payment.failed, payment.cancelled
    No JWT auth — PayMongo calls this directly.
    """
    permission_classes     = [AllowAny]
    authentication_classes = []

    def post(self, request):
        raw_body  = request.body
        signature = request.headers.get("Paymongo-Signature", "")

        if not _verify_webhook_signature(raw_body, signature):
            logger.warning("PayMongo webhook: invalid or missing signature")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            event = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response({"detail": "Invalid JSON."}, status=status.HTTP_400_BAD_REQUEST)

        event_type = (
            event.get("data", {})
                 .get("attributes", {})
                 .get("type", "")
        )
        logger.info("PayMongo webhook received: %s", event_type)

        # ── Route by event type ───────────────────────────────────────────────
        if event_type == "checkout_session.payment.paid":
            return self._handle_paid(event)
        elif event_type in ("checkout_session.payment.failed", "payment.failed"):
            return self._handle_failed(event)
        elif event_type in ("checkout_session.payment.cancelled", "payment.cancelled"):
            return self._handle_cancelled(event)
        else:
            logger.debug("PayMongo webhook: unhandled event type '%s' — ignoring", event_type)
            return Response({"detail": "Event ignored."}, status=status.HTTP_200_OK)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _handle_paid(self, event: dict) -> Response:
        """
        Handle payment.paid webhook event (LIVE MODE).
        PRODUCTION SAFETY: Never logs customer PII or full payment details.
        """
        try:
            session_data  = event["data"]["attributes"]["data"]
            session_attrs = session_data["attributes"]
            checkout_id   = session_data["id"]
            metadata      = session_attrs.get("metadata") or {}
            order_id      = metadata.get("order_id")
            method_type   = (
                session_attrs.get("payment_method_used") or
                (session_attrs.get("payment_method_types") or [""])[0]
            )
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("PayMongo webhook (paid): malformed payload — %s", exc)
            return Response({"detail": "Malformed payload."}, status=status.HTTP_400_BAD_REQUEST)

        order = self._get_order(order_id, checkout_id)
        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order.pk)

                if order.payment_status == "paid":
                    logger.info("Order %s already marked as paid — idempotent webhook", order.order_ref)
                    return Response({"detail": "Already processed."}, status=status.HTTP_200_OK)

                order.payment_status      = "paid"
                order.status              = "confirmed"
                order.payment_method_type = method_type or order.payment_method_type
                order.save(update_fields=["payment_status", "status", "payment_method_type"])

                _deduct_stock(order.items)

        except Exception as exc:
            logger.exception("PayMongo webhook (paid): DB error for order %s — %s", order.order_ref if order else "unknown", exc)
            return Response({"detail": "Internal error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        _notify_order_status(order)
        logger.info("Order %s confirmed — paid via %s (LIVE)", order.order_ref, method_type)
        return Response({"detail": "OK"}, status=status.HTTP_200_OK)

    def _handle_failed(self, event: dict) -> Response:
        order_id, checkout_id = self._extract_ids(event)
        order = self._get_order(order_id, checkout_id)
        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        if order.payment_status not in ("pending",):
            return Response({"detail": "Already processed."}, status=status.HTTP_200_OK)

        order.payment_status = "failed"
        order.status         = "cancelled"
        order.save(update_fields=["payment_status", "status"])
        _notify_order_status(order)
        logger.info("Order %s payment failed", order.order_ref)
        return Response({"detail": "OK"}, status=status.HTTP_200_OK)

    def _handle_cancelled(self, event: dict) -> Response:
        order_id, checkout_id = self._extract_ids(event)
        order = self._get_order(order_id, checkout_id)
        if order is None:
            return Response({"detail": "Order not found."}, status=status.HTTP_404_NOT_FOUND)

        if order.payment_status not in ("pending",):
            return Response({"detail": "Already processed."}, status=status.HTTP_200_OK)

        order.payment_status = "cancelled"
        order.status         = "cancelled"
        order.save(update_fields=["payment_status", "status"])
        _notify_order_status(order)
        logger.info("Order %s payment cancelled", order.order_ref)
        return Response({"detail": "OK"}, status=status.HTTP_200_OK)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_ids(event: dict):
        """Return (order_id, checkout_id) from any checkout session event."""
        try:
            session_data = event["data"]["attributes"]["data"]
            checkout_id  = session_data["id"]
            metadata     = session_data["attributes"].get("metadata") or {}
            order_id     = metadata.get("order_id")
            return order_id, checkout_id
        except (KeyError, TypeError):
            return None, None

    @staticmethod
    def _get_order(order_id, checkout_id) -> "Order | None":
        """
        Look up order by metadata order_id first; fall back to checkout_id.
        Returns None if not found.
        """
        if order_id:
            try:
                return Order.objects.get(pk=order_id)
            except Order.DoesNotExist:
                pass
        if checkout_id:
            try:
                return Order.objects.get(paymongo_checkout_id=checkout_id)
            except Order.DoesNotExist:
                pass
        logger.error(
            "PayMongo webhook: order not found (order_id=%s, checkout_id=%s)",
            order_id, checkout_id,
        )
        return None


# ── Signature verification ────────────────────────────────────────────────────

def _verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify PayMongo webhook signature (LIVE MODE).
    PayMongo signature header format:
        Paymongo-Signature: t=<unix_ts>,te=<hmac_test>,li=<hmac_live>

    Algorithm:
        message  = f"{timestamp}.{raw_body_as_utf8}"
        expected = HMAC-SHA256(webhook_secret, message).hexdigest()

    We check 'li' (live) first, then 'te' (test).

    PRODUCTION SAFETY:
      - Never logs the webhook secret
      - Rejects webhooks if PAYMONGO_WEBHOOK_SECRET is not configured
      - Uses constant-time comparison to prevent timing attacks
    """
    webhook_secret = getattr(settings, "PAYMONGO_WEBHOOK_SECRET", "")
    if not webhook_secret:
        logger.error("PAYMONGO_WEBHOOK_SECRET not configured — rejecting webhook (SECURITY)")
        return False

    # Parse header parts into a dict
    parts: dict[str, str] = {}
    for segment in signature_header.split(","):
        if "=" in segment:
            k, v = segment.split("=", 1)
            parts[k.strip()] = v.strip()

    timestamp     = parts.get("t", "")
    received_hmac = parts.get("li") or parts.get("te", "")

    if not timestamp or not received_hmac:
        logger.warning("PayMongo webhook: signature header missing t= or hmac value")
        return False

    message  = f"{timestamp}.{raw_body.decode('utf-8')}"
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected, received_hmac)
    if not is_valid:
        logger.warning("PayMongo webhook: signature verification failed (possible tampering or wrong secret)")
    return is_valid


# ── Stock deduction ───────────────────────────────────────────────────────────

def _deduct_stock(items: list) -> None:
    """
    Deduct sold quantities from Medicine.quantity inside the caller's
    atomic transaction. Sets in_stock=False when quantity reaches 0.
    """
    for item in items:
        try:
            med = Medicine.objects.select_for_update().get(pk=item["medicine_id"])
            qty = int(item["quantity"])
            med.quantity = max(0, med.quantity - qty)
            if med.quantity == 0:
                med.in_stock = False
            med.save(update_fields=["quantity", "in_stock"])
        except Medicine.DoesNotExist:
            logger.warning(
                "Stock deduction: Medicine id=%s not found — skipping",
                item.get("medicine_id"),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Stock deduction: bad item data %s — %s", item, exc)


# ── Misc ──────────────────────────────────────────────────────────────────────

def _safe_json(response: requests.Response):
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:500]}

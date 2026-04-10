"""
payouts/admin.py

Rich Django admin for the CareConnect payout system.

Features:
  - Color-coded status badges (pending=yellow, paid=green, rejected=red)
  - Bulk approve / reject actions
  - Per-doctor earnings summary inline
  - Platform revenue summary at the top of the changelist
  - Date-range filters (today / this week / this month)
"""

from decimal import Decimal

from django.contrib import admin, messages
from django.db.models import Sum, Count, Q
from django.utils import timezone
from django.utils.html import format_html

from .models import Payout


# ── Inline: per-doctor earnings summary ──────────────────────────────────────

class PayoutInline(admin.TabularInline):
    """Show a doctor's payout history inline on the User admin page."""
    model = Payout
    extra = 0
    fields = ["amount", "method", "status", "payout_reference", "created_at"]
    readonly_fields = ["amount", "method", "status", "payout_reference", "created_at"]
    can_delete = False
    show_change_link = True
    verbose_name = "Payout Request"
    verbose_name_plural = "Payout History"


# ── Main Payout admin ─────────────────────────────────────────────────────────

@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    """
    Full payout management for CareConnect admins.

    Workflow:
      1. Doctor submits payout request → status=pending
      2. Admin reviews here → bulk approve or reject
      3. On approval: enter payout_reference (GCash/bank ref number)
      4. Status → paid, doctor is notified automatically
    """

    list_display = [
        "id",
        "colored_status",
        "doctor_name_link",
        "amount_display",
        "method_display",
        "account_info",
        "payout_reference",
        "reviewed_by",
        "created_at",
    ]
    list_filter  = ["status", "method", "created_at"]
    search_fields = [
        "doctor__first_name", "doctor__last_name", "doctor__email",
        "payout_reference", "account_number",
    ]
    readonly_fields = [
        "created_at", "updated_at", "reviewed_at",
        "doctor_earnings_summary",
    ]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"
    list_per_page = 25

    fieldsets = [
        ("Payout Request", {
            "fields": [
                "doctor", "doctor_earnings_summary",
                "amount", "method",
                "account_name", "account_number", "bank_name",
                "period_start", "period_end",
            ],
        }),
        ("Status & Review", {
            "fields": [
                "status",
                "payout_reference",
                "rejection_reason",
                "reviewed_by",
                "reviewed_at",
                "admin_notes",
            ],
        }),
        ("Timestamps", {
            "fields": ["created_at", "updated_at"],
            "classes": ["collapse"],
        }),
    ]

    actions = ["action_approve", "action_reject"]

    # ── List display helpers ──────────────────────────────────────────────────

    @admin.display(description="Status", ordering="status")
    def colored_status(self, obj):
        colors = {
            "pending":  ("#f59e0b", "#fffbeb", "⏳ Pending"),
            "approved": ("#10b981", "#ecfdf5", "✓ Approved"),
            "paid":     ("#0d9488", "#f0fdfa", "✅ Paid"),
            "rejected": ("#ef4444", "#fef2f2", "✗ Rejected"),
        }
        color, bg, label = colors.get(obj.status, ("#6b7280", "#f9fafb", obj.status))
        return format_html(
            '<span style="color:{};background:{};padding:3px 10px;border-radius:12px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            color, bg, label,
        )

    @admin.display(description="Doctor", ordering="doctor__last_name")
    def doctor_name_link(self, obj):
        return format_html(
            '<strong>Dr. {} {}</strong><br><small style="color:#6b7280">{}</small>',
            obj.doctor.first_name,
            obj.doctor.last_name,
            obj.doctor.email,
        )

    @admin.display(description="Amount (PHP)", ordering="amount")
    def amount_display(self, obj):
        return format_html(
            '<span style="font-weight:700;color:#0d9488;font-size:15px;">₱{}</span>',
            f"{obj.amount:,.2f}",
        )

    @admin.display(description="Method")
    def method_display(self, obj):
        icons = {"gcash": "📱", "bank_transfer": "🏦", "maya": "💳", "other": "💰"}
        return f"{icons.get(obj.method, '💰')} {obj.get_method_display()}"

    @admin.display(description="Account")
    def account_info(self, obj):
        if obj.account_number:
            return format_html(
                '{}<br><small style="color:#6b7280">{}</small>',
                obj.account_name or "—",
                obj.account_number,
            )
        return obj.account_name or "—"

    # ── Detail page: doctor earnings summary ──────────────────────────────────

    @admin.display(description="Doctor Earnings Summary")
    def doctor_earnings_summary(self, obj):
        """
        Shows the doctor's total earnings, commission deducted, and payout
        history inline on the payout detail page.
        """
        from appointments.models import Appointment

        agg = (
            Appointment.objects
            .filter(
                doctor=obj.doctor,
                status="completed",
                payment_status="paid",
                type__in=("online", "on_demand"),
            )
            .exclude(doctor_earnings=None)
            .aggregate(
                total_gross=Sum("fee"),
                total_commission=Sum("platform_commission"),
                total_earnings=Sum("doctor_earnings"),
                count=Count("id"),
            )
        )

        paid_out = (
            Payout.objects
            .filter(doctor=obj.doctor, status__in=("approved", "paid"))
            .aggregate(total=Sum("amount"))["total"]
        ) or Decimal("0.00")

        pending = (
            Payout.objects
            .filter(doctor=obj.doctor, status="pending")
            .aggregate(total=Sum("amount"))["total"]
        ) or Decimal("0.00")

        total_earnings = agg["total_earnings"] or Decimal("0.00")
        available = max(Decimal("0.00"), total_earnings - paid_out - pending)

        return format_html(
            """
            <table style="border-collapse:collapse;font-size:13px;min-width:320px;">
              <tr style="background:#f0fdfa;">
                <td style="padding:6px 12px;color:#6b7280;">Completed Consults</td>
                <td style="padding:6px 12px;font-weight:600;">{} appointments</td>
              </tr>
              <tr>
                <td style="padding:6px 12px;color:#6b7280;">Gross Fees Collected</td>
                <td style="padding:6px 12px;font-weight:600;">₱{:,.2f}</td>
              </tr>
              <tr style="background:#fef2f2;">
                <td style="padding:6px 12px;color:#ef4444;">Platform Commission (15%)</td>
                <td style="padding:6px 12px;font-weight:600;color:#ef4444;">−₱{:,.2f}</td>
              </tr>
              <tr style="background:#f0fdfa;">
                <td style="padding:6px 12px;color:#0d9488;font-weight:600;">Doctor Net Earnings</td>
                <td style="padding:6px 12px;font-weight:700;color:#0d9488;">₱{:,.2f}</td>
              </tr>
              <tr>
                <td style="padding:6px 12px;color:#6b7280;">Already Paid Out</td>
                <td style="padding:6px 12px;">₱{:,.2f}</td>
              </tr>
              <tr>
                <td style="padding:6px 12px;color:#f59e0b;">Pending Payout</td>
                <td style="padding:6px 12px;color:#f59e0b;">₱{:,.2f}</td>
              </tr>
              <tr style="background:#ecfdf5;border-top:2px solid #0d9488;">
                <td style="padding:8px 12px;font-weight:700;color:#0d9488;">Available for Payout</td>
                <td style="padding:8px 12px;font-weight:700;font-size:15px;color:#0d9488;">₱{:,.2f}</td>
              </tr>
            </table>
            """,
            agg["count"] or 0,
            agg["total_gross"] or Decimal("0.00"),
            agg["total_commission"] or Decimal("0.00"),
            total_earnings,
            paid_out,
            pending,
            available,
        )

    # ── Bulk actions ──────────────────────────────────────────────────────────

    @admin.action(description="✅ Approve selected pending payouts")
    def action_approve(self, request, queryset):
        pending = queryset.filter(status="pending")
        count = pending.update(
            status="paid",
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )
        # Send notifications (best-effort)
        for payout in pending:
            try:
                from notifications.models import Notification
                Notification.objects.create(
                    user=payout.doctor,
                    type="payout",
                    title="Payout Approved ✅",
                    message=(
                        f"Your payout of ₱{payout.amount:,.2f} has been approved. "
                        f"Funds will be transferred via {payout.get_method_display()}."
                    ),
                    data={"payout_id": payout.pk},
                )
            except Exception:
                pass
        self.message_user(request, f"{count} payout(s) approved.", messages.SUCCESS)

    @admin.action(description="✗ Reject selected pending payouts")
    def action_reject(self, request, queryset):
        pending = queryset.filter(status="pending")
        count = pending.update(
            status="rejected",
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
            rejection_reason="Rejected via bulk admin action. Please contact admin for details.",
        )
        for payout in pending:
            try:
                from notifications.models import Notification
                Notification.objects.create(
                    user=payout.doctor,
                    type="payout",
                    title="Payout Request Rejected",
                    message=(
                        f"Your payout request of ₱{payout.amount:,.2f} was rejected. "
                        f"Your earnings remain available. Please contact admin for details."
                    ),
                    data={"payout_id": payout.pk},
                )
            except Exception:
                pass
        self.message_user(request, f"{count} payout(s) rejected.", messages.WARNING)

    # ── Changelist: inject platform revenue summary at top ────────────────────

    def changelist_view(self, request, extra_context=None):
        """Inject platform revenue summary into the changelist page."""
        from appointments.models import Appointment

        extra_context = extra_context or {}

        # All-time platform revenue
        agg = (
            Appointment.objects
            .filter(
                status="completed",
                payment_status="paid",
                type__in=("online", "on_demand"),
            )
            .exclude(platform_commission=None)
            .aggregate(
                total_revenue=Sum("platform_commission"),
                total_gross=Sum("fee"),
                total_count=Count("id"),
            )
        )

        # This month
        today = timezone.localdate()
        month_start = today.replace(day=1)
        month_agg = (
            Appointment.objects
            .filter(
                status="completed",
                payment_status="paid",
                type__in=("online", "on_demand"),
                date__gte=month_start,
            )
            .exclude(platform_commission=None)
            .aggregate(
                revenue=Sum("platform_commission"),
                count=Count("id"),
            )
        )

        # Payout totals
        payout_agg = Payout.objects.aggregate(
            total_paid=Sum("amount", filter=Q(status__in=("approved", "paid"))),
            total_pending=Sum("amount", filter=Q(status="pending")),
            count_pending=Count("id", filter=Q(status="pending")),
        )

        extra_context["revenue_summary"] = {
            "total_revenue":   agg["total_revenue"]   or Decimal("0.00"),
            "total_gross":     agg["total_gross"]      or Decimal("0.00"),
            "total_count":     agg["total_count"]      or 0,
            "month_revenue":   month_agg["revenue"]    or Decimal("0.00"),
            "month_count":     month_agg["count"]      or 0,
            "total_paid_out":  payout_agg["total_paid"]    or Decimal("0.00"),
            "total_pending":   payout_agg["total_pending"] or Decimal("0.00"),
            "count_pending":   payout_agg["count_pending"] or 0,
        }

        return super().changelist_view(request, extra_context=extra_context)

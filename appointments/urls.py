from django.urls import path
from .views import (
    AppointmentViewSet,
    AppointmentPaymongoWebhookView,
    FollowUpInvitationDetailView,
    FollowUpInvitationIgnoreView,
    FollowUpInvitationListView,
    OnDemandView,
    ReviewView,
)

appt = AppointmentViewSet.as_view

urlpatterns = [
    # Collection
    path("",                              appt({"get": "list",    "post": "create"}),          name="appointment-list"),
    path("upcoming/",                     appt({"get": "upcoming"}),                            name="appointment-upcoming"),
    path("queue/today/",                  appt({"get": "queue_today"}),                         name="appointment-queue-today"),
    path("slots/<int:doctor_id>/",        appt({"get": "available_slots"}),                     name="appointment-slots"),
    path("on-demand/",                    OnDemandView.as_view(),                               name="appointment-on-demand"),
    path("reviews/",                      ReviewView.as_view(),                                 name="appointment-reviews"),
    path("follow-up-invitations/",                FollowUpInvitationListView.as_view(),          name="follow-up-invitation-list"),
    path("follow-up-invitations/<int:pk>/",       FollowUpInvitationDetailView.as_view(),        name="follow-up-invitation-detail"),
    path("follow-up-invitations/<int:pk>/ignore/", FollowUpInvitationIgnoreView.as_view(),       name="follow-up-invitation-ignore"),
    path("paymongo/webhook",              AppointmentPaymongoWebhookView.as_view(),              name="appointment-paymongo-webhook"),

    # Instance
    path("<int:pk>/",                     appt({"get": "retrieve", "patch": "partial_update"}), name="appointment-detail"),
    path("<int:pk>/accept/",              appt({"post": "accept"}),                             name="appointment-accept"),
    path("<int:pk>/confirm_payment/",      appt({"post": "confirm_payment"}),                    name="appointment-confirm-payment"),
    path("<int:pk>/reject/",              appt({"post": "reject"}),                             name="appointment-reject"),
    path("<int:pk>/start_consult/",       appt({"post": "start_consult"}),                      name="appointment-start-consult"),
    path("<int:pk>/start_video/",          appt({"post": "start_video_consultation"}),            name="appointment-start-video"),
    path("<int:pk>/share_document/",      appt({"post": "share_document"}),                     name="appointment-share-document"),
    path("<int:pk>/call_next/",           appt({"post": "call_next"}),                          name="appointment-call-next"),
    path("<int:pk>/complete/",            appt({"post": "complete"}),                           name="appointment-complete"),
    path("<int:pk>/cancel/",              appt({"post": "cancel"}),                             name="appointment-cancel"),
    path("<int:pk>/refund/",              appt({"post": "refund"}),                             name="appointment-refund"),
    path("<int:pk>/no_show/",             appt({"post": "no_show"}),                            name="appointment-no-show"),
    # Review actions (NowServing pattern)
    path("<int:pk>/review/",              appt({"post": "submit_review"}),                      name="appointment-review"),
    path("<int:pk>/review/reply/",        appt({"patch": "reply_to_review"}),                   name="appointment-review-reply"),
]

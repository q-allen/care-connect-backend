from django.urls import path
from .views import (
    CertificateDetailView,
    CertificateListView,
    CertificateRequestDetailView,
    CertificateRequestListView,
    CertificatePdfProxyView,
    LabResultDetailView,
    LabResultListView,
    PrescriptionDetailView,
    PrescriptionListView,
    PrescriptionPdfProxyView,
)

urlpatterns = [
    path("prescriptions",                          PrescriptionListView.as_view(),      name="prescription-list"),
    path("prescriptions/<int:pk>",                 PrescriptionDetailView.as_view(),    name="prescription-detail"),
    path("prescriptions/<int:pk>/pdf/",            PrescriptionPdfProxyView.as_view(),  name="prescription-pdf-proxy"),
    path("labs",                                   LabResultListView.as_view(),         name="lab-list"),
    path("labs/<int:pk>",                          LabResultDetailView.as_view(),       name="lab-detail"),
    path("certificates",                           CertificateListView.as_view(),       name="certificate-list"),
    path("certificates/<int:pk>",                  CertificateDetailView.as_view(),     name="certificate-detail"),
    path("certificates/<int:pk>/pdf/",             CertificatePdfProxyView.as_view(),   name="certificate-pdf-proxy"),
    path("certificates/request",                   CertificateRequestListView.as_view(),    name="cert-request-list"),
    path("certificates/request/<int:pk>/approve",  CertificateRequestDetailView.as_view(), {"action_name": "approve"}, name="cert-request-approve"),
    path("certificates/request/<int:pk>/reject",   CertificateRequestDetailView.as_view(), {"action_name": "reject"},  name="cert-request-reject"),
]

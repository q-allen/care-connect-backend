from rest_framework import serializers
from .models import CertificateRequest, LabResult, MedicalCertificate, Prescription


class PrescriptionSerializer(serializers.ModelSerializer):
    doctor_name = serializers.SerializerMethodField()
    pdf_url     = serializers.SerializerMethodField()

    class Meta:
        model  = Prescription
        fields = ["id", "appointment", "patient", "doctor", "doctor_name",
                  "date", "diagnosis", "medications", "instructions", "valid_until",
                  "is_digital", "pdf_url", "created_at"]
        read_only_fields = ["id", "date", "created_at", "pdf_url"]

    def get_doctor_name(self, obj):
        try:
            name = f"{obj.doctor.first_name} {obj.doctor.last_name}".strip()
            return f"Dr. {name}" if name else "Physician"
        except Exception:
            return "Physician"

    def get_pdf_url(self, obj):
        request = self.context.get("request")
        if not obj.pk:
            return None
        path = f"/api/records/prescriptions/{obj.pk}/pdf/"
        return request.build_absolute_uri(path) if request else path


class CreatePrescriptionSerializer(serializers.Serializer):
    appointment_id = serializers.IntegerField(required=False, allow_null=True)
    patient_id     = serializers.IntegerField()
    diagnosis      = serializers.CharField()
    medications    = serializers.ListField(child=serializers.DictField())
    instructions   = serializers.CharField(required=False, allow_blank=True, default="")
    valid_days     = serializers.IntegerField(default=30, min_value=1)

    def validate_patient_id(self, value):
        from users.models import User
        if not User.objects.filter(pk=value, role="patient").exists():
            raise serializers.ValidationError("Patient not found.")
        return value


class LabResultSerializer(serializers.ModelSerializer):
    doctor_name = serializers.SerializerMethodField()
    file_url    = serializers.SerializerMethodField()

    class Meta:
        model  = LabResult
        fields = ["id", "patient", "doctor", "doctor_name", "appointment",
                  "test_name", "test_type", "date", "status", "results",
                  "notes", "file_url", "laboratory", "created_at"]
        read_only_fields = ["id", "date", "created_at"]

    def get_doctor_name(self, obj):
        try:
            name = f"{obj.doctor.first_name} {obj.doctor.last_name}".strip()
            return f"Dr. {name}" if name else "Physician"
        except Exception:
            return "Physician"

    def get_file_url(self, obj):
        request = self.context.get("request")
        if obj.file and request:
            return request.build_absolute_uri(obj.file.url)
        return None


class MedicalCertificateSerializer(serializers.ModelSerializer):
    doctor_name = serializers.SerializerMethodField()
    pdf_url     = serializers.SerializerMethodField()

    class Meta:
        model  = MedicalCertificate
        fields = ["id", "patient", "doctor", "doctor_name", "date",
                  "purpose", "diagnosis", "rest_days", "valid_from", "valid_until",
                  "pdf_url", "created_at"]
        read_only_fields = ["id", "date", "created_at", "pdf_url"]

    def get_doctor_name(self, obj):
        try:
            name = f"{obj.doctor.first_name} {obj.doctor.last_name}".strip()
            return f"Dr. {name}" if name else "Physician"
        except Exception:
            return "Physician"

    def get_pdf_url(self, obj):
        request = self.context.get("request")
        if not obj.pk:
            return None
        path = f"/api/records/certificates/{obj.pk}/pdf/"
        return request.build_absolute_uri(path) if request else path


class CertificateRequestSerializer(serializers.ModelSerializer):
    patient_name = serializers.SerializerMethodField()
    doctor_name  = serializers.SerializerMethodField()
    certificate  = MedicalCertificateSerializer(read_only=True)

    class Meta:
        model  = CertificateRequest
        fields = ["id", "patient", "patient_name", "doctor", "doctor_name",
                  "appointment", "purpose", "notes", "status", "certificate",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "patient", "status", "certificate", "created_at", "updated_at"]

    def get_patient_name(self, obj):
        try:
            return f"{obj.patient.first_name} {obj.patient.last_name}".strip() or "Patient"
        except Exception:
            return "Patient"

    def get_doctor_name(self, obj):
        try:
            name = f"{obj.doctor.first_name} {obj.doctor.last_name}".strip()
            return f"Dr. {name}" if name else "Physician"
        except Exception:
            return "Physician"


class CertificateRequestCreateSerializer(serializers.Serializer):
    doctor_id      = serializers.IntegerField()
    purpose        = serializers.CharField(max_length=300)
    notes          = serializers.CharField(required=False, allow_blank=True, default="")
    appointment_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_doctor_id(self, value):
        from users.models import User
        if not User.objects.filter(pk=value, role="doctor", is_active=True).exists():
            raise serializers.ValidationError("Doctor not found.")
        return value


class ApproveCertificateSerializer(serializers.Serializer):
    diagnosis  = serializers.CharField()
    rest_days  = serializers.IntegerField(default=0, min_value=0)
    valid_from = serializers.DateField()
    valid_until = serializers.DateField()

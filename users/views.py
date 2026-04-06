import threading

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
import secrets

from .email import send_otp_email
from .models import FamilyMember, User
from .serializers import (
    ForgotPasswordSerializer,
    LoginSerializer,
    ProfileCompletionSerializer,
    RegisterSerializer,
    ResetPasswordSerializer,
    SendOtpSerializer,
    UserSerializer,
)
from .authentication import OptionalCookieJWTAuthentication

OTP_TTL = 60 * 10        # 10 minutes
OTP_RATE_TTL = 60        # 1 minute cooldown between OTP requests

COOKIE_SAMESITE = "None" if not settings.DEBUG else "Lax"
COOKIE_SECURE = not settings.DEBUG


def _set_auth_cookies(response, refresh):
    access = refresh.access_token
    access_lifetime = settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"]
    refresh_lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]

    response.set_cookie(
        "access_token",
        str(access),
        max_age=int(access_lifetime.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )
    response.set_cookie(
        "refresh_token",
        str(refresh),
        max_age=int(refresh_lifetime.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
    )


class SendOtpView(APIView):
    """
    POST /api/auth/send-otp/
    Send a 6-digit OTP to the user's email for verification.
    NowServing pattern: clear, helpful error messages with rate limiting.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SendOtpSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        rate_key = f"otp_rate:{email}"
        if cache.get(rate_key):
            return Response(
                {
                    "detail": "Please wait a moment before requesting another code. You can request a new code in 60 seconds."
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = str(secrets.randbelow(900000) + 100000)
        cache.set(f"otp:{email}", otp, timeout=OTP_TTL)
        cache.set(rate_key, 1, timeout=OTP_RATE_TTL)

        threading.Thread(
            target=send_otp_email,
            args=(email, otp, "verify your email"),
            daemon=True,
        ).start()
        
        return Response(
            {
                "detail": "Verification code sent! Please check your email.",
                "expires_in": OTP_TTL  # 10 minutes
            },
            status=status.HTTP_200_OK
        )


class RegisterView(APIView):
    """
    POST /api/auth/register/
    Create a new patient account with OTP verification.
    NowServing pattern: smooth registration with helpful error messages.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        data = {
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "middle_name": user.middle_name,
                "last_name": user.last_name,
                "role": user.role,
            },
            "message": "Account created successfully! Welcome to CareConnect."
        }
        response = Response(data, status=status.HTTP_201_CREATED)
        _set_auth_cookies(response, refresh)
        return response


class LoginView(APIView):
    """
    POST /api/auth/login/
    Authenticate user and return JWT tokens.
    NowServing pattern: clear error messages for failed login attempts.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        refresh = RefreshToken.for_user(user)
        data = {
            "user": {
                "id": user.id,
                "email": user.email,
                "first_name": user.first_name,
                "middle_name": user.middle_name,
                "last_name": user.last_name,
                "role": user.role,
            },
            "message": f"Welcome back, {user.first_name}!"
        }
        response = Response(data, status=status.HTTP_200_OK)
        _set_auth_cookies(response, refresh)
        return response


class MeView(APIView):
    authentication_classes = [OptionalCookieJWTAuthentication]
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        user = request.user
        if not user or not user.is_authenticated:
            return Response({"user": None}, status=status.HTTP_200_OK)

        doctor_profile_complete = None
        try:
            doctor_profile_complete = user.doctor_profile.is_profile_complete
        except Exception:
            pass

        # UserSerializer returns full profile + nested family_members in one call.
        # NowServing pattern: single GET /api/auth/me/ hydrates the entire patient store.
        data = UserSerializer(user).data
        data["doctor_profile_complete"] = doctor_profile_complete
        # For doctors, expose profile_photo as avatar so the frontend user.avatar works.
        if user.role == "doctor":
            try:
                photo = user.doctor_profile.profile_photo
                data["avatar"] = photo.url if photo else None
            except Exception:
                pass
        return Response({"user": data}, status=status.HTTP_200_OK)

    def patch(self, request):
        """
        PATCH /api/auth/me/
        General profile update — same partial-update logic as CompleteProfileView
        but without the is_profile_complete gate check.
        Used by the main profile page tabs (Personal, Health Info).
        """
        if not request.user or not request.user.is_authenticated:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = ProfileCompletionSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        data = UserSerializer(user).data
        return Response({"user": data}, status=status.HTTP_200_OK)


class RefreshView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.COOKIES.get("refresh_token")
        if not token:
            return Response({"detail": "Refresh token not found."}, status=status.HTTP_401_UNAUTHORIZED)
        try:
            refresh = RefreshToken(token)
        except TokenError as e:
            return Response({"detail": str(e)}, status=status.HTTP_401_UNAUTHORIZED)
        response = Response({"detail": "Token refreshed."}, status=status.HTTP_200_OK)
        _set_auth_cookies(response, refresh)
        return response


class ForgotPasswordView(APIView):
    """
    POST /api/auth/forgot-password/
    Send OTP for password reset.
    NowServing pattern: helpful messages for password recovery.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        rate_key = f"otp_rate:{email}"
        if cache.get(rate_key):
            return Response(
                {
                    "detail": "Please wait a moment before requesting another code. You can request a new code in 60 seconds."
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        otp = str(secrets.randbelow(900000) + 100000)
        cache.set(f"otp:{email}", otp, timeout=OTP_TTL)
        cache.set(rate_key, 1, timeout=OTP_RATE_TTL)

        threading.Thread(
            target=send_otp_email,
            args=(email, otp, "reset your password"),
            daemon=True,
        ).start()
        
        return Response(
            {
                "detail": "Password reset code sent! Please check your email.",
                "expires_in": OTP_TTL
            },
            status=status.HTTP_200_OK
        )


class ResetPasswordView(APIView):
    """
    POST /api/auth/reset-password/
    Reset password with OTP verification.
    NowServing pattern: clear success message after password reset.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(
            {
                "detail": "Password reset successful! You can now sign in with your new password."
            },
            status=status.HTTP_200_OK
        )


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.COOKIES.get("refresh_token")
        if token:
            try:
                RefreshToken(token).blacklist()
            except TokenError:
                pass
        response = Response({"detail": "Logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")
        return response


# ── FamilyMember CRUD ───────────────────────────────────────────────────────────────────────────────


class CompleteProfileView(APIView):
    """
    PATCH /api/auth/me/complete/

    Patient onboarding wizard endpoint.
    NowServing.ph pattern: after registration patients are redirected to a
    multi-step wizard. Each step PATCHes this endpoint with partial data.
    The final step (or "Skip for now") can send is_profile_complete=True.
    Booking is never blocked — this endpoint accepts partial/optional fields.

    Example payloads:
      Step 1: {"first_name": "Maria", "last_name": "Santos",
               "phone": "+639171234567", "birthdate": "1990-05-15", "gender": "female"}
      Step 2: {"blood_type": "O+", "allergies": ["Penicillin", "Sulfa"]}
      Step 3: {"is_profile_complete": true}  (optional skip/finalize)
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        serializer = ProfileCompletionSerializer(
            request.user, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        # Return the full user shape so the frontend store can sync in one call.
        data = UserSerializer(user).data
        return Response(data, status=status.HTTP_200_OK)


class AvatarUploadView(APIView):
    """
    POST /api/auth/me/avatar/
    Accepts multipart/form-data with field `avatar` (image file).
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file = request.FILES.get("avatar")
        if not file:
            return Response({"detail": "No file provided."}, status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        if user.avatar:
            try:
                user.avatar.delete(save=False)
            except Exception:
                pass
        user.avatar = file
        user.save(update_fields=["avatar"])
        url = user.avatar.name
        if not url.startswith(("http://", "https://")):
            url = user.avatar.url
        return Response({"avatar": url}, status=status.HTTP_200_OK)


class FamilyMemberView(APIView):
    """
    GET  /api/patients/family-members/        — list the logged-in patient's family members
    POST /api/patients/family-members/        — add a new family member

    NowServing.ph pattern: one account books for the whole family.
    The patient FK on Appointment always points to the booker;
    these records only carry the consultee's personal details.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        members = FamilyMember.objects.filter(patient=request.user)
        data = [
            {
                "id":           m.pk,
                "name":         m.name,
                "age":          m.age,
                "gender":       m.gender,
                "relationship": m.relationship,
                "birthdate":    str(m.birthdate) if m.birthdate else None,
            }
            for m in members
        ]
        return Response(data, status=status.HTTP_200_OK)

    def post(self, request):
        d = request.data
        name         = str(d.get("name", "")).strip()
        age          = d.get("age")
        gender       = str(d.get("gender", "")).strip()
        relationship = str(d.get("relationship", "other")).strip()
        birthdate    = d.get("birthdate") or None

        errors = {}
        if not name:
            errors["name"] = "Name is required."
        if age is None:
            errors["age"] = "Age is required."
        else:
            try:
                age = int(age)
                if not (0 <= age <= 150):
                    errors["age"] = "Age must be between 0 and 150."
            except (TypeError, ValueError):
                errors["age"] = "Age must be a number."
        if gender not in ("male", "female", "other"):
            errors["gender"] = "Gender must be male, female, or other."
        if relationship not in ("spouse", "child", "parent", "sibling", "other"):
            errors["relationship"] = "Invalid relationship."
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        member = FamilyMember.objects.create(
            patient=request.user,
            name=name,
            age=age,
            gender=gender,
            relationship=relationship,
            birthdate=birthdate,
        )
        return Response(
            {
                "id":           member.pk,
                "name":         member.name,
                "age":          member.age,
                "gender":       member.gender,
                "relationship": member.relationship,
                "birthdate":    str(member.birthdate) if member.birthdate else None,
            },
            status=status.HTTP_201_CREATED,
        )


class FamilyMemberDetailView(APIView):
    """
    PATCH  /api/patients/family-members/{id}/  — update a family member
    DELETE /api/patients/family-members/{id}/  — remove a family member
    """
    permission_classes = [IsAuthenticated]

    def _get_member(self, pk, user):
        try:
            return FamilyMember.objects.get(pk=pk, patient=user)
        except FamilyMember.DoesNotExist:
            return None

    def patch(self, request, pk):
        member = self._get_member(pk, request.user)
        if not member:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        d = request.data
        if "name" in d:
            member.name = str(d["name"]).strip() or member.name
        if "age" in d:
            try:
                member.age = int(d["age"])
            except (TypeError, ValueError):
                return Response({"age": "Age must be a number."}, status=status.HTTP_400_BAD_REQUEST)
        if "gender" in d and d["gender"] in ("male", "female", "other"):
            member.gender = d["gender"]
        if "relationship" in d and d["relationship"] in ("spouse", "child", "parent", "sibling", "other"):
            member.relationship = d["relationship"]
        if "birthdate" in d:
            member.birthdate = d["birthdate"] or None
        member.save()

        return Response(
            {
                "id":           member.pk,
                "name":         member.name,
                "age":          member.age,
                "gender":       member.gender,
                "relationship": member.relationship,
                "birthdate":    str(member.birthdate) if member.birthdate else None,
            }
        )

    def delete(self, request, pk):
        member = self._get_member(pk, request.user)
        if not member:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        member.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

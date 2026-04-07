import json
import hmac
import hashlib
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings

# NOTE: This file is superseded by pharmacy/views.py PayMongoWebhookView.
# The active webhook handler lives there. This file is kept for reference only.

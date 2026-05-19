import logging
from io import BytesIO
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging
from django.core.files.base import ContentFile

from django.conf import settings


logger = logging.getLogger(__name__)


def create_image_thumbnail(upload, size=(200, 200)):
    if upload is None:
        return None
    content_type = getattr(upload, "content_type", "")
    if not content_type.startswith("image/"):
        return None
    try:
        from PIL import Image

        position = upload.tell() if hasattr(upload, "tell") else None
        image = Image.open(upload)
        image.thumbnail(size)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        if position is not None:
            upload.seek(position)
        name = f"thumb_{getattr(upload, 'name', 'image')}.jpg"
        return ContentFile(buffer.getvalue(), name=name)
    except Exception:
        logger.exception("Failed to generate image thumbnail")
        try:
            upload.seek(0)
        except Exception:
            pass
        return None


def get_firebase_app() -> Optional[firebase_admin.App]:
    if firebase_admin._apps:
        return firebase_admin.get_app()

    if not (settings.FIREBASE_PROJECT_ID and settings.FIREBASE_CLIENT_EMAIL and settings.FIREBASE_PRIVATE_KEY):
        logger.warning("Firebase credentials are not configured; push notifications disabled.")
        return None

    cred = credentials.Certificate(
        {
            "type": "service_account",
            "project_id": settings.FIREBASE_PROJECT_ID,
            "client_email": settings.FIREBASE_CLIENT_EMAIL,
            "private_key": settings.FIREBASE_PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    return firebase_admin.initialize_app(cred)


def send_push_notification(receiver, title, message, data=None):
    if not receiver.fcm_token:
        return False

    if get_firebase_app() is None:
        return False

    msg_data = {
        "title": title,
        "body": message,
        **(data or {}),
    }
    msg_data = {str(key): str(value) for key, value in msg_data.items()}

    fcm_message = messaging.Message(
        notification=messaging.Notification(title=title, body=message),
        data=msg_data,
        token=receiver.fcm_token,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                sound="default",
                click_action="FLUTTER_NOTIFICATION_CLICK",
            ),
        ),
    )

    try:
        messaging.send(fcm_message)
        return True
    except Exception:
        logger.exception("FCM send failed for user_id=%s", receiver.id)
        return False

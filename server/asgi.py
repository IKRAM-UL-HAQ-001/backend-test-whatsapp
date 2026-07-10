import os
import signal
import threading

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')

from django.core.asgi import get_asgi_application

# Initialise Django's app registry BEFORE importing anything that touches
# models. chat.routing imports consumers -> models, so importing it earlier
# crashes every worker at boot with AppRegistryNotReady.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

import chat.routing

from .health import set_shutting_down


def _handle_sigterm(signum, frame):
    set_shutting_down()


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _handle_sigterm)

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
            chat.routing.websocket_urlpatterns
        )
    ),
})

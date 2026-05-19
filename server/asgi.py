import os
import signal
import threading

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
import chat.routing
from django.core.asgi import get_asgi_application

from .health import set_shutting_down

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')


def _handle_sigterm(signum, frame):
    set_shutting_down()


if threading.current_thread() is threading.main_thread():
    signal.signal(signal.SIGTERM, _handle_sigterm)

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            chat.routing.websocket_urlpatterns
        )
    ),
})

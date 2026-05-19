
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from .views import HealthView, ReadyView

urlpatterns = [
    path("health/", HealthView.as_view()),
    path("ready/", ReadyView.as_view()),
    path("auth/", include("users.urls")),
    path("api/", include("chat.urls")),
    path("api/status/", include("status.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

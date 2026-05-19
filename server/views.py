from django.http import JsonResponse
from rest_framework.views import APIView

from .health import cache_ready, database_ready, is_shutting_down


class HealthView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return JsonResponse(
            {
                "status": "ok",
                "shutting_down": is_shutting_down(),
            }
        )


class ReadyView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        db_status = database_ready()
        cache_status = cache_ready()
        ready = db_status.ok and cache_status.ok and not is_shutting_down()
        status_code = 200 if ready else 503
        return JsonResponse(
            {
                "status": "ready" if ready else "not_ready",
                "shutting_down": is_shutting_down(),
                "database": db_status.detail,
                "cache": cache_status.detail,
            },
            status=status_code,
        )

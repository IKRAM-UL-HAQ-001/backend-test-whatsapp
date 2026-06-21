from django.urls import path

from .views import AcceptCall, CallDetail, CallHeartbeat, CallHistory, CancelCall, CurrentCall, EndCall, JoinCall, RejectCall, RingingCall, StartCall


urlpatterns = [
    path("start/", StartCall.as_view()),
    path("history/", CallHistory.as_view()),
    path("current/", CurrentCall.as_view()),
    path("<int:call_id>/", CallDetail.as_view()),
    path("<int:call_id>/join/", JoinCall.as_view()),
    path("<int:call_id>/ringing/", RingingCall.as_view()),
    path("<int:call_id>/heartbeat/", CallHeartbeat.as_view()),
    path("<int:call_id>/accept/", AcceptCall.as_view()),
    path("<int:call_id>/reject/", RejectCall.as_view()),
    path("<int:call_id>/cancel/", CancelCall.as_view()),
    path("<int:call_id>/end/", EndCall.as_view()),
]

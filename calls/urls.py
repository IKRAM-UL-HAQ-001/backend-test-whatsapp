from django.urls import path

from .views import AcceptCall, CallDetail, CallHistory, CancelCall, EndCall, JoinCall, RejectCall, StartCall


urlpatterns = [
    path("start/", StartCall.as_view()),
    path("history/", CallHistory.as_view()),
    path("<int:call_id>/", CallDetail.as_view()),
    path("<int:call_id>/join/", JoinCall.as_view()),
    path("<int:call_id>/accept/", AcceptCall.as_view()),
    path("<int:call_id>/reject/", RejectCall.as_view()),
    path("<int:call_id>/cancel/", CancelCall.as_view()),
    path("<int:call_id>/end/", EndCall.as_view()),
]

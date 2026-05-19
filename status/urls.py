from django.urls import path

from .views import (
    CreateStatusView,
    DeleteStatusView,
    MyStatusesView,
    StatusFeedView,
    StatusPrivacyView,
    StatusViewersView,
    ViewStatusView,
)

urlpatterns = [
    path("feed/", StatusFeedView.as_view()),
    path("my/", MyStatusesView.as_view()),
    path("create/", CreateStatusView.as_view()),
    path("privacy/", StatusPrivacyView.as_view()),
    path("<uuid:status_id>/delete/", DeleteStatusView.as_view()),
    path("<uuid:status_id>/view/", ViewStatusView.as_view()),
    path("<uuid:status_id>/views/", StatusViewersView.as_view()),
]

from django.urls import path

from . import views
from notifications import views as notification_views

urlpatterns = [
    path("open/", views.open_link, name="participant-open"),
    path("entry/", views.entry, name="participant-entry"),
    path("entry/status/", views.entry_status, name="participant-entry-status"),
    path("requests/", views.my_requests, name="participant-requests"),
    path("notifications/", notification_views.settings, name="participant-notifications"),
    path("notifications/skip/", notification_views.skip, name="participant-notifications-skip"),
]

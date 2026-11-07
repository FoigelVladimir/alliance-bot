from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="workspace-home"),
    path("staff/home/fragments/<str:section>/", views.home_fragment, name="workspace-home-fragment"),
    path("staff/notifications/", views.notifications, name="staff-notifications"),
    path("staff/notifications/<int:notification_id>/read/", views.read, name="staff-notification-read"),
]

from django.urls import path

from . import views

urlpatterns = [
    path("auth/vk/start/", views.start, name="vk-start"),
    path("auth/vk/callback/", views.callback, name="vk-callback"),
    path("auth/vk/recovery/", views.recovery, name="leader-recovery"),
    path("auth/vk/recovery/confirmed/", views.recovery_confirmed, name="leader-recovery-confirmed"),
    path("auth/logout/", views.logout, name="staff-logout"),
    path("access/application/", views.application, name="access-application"),
    path("staff/access/applications/", views.applications, name="staff-applications"),
    path("staff/access/applications/<int:application_id>/", views.application_detail, name="staff-application-detail"),
    path("staff/access/applications/<int:application_id>/approve/", views.decision, {"action": "approve"}, name="staff-approve"),
    path("staff/access/applications/<int:application_id>/reject/", views.decision, {"action": "reject"}, name="staff-reject"),
    path("staff/access/profiles/<int:profile_id>/change/", views.change_access, name="staff-change-access"),
]

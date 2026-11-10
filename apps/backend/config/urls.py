from django.urls import include, path

urlpatterns = [path("support/", include("support.urls")), path("knowledge/", include("knowledge.urls")),
               path("participant/", include("participants.urls")),
               path("events/", include("events.urls")),
               path("", include("workspace.urls")), path("", include("accounts.urls"))]

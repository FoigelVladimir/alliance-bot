from django.urls import include, path

urlpatterns = [path("support/", include("support.urls")), path("", include("workspace.urls")), path("", include("accounts.urls"))]

from django.urls import path

from . import views

urlpatterns = [
    path("materials/", views.materials, name="knowledge-materials"),
    path("materials/<int:material_id>/drafts/", views.new_draft, name="knowledge-new-draft"),
    path("materials/<int:material_id>/drafts/<int:version_id>/", views.draft, name="knowledge-draft"),
]

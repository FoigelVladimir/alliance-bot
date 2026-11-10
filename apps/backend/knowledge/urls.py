from django.urls import path

from . import source_views, views

urlpatterns = [
    path("materials/<int:material_id>/drafts/<int:version_id>/source/", source_views.attach, name="knowledge-source"),
    path("materials/<int:material_id>/drafts/<int:version_id>/table-selection/", source_views.table_selection, name="knowledge-table-selection"),
    path("materials/<int:material_id>/versions/<int:version_id>/refresh-google/", source_views.refresh_google, name="knowledge-refresh-google"),
    path("drafts/from-support/", views.from_support, name="knowledge-from-support"),
    path("materials/", views.materials, name="knowledge-materials"),
    path("materials/<int:material_id>/publish/", views.publication_command, {"action": "publish"}, name="knowledge-publish"),
    path("materials/<int:material_id>/unpublish/", views.publication_command, {"action": "unpublish"}, name="knowledge-unpublish"),
    path("materials/<int:material_id>/drafts/", views.new_draft, name="knowledge-new-draft"),
    path("materials/<int:material_id>/drafts/<int:version_id>/rebase/", views.rebase, name="knowledge-rebase"),
    path("materials/<int:material_id>/drafts/<int:version_id>/prepare/", views.prepare, name="knowledge-prepare"),
    path("materials/<int:material_id>/drafts/<int:version_id>/", views.draft, name="knowledge-draft"),
]

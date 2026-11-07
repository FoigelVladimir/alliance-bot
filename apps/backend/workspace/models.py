from django.db import models
from django.utils import timezone


class Notification(models.Model):
    source_event = models.ForeignKey("support.TicketEvent", on_delete=models.PROTECT)
    recipient = models.ForeignKey("accounts.StaffProfile", on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source_event", "recipient"], name="workspace_event_recipient_unique"),
        ]
        indexes = [
            models.Index(fields=["recipient", "-created_at", "-id"], name="workspace_recipient_feed"),
            models.Index(fields=["recipient"], condition=models.Q(read_at__isnull=True), name="workspace_unread_recipient"),
        ]

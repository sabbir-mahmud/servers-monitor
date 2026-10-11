"""An explicit "email is switched off" backend for deployments without SMTP.

The dashboard never tries to send mail unless EMAIL_HOST is set (Admins hand out reset and
invitation links instead), so this backend should never see a message. If something does
try, it says so loudly in the log rather than pretending the mail went out.
"""

import logging

from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class DisabledEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        for message in email_messages:
            logger.warning(
                "Email is not configured (set EMAIL_HOST); dropped %r to %s",
                message.subject,
                ", ".join(message.to),
            )
        return 0

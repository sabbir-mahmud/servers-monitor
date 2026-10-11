from django.db import models

from . import crypto


class EncryptedTextField(models.TextField):
    """Stored encrypted (see crypto.py), read back as plaintext. Not usable in lookups:
    every encryption is randomised, so equal values never compare equal in SQL."""

    def from_db_value(self, value, expression, connection):
        return crypto.decrypt(value) if value else value

    def to_python(self, value):
        return crypto.decrypt(value) if isinstance(value, str) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value or value.startswith(crypto.PREFIX):
            return value
        return crypto.encrypt(value)

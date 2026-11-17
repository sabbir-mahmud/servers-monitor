"""Migration operations Django doesn't ship."""

from django.db import models
from django.db.migrations.operations.base import Operation


class SetModelBases(Operation):
    """Change a model's bases in the migration state only.

    Django has no operation for this: the autodetector never compares bases. It is
    needed when an existing model becomes a multi-table-inheritance child, so historical
    models in later data migrations see the inherited fields. Run it after the parent
    link exists and the fields that moved to the parent are gone from the child.
    """

    reduces_to_sql = False
    reversible = True

    def __init__(self, model_name, bases, old_bases=(models.Model,)):
        self.model_name = model_name.lower()
        self.bases = tuple(bases)
        self.old_bases = tuple(old_bases)

    def deconstruct(self):
        kwargs = {"model_name": self.model_name, "bases": self.bases}
        return self.__class__.__qualname__, [], kwargs

    def _set(self, app_label, state, bases):
        state.models[app_label, self.model_name].bases = bases
        state.reload_model(app_label, self.model_name, delay=True)

    def state_forwards(self, app_label, state):
        self._set(app_label, state, self.bases)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def state_backwards(self, app_label, state):  # pragma: no cover - never run in prod
        self._set(app_label, state, self.old_bases)

    def describe(self):
        return f"Set bases of {self.model_name} to {self.bases}"

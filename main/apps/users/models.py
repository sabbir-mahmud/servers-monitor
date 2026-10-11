from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """Custom from day one so adding fields later never needs a swap migration."""

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"

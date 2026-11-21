from django.core.management.base import BaseCommand, CommandError

from apps.agent_api import relay


class Command(BaseCommand):
    help = (
        "Create (or reset) the NexGuard phone app's shared relay login and print the lines "
        "for phone-app/.env. The login may only send pairing requests and read answers; "
        "resetting it stops apps built with the old one from pairing (paired phones keep "
        "reporting)."
    )

    def handle(self, *args, **options):
        try:
            login = relay.provision_app_login()
        except relay.RelayError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write("# phone-app/.env (rebuild the app after changing it)")
        self.stdout.write(f"EXPO_PUBLIC_REDIS_URL={login['url']}")
        self.stdout.write(f"EXPO_PUBLIC_REDIS_TOKEN={login['token']}")
        self.stdout.write(f"EXPO_PUBLIC_REDIS_PREFIX={login['prefix']}")

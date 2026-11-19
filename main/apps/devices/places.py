"""Places (geofences): arriving, leaving, and "hasn't arrived by 8:30".

Each usable location fix is compared with the account's places. A device is inside a place
within its radius, and counts as having left only beyond the radius plus a margin (so a fix
wobbling on the edge doesn't announce arrive/leave/arrive). Town-level fixes (an internet
address) are too coarse and are ignored. The first fix after a place is added only sets
the state: nobody is told "Sara arrived at Home" because Home was just drawn around her.

Arrivals and departures are announced as event alerts (opened and resolved at once, sent
once to every matching channel). A missed arrival is an ordinary alert that resolves when
the person arrives, or at the end of the day.
"""

import math
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.alerts import device_rules
from apps.alerts.models import Alert, AlertKind, Severity

from .models import Device, DevicePlace, Place

MAX_ACCURACY_M = 500  # coarser fixes can't tell School from the shop next door
LEAVE_MARGIN_M = 75
EARTH_M = 6_371_000


def distance_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp, dl = p2 - p1, math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(a))


def _counts(place: Place, device: Device) -> bool:
    people = [p.pk for p in place.people.all()]
    return not people or device.person_id in people


def _event(device, place, arrived: bool, at) -> None:
    who = device.person.name if device.person_id else device.name
    verb = "arrived at" if arrived else "left"
    Alert.objects.create(
        account_id=device.account_id, device=device, kind=AlertKind.PLACE, is_event=True,
        subject=f"{place.pk}:{'in' if arrived else 'out'}:{int(at.timestamp())}",
        severity=Severity.WARNING, message=f"{who} {verb} {place.name}"[:300],
        resolved_at=timezone.now(),
    )  # fmt: skip


def late_subject(place, person) -> str:
    return f"late:{place.pk}:{person.pk}"


def evaluate(device: Device, latitude, longitude, accuracy_m, at) -> None:
    """Compare one fresh location fix with the account's places."""
    if accuracy_m is not None and accuracy_m > MAX_ACCURACY_M:
        return
    places = Place.objects.filter(account_id=device.account_id).prefetch_related("people")
    states = {s.place_id: s for s in DevicePlace.objects.filter(device=device)}
    for place in places:
        if not _counts(place, device):
            continue
        distance = distance_m(latitude, longitude, place.latitude, place.longitude)
        state = states.get(place.pk)
        if distance <= place.radius_m:
            inside = True
        elif distance > place.radius_m + LEAVE_MARGIN_M:
            inside = False
        else:
            continue  # on the edge: keep whatever it was
        if state is None:
            DevicePlace.objects.create(device=device, place=place, inside=inside, changed_at=at,
                                       last_arrived_at=at if inside else None)  # fmt: skip
            continue
        if state.inside == inside:
            continue
        with transaction.atomic():
            state.inside, state.changed_at = inside, at
            if inside:
                state.last_arrived_at = at
            state.save()
            if (place.notify_arrive and inside) or (place.notify_leave and not inside):
                _event(device, place, inside, at)
            if inside and device.person_id:
                _resolve_late(place, device.person_id)


def _resolve_late(place, person_id) -> None:
    Alert.objects.filter(kind=AlertKind.PLACE, subject=f"late:{place.pk}:{person_id}",
                         resolved_at=None).update(resolved_at=timezone.now())  # fmt: skip


def _arrived_since(place, person, since) -> bool:
    return DevicePlace.objects.filter(
        place=place, device__person=person, last_arrived_at__gte=since
    ).exists()


def sweep(now=None) -> int:
    """Open "hasn't arrived" alerts that are due; close yesterday's. Returns how many opened."""
    now = now or timezone.now()
    local = timezone.localtime(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Yesterday's misses are history now.
    Alert.objects.filter(kind=AlertKind.PLACE, is_event=False, subject__startswith="late:",
                         resolved_at=None, opened_at__lt=midnight).update(resolved_at=now)  # fmt: skip
    opened = 0
    places = Place.objects.filter(arrive_by__isnull=False).prefetch_related("people")
    for place in places:
        if local.weekday() not in (place.arrive_days or []):
            continue
        due = datetime.combine(local.date(), place.arrive_by, tzinfo=local.tzinfo)
        if local < due:
            continue
        for person in place.people.all():
            subject = late_subject(place, person)
            if Alert.objects.filter(kind=AlertKind.PLACE, subject=subject,
                                    opened_at__gte=midnight).exists():  # fmt: skip
                continue  # already raised today (open or resolved)
            if _arrived_since(place, person, midnight):
                continue
            device = (Device.objects.filter(person=person).exclude(token_hash="")
                      .order_by("-last_seen_at").first())  # fmt: skip
            if device is None:
                continue
            device_rules.open_alert(
                device, AlertKind.PLACE,
                f"{person.name} hasn't arrived at {place.name} (expected by "
                f"{place.arrive_by:%H:%M})", Severity.WARNING, subject=subject,
            )  # fmt: skip
            opened += 1
    return opened

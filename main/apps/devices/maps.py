"""Small static maps drawn from map tiles, with no JavaScript library.

The server works out which 256 px tiles surround a location (Web Mercator, the scheme
every tile server uses) and where the pin, the accuracy circle and the recent trail fall
on them. The template positions plain <img> tiles; CSS centres the pin in whatever
width the card has. 5 × 3 tiles cover cards up to ~1000 × 500 px around the pin.

Tiles come from MAP_TILE_URL (OpenStreetMap by default: free, needs the attribution the
template shows, fine for a family dashboard's light use). Set it empty to turn maps off;
pages then show coordinates and "open map" links only. The viewer's browser fetches the
tiles, so the tile server learns roughly which area is being looked at, never who.
"""

import math
from urllib.parse import urlsplit

from django.conf import settings

TILE = 256
COLS, ROWS = 5, 3  # tiles around the pin's tile: -2..2 across, -1..1 down


def enabled() -> bool:
    return bool(settings.MAP_TILE_URL)


def tile_origin() -> str | None:
    """scheme://host of the tile server, for the Content-Security-Policy."""
    parts = urlsplit(settings.MAP_TILE_URL or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else None


def _world_px(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat = max(min(lat, 85.0511), -85.0511)
    scale = TILE * 2**zoom
    x = (lon + 180.0) / 360.0 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def zoom_for(accuracy_m) -> int:
    """Close in on a GPS fix; step back for a vague Wi-Fi/IP one so the circle fits."""
    if accuracy_m is None or accuracy_m <= 150:
        return 16
    if accuracy_m <= 600:
        return 15
    if accuracy_m <= 2500:
        return 13
    return 11


def static_map(lat, lon, accuracy_m=None, trail=()) -> dict | None:
    """Everything the _map.html template needs, or None when maps are off.
    `trail` is an iterable of (lat, lon), oldest first; points off the tiles are dropped."""
    if not enabled() or lat is None or lon is None:
        return None
    lat, lon = float(lat), float(lon)
    zoom = zoom_for(accuracy_m)
    px, py = _world_px(lat, lon, zoom)
    cx, cy = int(px // TILE), int(py // TILE)
    left, top = (cx - COLS // 2) * TILE, (cy - ROWS // 2) * TILE  # layer origin in world px
    n = 2**zoom
    tiles = [
        {
            "url": settings.MAP_TILE_URL.format(z=zoom, x=tx % n, y=ty),
            "x": (tx - cx + COLS // 2) * TILE,
            "y": (ty - cy + ROWS // 2) * TILE,
        }
        for ty in range(cy - ROWS // 2, cy + ROWS // 2 + 1)
        if 0 <= ty < n
        for tx in range(cx - COLS // 2, cx + COLS // 2 + 1)
    ]
    width, height = COLS * TILE, ROWS * TILE
    pin = (round(px - left), round(py - top))
    radius = None
    if accuracy_m:
        metres_per_px = 156543.03392 * math.cos(math.radians(lat)) / n
        radius = min(round(accuracy_m / metres_per_px), 600)
        radius = radius if radius >= 8 else None  # smaller than the pin: not worth drawing
    points = []
    for t_lat, t_lon in trail:
        x, y = _world_px(float(t_lat), float(t_lon), zoom)
        x, y = round(x - left), round(y - top)
        if 0 <= x <= width and 0 <= y <= height:
            points.append((x, y))
    return {
        "zoom": zoom,
        "tiles": tiles,
        "width": width,
        "height": height,
        "pin_x": pin[0],
        "pin_y": pin[1],
        "radius": radius,
        "trail": " ".join(f"{x},{y}" for x, y in points) if len(points) >= 2 else "",
        "dots": points[:-1] if points else [],
        "attribution": settings.MAP_ATTRIBUTION,
    }


def trail_for(device, hours: int = 24) -> list[tuple]:
    """The device's path over the last day, oldest first (for the map's trail)."""
    from datetime import timedelta

    from django.utils import timezone

    since = timezone.now() - timedelta(hours=hours)
    rows = device.locations.filter(recorded_at__gte=since).order_by("-recorded_at")
    return list(rows.values_list("latitude", "longitude")[:300])[::-1]


def for_device(device) -> dict | None:
    if not device.has_location:
        return None
    return static_map(
        device.latitude, device.longitude, device.location_accuracy_m, trail_for(device)
    )


def open_url(lat, lon) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}"

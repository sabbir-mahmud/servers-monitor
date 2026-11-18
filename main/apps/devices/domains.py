"""Domain names as parents type them and as agents report them.

Parents paste anything ("https://www.Example.com/path", "*.example.com"); agents send
what they reduced a URL to. Both end up as one lowercase hostname or nothing.
"""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_LIST = 2000  # per list on a device: the hosts file has to stay small (see block.go)


def normalize(raw: object) -> str:
    """A bare lowercase hostname, or "" if it isn't one."""
    text = str(raw or "").strip().lower()
    if "://" in text:
        text = urlsplit(text).hostname or ""
    text = text.split("/", 1)[0].split(":", 1)[0].removeprefix("*.").rstrip(".")
    try:
        text = text.encode("idna").decode("ascii")  # bücher.de -> xn--bcher-kva.de
    except UnicodeError:
        return ""
    labels = text.split(".")
    if len(text) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return ""
    if labels[-1].isdigit():  # an IP address, not a name
        return ""
    return text


def parse_list(text: str) -> list[str]:
    """One domain per line (commas and spaces work too); raises ValidationError naming the
    entries that aren't domains. Duplicates are dropped, order kept."""
    entries = [e for e in re.split(r"[\s,]+", text or "") if e and not e.startswith("#")]
    bad = [e for e in entries if not normalize(e)]
    if bad:
        shown = ", ".join(bad[:5]) + (" …" if len(bad) > 5 else "")
        raise ValidationError(f"Not a website address: {shown}")
    domains = list(dict.fromkeys(normalize(e) for e in entries))
    if len(domains) > MAX_LIST:
        raise ValidationError(f"At most {MAX_LIST} sites per list.")
    return domains

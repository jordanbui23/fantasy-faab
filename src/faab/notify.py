"""ntfy delivery.

ntfy.sh is free, needs no signup, and is already installed on the owner's phone. The
topic name IS the credential, because anyone who knows it can publish to and read the
topic, so it comes from the environment and never from a committed file.

A message over 4,096 bytes is converted by the server into a file attachment rather
than a notification. `faab.report.fit_to_ntfy` is responsible for staying under that;
this module refuses to send an oversized body so the failure is loud rather than a
notification that quietly stopped appearing.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests

from faab.report import NTFY_MAX_BYTES

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_SECONDS = 20
TOPIC_ENV = "FAAB_NTFY_TOPIC"
SERVER_ENV = "FAAB_NTFY_SERVER"


class NotifyError(RuntimeError):
    """Raised when a notification could not be delivered."""


def resolve_topic(explicit: str | None = None) -> str:
    topic = explicit or os.environ.get(TOPIC_ENV, "")
    topic = topic.strip()
    if not topic:
        raise NotifyError(
            f"no ntfy topic: pass one, or set {TOPIC_ENV} in the environment"
        )
    if "/" in topic or not topic:
        raise NotifyError(f"invalid ntfy topic {topic!r}: it must not contain a slash")
    return topic


def send(
    body: str,
    title: str,
    topic: str | None = None,
    server: str | None = None,
    priority: str = "default",
    tags: str = "football",
) -> None:
    """Publish one notification. Raises NotifyError on anything but success."""
    resolved_topic = resolve_topic(topic)
    base = (server or os.environ.get(SERVER_ENV) or DEFAULT_SERVER).rstrip("/")
    encoded = body.encode("utf-8")

    if not encoded:
        raise NotifyError("refusing to send an empty notification")
    if len(encoded) > NTFY_MAX_BYTES:
        raise NotifyError(
            f"body is {len(encoded)} bytes, over ntfy's {NTFY_MAX_BYTES} byte limit; "
            "it would arrive as a file attachment instead of a notification"
        )

    try:
        response = requests.post(
            f"{base}/{quote(resolved_topic, safe='')}",
            data=encoded,
            headers={
                # Latin-1 is the header charset, and a title is ASCII here, but encode
                # defensively so a stray character cannot make the request unsendable.
                "Title": title.encode("ascii", "replace").decode("ascii"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise NotifyError(f"ntfy publish failed: {exc}") from exc

    if response.status_code != 200:
        raise NotifyError(f"ntfy returned HTTP {response.status_code}: {response.text[:200]}")

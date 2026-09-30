"""Yahoo OAuth2, read-only.

Yahoo gated its Fantasy API behind manual approval in mid-2026. Approval arrived, so this
replaces the pasted-text stopgap for everything the API exposes.

The flow is authorization-code with a refresh token. A human authorizes once in a browser,
and the refresh token then carries the cron job indefinitely without further interaction.
That is the only reason this is worth the complexity: a weekly report cannot prompt anybody.

Three things here are deliberate rather than incidental.

The redirect URI is never contacted. Yahoo requires HTTPS and rejects plain localhost, so
`https://localhost:8000` is registered and the browser's failure to connect is expected: the
authorization code arrives in the address bar, which is all that is needed. Running a local
HTTPS server to catch it would add a certificate to manage and prove nothing extra.

The token file holds a live credential, so it is written with 0600 and into `data/`, which is
gitignored alongside every other piece of league data.

Nothing here can write. The approved scope is Fantasy Sports Read, and `AGENTS.md` forbids
designing toward a write path even if the scope were widened.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

# Refresh this many seconds before the token actually expires. A weekly cron run that starts
# just inside the window would otherwise fail mid-report on an expired token.
EXPIRY_MARGIN = 300

TIMEOUT = 20


class AuthError(RuntimeError):
    """Authorization failed, with Yahoo's own message where it gave one."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Credentials:
        source = env if env is not None else os.environ
        missing = [
            name
            for name in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REDIRECT_URI")
            if not source.get(name)
        ]
        if missing:
            raise AuthError(f"missing from the environment: {', '.join(missing)}")
        return cls(
            client_id=source["YAHOO_CLIENT_ID"],
            client_secret=source["YAHOO_CLIENT_SECRET"],
            redirect_uri=source["YAHOO_REDIRECT_URI"],
        )


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at - EXPIRY_MARGIN

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
            },
            indent=2,
        )

    @classmethod
    def from_response(cls, payload: dict, now: float | None = None) -> Token:
        for field in ("access_token", "refresh_token", "expires_in"):
            if field not in payload:
                raise AuthError(f"Yahoo's response has no {field}: {sorted(payload)}")
        moment = now if now is not None else time.time()
        return cls(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            expires_at=moment + float(payload["expires_in"]),
        )


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=value file.

    Written by hand rather than with a dependency, because the file holds four lines and
    `requests` is meant to stay this project's only runtime dependency.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def authorization_url(credentials: Credentials) -> str:
    """The URL a human opens once to grant access."""
    return f"{AUTHORIZE_URL}?" + urlencode(
        {
            "client_id": credentials.client_id,
            "redirect_uri": credentials.redirect_uri,
            "response_type": "code",
            "language": "en-us",
        }
    )


def code_from_redirect(pasted: str) -> str:
    """The authorization code, from either a bare code or the whole redirected URL.

    Accepts the full URL because that is what a browser puts on the clipboard, and asking
    somebody to surgically extract a query parameter invites the one-character error that
    makes the next call fail with an opaque message.
    """
    text = pasted.strip()
    if "code=" not in text:
        if not text or " " in text:
            raise AuthError("no authorization code found in that text")
        return text
    tail = text.split("code=", 1)[1]
    for separator in ("&", "#", " "):
        tail = tail.split(separator, 1)[0]
    if not tail:
        raise AuthError("the code= parameter is empty")
    return tail


def _post(credentials: Credentials, form: dict[str, str]) -> Token:
    body = {
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "redirect_uri": credentials.redirect_uri,
        **form,
    }
    try:
        response = requests.post(TOKEN_URL, data=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise AuthError(f"could not reach Yahoo: {exc}") from exc

    if response.status_code != 200:
        # Yahoo returns a JSON error body on a bad code or a stale refresh token, and its
        # message is far more useful than the status alone.
        detail = response.text[:300]
        raise AuthError(f"Yahoo refused the token request ({response.status_code}): {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthError(f"Yahoo's response was not JSON: {response.text[:200]}") from exc
    return Token.from_response(payload)


def exchange_code(credentials: Credentials, code: str) -> Token:
    """Trade a one-time authorization code for an access and refresh token pair."""
    return _post(credentials, {"code": code, "grant_type": "authorization_code"})


def refresh(credentials: Credentials, token: Token) -> Token:
    """Trade a refresh token for a fresh access token.

    Yahoo returns a refresh token in this response too. It is stored rather than assumed
    unchanged, because a provider that rotates refresh tokens and a provider that does not
    are indistinguishable until the day the old one stops working.
    """
    return _post(
        credentials,
        {"refresh_token": token.refresh_token, "grant_type": "refresh_token"},
    )


def save_token(path: Path, token: Token) -> None:
    """Write the token with owner-only permissions.

    The mode is set on the file descriptor rather than after the write, so the secret is
    never briefly readable by anyone else.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token.to_json())


def load_token(path: Path) -> Token | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise AuthError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return Token(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload["refresh_token"]),
            expires_at=float(payload["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError(f"{path} is missing a token field: {exc}") from exc


def access_token(credentials: Credentials, path: Path) -> str:
    """A usable access token, refreshed and re-saved when it has aged out.

    Raises rather than starting a browser flow when no token exists, because this runs from
    cron where nobody can answer a prompt.
    """
    token = load_token(path)
    if token is None:
        raise AuthError(
            f"no token at {path}. Run `python -m faab auth` once to authorize."
        )
    if not token.expired:
        return token.access_token
    renewed = refresh(credentials, token)
    save_token(path, renewed)
    return renewed.access_token

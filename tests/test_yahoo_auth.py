"""Tests for Yahoo OAuth2.

No test here touches the network. The one guard worth proving is the token file's
permissions, because that file holds a live credential.
"""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faab.collectors import yahoo_auth as auth  # noqa: E402

CREDENTIALS = auth.Credentials(
    client_id="a-client-id",
    client_secret="a-secret",
    redirect_uri="https://localhost:8000",
)


def _token(expires_in=3600.0, now=1000.0):
    return auth.Token.from_response(
        {"access_token": "at", "refresh_token": "rt", "expires_in": expires_in}, now=now
    )


# --- credentials ---------------------------------------------------------------


def test_credentials_read_from_a_mapping():
    creds = auth.Credentials.from_env(
        {
            "YAHOO_CLIENT_ID": "i",
            "YAHOO_CLIENT_SECRET": "s",
            "YAHOO_REDIRECT_URI": "https://localhost:8000",
        }
    )
    assert creds.client_id == "i"


def test_a_missing_credential_is_named():
    with pytest.raises(auth.AuthError) as caught:
        auth.Credentials.from_env({"YAHOO_CLIENT_ID": "i"})
    message = str(caught.value)
    assert "YAHOO_CLIENT_SECRET" in message
    assert "YAHOO_REDIRECT_URI" in message


def test_an_empty_credential_counts_as_missing():
    with pytest.raises(auth.AuthError):
        auth.Credentials.from_env(
            {
                "YAHOO_CLIENT_ID": "",
                "YAHOO_CLIENT_SECRET": "s",
                "YAHOO_REDIRECT_URI": "u",
            }
        )


# --- the env file --------------------------------------------------------------


def test_the_env_file_is_parsed(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a note\n\nA=1\nB = two \nBAD_LINE\n")
    assert auth.read_env_file(path) == {"A": "1", "B": "two"}


def test_a_value_containing_an_equals_sign_survives(tmp_path):
    """A base64 client id can end in padding, and a secret can hold anything."""
    path = tmp_path / ".env"
    path.write_text("KEY=abc==def\n")
    assert auth.read_env_file(path)["KEY"] == "abc==def"


def test_a_missing_env_file_is_empty_not_an_error(tmp_path):
    assert auth.read_env_file(tmp_path / "absent") == {}


# --- the authorization URL -----------------------------------------------------


def test_the_authorization_url_carries_the_registered_redirect():
    url = auth.authorization_url(CREDENTIALS)
    assert url.startswith(auth.AUTHORIZE_URL)
    assert "response_type=code" in url
    assert "redirect_uri=https%3A%2F%2Flocalhost%3A8000" in url


# --- reading the code back from a browser --------------------------------------


def test_the_code_is_taken_from_a_whole_redirected_url():
    assert auth.code_from_redirect("https://localhost:8000/?code=abc123") == "abc123"


def test_a_trailing_parameter_is_not_part_of_the_code():
    assert auth.code_from_redirect("https://localhost:8000/?code=abc&state=x") == "abc"


def test_a_fragment_is_not_part_of_the_code():
    assert auth.code_from_redirect("https://localhost:8000/?code=abc#done") == "abc"


def test_a_bare_code_is_accepted():
    assert auth.code_from_redirect("  abc123  ") == "abc123"


def test_an_empty_code_parameter_is_refused():
    with pytest.raises(auth.AuthError):
        auth.code_from_redirect("https://localhost:8000/?code=")


def test_prose_is_not_mistaken_for_a_code():
    with pytest.raises(auth.AuthError):
        auth.code_from_redirect("I could not find the code")


# --- expiry --------------------------------------------------------------------


def test_a_fresh_token_is_not_expired(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert not _token(expires_in=3600.0).expired


def test_a_token_inside_the_margin_counts_as_expired(monkeypatch):
    """A weekly run starting just inside the window must not fail mid-report."""
    monkeypatch.setattr(time, "time", lambda: 1000.0 + 3600.0 - auth.EXPIRY_MARGIN + 1)
    assert _token(expires_in=3600.0).expired


def test_a_response_without_a_refresh_token_is_refused():
    with pytest.raises(auth.AuthError) as caught:
        auth.Token.from_response({"access_token": "at", "expires_in": 3600})
    assert "refresh_token" in str(caught.value)


# --- the token file, which holds a live credential -----------------------------


def test_the_token_file_is_owner_only(tmp_path):
    path = tmp_path / "nested" / "yahoo_token.json"
    auth.save_token(path, _token())
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token written world-readable as {oct(mode)}"


def test_the_token_round_trips(tmp_path):
    path = tmp_path / "t.json"
    original = _token()
    auth.save_token(path, original)
    assert auth.load_token(path) == original


def test_a_missing_token_file_reads_as_none(tmp_path):
    assert auth.load_token(tmp_path / "absent") is None


def test_a_corrupt_token_file_is_an_error_not_a_silent_none(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{not json")
    with pytest.raises(auth.AuthError):
        auth.load_token(path)


def test_a_token_file_missing_a_field_is_an_error(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"access_token": "at"}))
    with pytest.raises(auth.AuthError):
        auth.load_token(path)


# --- what cron does -----------------------------------------------------------


def test_no_token_tells_the_operator_to_authorize(tmp_path):
    """Cron cannot answer a browser prompt, so this must raise rather than start one."""
    with pytest.raises(auth.AuthError) as caught:
        auth.access_token(CREDENTIALS, tmp_path / "absent")
    assert "faab auth" in str(caught.value)


def test_a_valid_token_is_returned_without_a_refresh(tmp_path, monkeypatch):
    path = tmp_path / "t.json"
    auth.save_token(path, _token(expires_in=3600.0, now=time.time()))

    def fail(*args, **kwargs):
        raise AssertionError("refreshed a token that was still valid")

    monkeypatch.setattr(auth, "refresh", fail)
    assert auth.access_token(CREDENTIALS, path) == "at"


def test_an_expired_token_is_refreshed_and_re_saved(tmp_path, monkeypatch):
    path = tmp_path / "t.json"
    auth.save_token(path, _token(expires_in=1.0, now=time.time() - 10))
    renewed = auth.Token(access_token="new-at", refresh_token="new-rt",
                         expires_at=time.time() + 3600)
    monkeypatch.setattr(auth, "refresh", lambda creds, token: renewed)

    assert auth.access_token(CREDENTIALS, path) == "new-at"
    assert auth.load_token(path).refresh_token == "new-rt", "rotated token was not saved"


def test_a_rotated_refresh_token_is_persisted(tmp_path, monkeypatch):
    """Yahoo may rotate the refresh token, and assuming otherwise fails silently later."""
    path = tmp_path / "t.json"
    auth.save_token(path, _token(expires_in=1.0, now=time.time() - 10))
    monkeypatch.setattr(
        auth,
        "refresh",
        lambda creds, token: auth.Token("a2", "rotated", time.time() + 3600),
    )
    auth.access_token(CREDENTIALS, path)
    assert auth.load_token(path).refresh_token == "rotated"

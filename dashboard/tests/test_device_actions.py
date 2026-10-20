"""Tests for the delete confirmation flow.

Streamlit is swapped for a stub so the branching can be exercised without a
browser: what matters here is that a mistyped name never deletes, that a
correct one calls the right endpoint, and that a failure leaves the panel armed
so the operator can retry rather than losing their place.
"""

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import device_actions  # noqa: E402
from api_client import ApiError  # noqa: E402


class FakeColumn:
    def __init__(self, owner, index):
        self._owner = owner
        self._index = index

    def button(self, label, **_kw):
        return self._owner.clicked == (self._index, label)


class FakeStreamlit:
    """Minimal stand-in for the handful of Streamlit calls this module uses."""

    def __init__(self, *, typed="", clicked=None):
        self.session_state = {}
        self.typed = typed
        # (column index, button label) that the operator "pressed".
        self.clicked = clicked
        self.errors = []
        self.successes = []
        self.markdown_calls = []
        self.reran = False

    @contextmanager
    def container(self, **_kw):
        yield

    def columns(self, n, **_kw):
        return [FakeColumn(self, i) for i in range(n)]

    def text_input(self, _label, **_kw):
        return self.typed

    def button(self, label, **_kw):
        # Buttons outside a column are addressed as column -1.
        return self.clicked == (-1, label)

    def error(self, message):
        self.errors.append(message)

    def success(self, message):
        self.successes.append(message)

    def markdown(self, message):
        self.markdown_calls.append(message)

    def caption(self, _message):
        pass

    def rerun(self):
        self.reran = True


class FakeApi:
    def __init__(self, error=None):
        self.deleted = []
        self.error = error

    def delete(self, path):
        if self.error:
            raise self.error
        self.deleted.append(path)


@pytest.fixture
def st(monkeypatch):
    fake = FakeStreamlit()
    monkeypatch.setattr(device_actions, "st", fake)
    return fake


DEVICE_ID = "11111111-2222-3333-4444-555555555555"
NAME = "web-01"


def arm(st):
    device_actions.request_delete(DEVICE_ID, NAME)


# --------------------------------------------------------------------------- #
# Arming and state
# --------------------------------------------------------------------------- #


def test_nothing_renders_when_not_armed(st):
    api = FakeApi()
    assert device_actions.render_confirmation(api) is False
    assert api.deleted == []


def test_request_delete_arms_for_that_device(st):
    arm(st)
    assert device_actions.is_pending(DEVICE_ID) is True
    assert device_actions.is_pending("another-id") is False
    assert device_actions.is_pending() is True


def test_cancel_disarms(st):
    arm(st)
    device_actions.cancel()
    assert device_actions.is_pending() is False


def test_uuid_objects_are_normalised_to_strings(st):
    """The API returns ids as UUID objects in some paths; comparison must not
    depend on which one the caller happened to pass."""
    import uuid

    real = uuid.UUID(DEVICE_ID)
    device_actions.request_delete(real, NAME)
    assert device_actions.is_pending(DEVICE_ID) is True
    assert device_actions.is_pending(real) is True


# --------------------------------------------------------------------------- #
# The confirmation itself
# --------------------------------------------------------------------------- #


def test_armed_panel_does_not_delete_until_confirmed(st):
    arm(st)
    api = FakeApi()
    assert device_actions.render_confirmation(api) is False
    assert api.deleted == []
    assert device_actions.is_pending() is True


def test_wrong_name_refuses_to_delete(st):
    arm(st)
    st.typed = "web-02"
    st.clicked = (0, "Delete permanently")
    api = FakeApi()

    assert device_actions.render_confirmation(api) is False
    assert api.deleted == []
    assert any("does not match" in message for message in st.errors)
    # Still armed, so a typo does not make the operator start over.
    assert device_actions.is_pending() is True


def test_blank_name_refuses_to_delete(st):
    arm(st)
    st.typed = "   "
    st.clicked = (0, "Delete permanently")
    api = FakeApi()

    assert device_actions.render_confirmation(api) is False
    assert api.deleted == []


def test_correct_name_deletes_and_disarms(st):
    arm(st)
    st.typed = NAME
    st.clicked = (0, "Delete permanently")
    api = FakeApi()

    assert device_actions.render_confirmation(api) is True
    assert api.deleted == [f"/devices/{DEVICE_ID}"]
    assert device_actions.is_pending() is False
    assert any(NAME in message for message in st.successes)


def test_surrounding_whitespace_is_tolerated(st):
    arm(st)
    st.typed = f"  {NAME}  "
    st.clicked = (0, "Delete permanently")
    api = FakeApi()

    assert device_actions.render_confirmation(api) is True
    assert api.deleted == [f"/devices/{DEVICE_ID}"]


def test_api_failure_keeps_the_panel_armed(st):
    """A 500 or a dropped connection must not look like a successful delete,
    and must not force the operator to re-arm and retype."""
    arm(st)
    st.typed = NAME
    st.clicked = (0, "Delete permanently")
    api = FakeApi(error=ApiError(500, "boom", "Server exploded"))

    assert device_actions.render_confirmation(api) is False
    assert device_actions.is_pending() is True
    assert "Server exploded" in st.errors


def test_cancel_button_disarms_and_reruns(st):
    arm(st)
    st.clicked = (1, "Cancel")
    api = FakeApi()

    assert device_actions.render_confirmation(api) is False
    assert api.deleted == []
    assert device_actions.is_pending() is False
    assert st.reran is True


def test_panel_states_what_will_be_lost(st):
    arm(st)
    api = FakeApi()
    device_actions.render_confirmation(api, package_count=322)

    blurb = " ".join(st.markdown_calls)
    assert "322 packages" in blurb
    assert "change history" in blurb
    assert "no undo" in blurb.lower()
    # The operator is told the watchdog keeps beating until it is uninstalled,
    # which is the surprising part of deleting a live device.
    assert "--uninstall" in blurb


def test_package_count_is_optional(st):
    """With no count available the sentence still reads correctly, rather than
    rendering "(None packages)"."""
    arm(st)
    api = FakeApi()
    device_actions.render_confirmation(api)
    blurb = " ".join(st.markdown_calls)
    assert "its current package inventory" in blurb
    assert "None" not in blurb
    assert "(" not in blurb.split("inventory")[1].split("\n")[0]

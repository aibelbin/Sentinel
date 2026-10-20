import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import views_liva  # noqa: E402
from liva_client import Liva, LivaError, LivaUnavailable  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, lines=None, content=b""):
        self.status_code = status
        self._body = body
        self._lines = lines or []
        self.content = content
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


def patch_request(monkeypatch, response=None, exc=None):
    seen = {}

    def fake(self, method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        if exc:
            raise exc
        return response

    monkeypatch.setattr(requests.Session, "request", fake)
    return seen


def test_unreachable_liva_is_liva_unavailable(monkeypatch):
    patch_request(monkeypatch, exc=requests.ConnectionError("refused"))
    with pytest.raises(LivaUnavailable):
        Liva("tok", "admin").health()


def test_requests_carry_the_session_token_and_user(monkeypatch):
    seen = patch_request(monkeypatch, FakeResponse(body=[]))
    Liva("tok", "admin").chats()
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"]["X-Liva-User"] == "admin"


def test_error_responses_become_liva_error(monkeypatch):
    patch_request(monkeypatch, FakeResponse(409, {"detail": "Approve or deny the pending action first."}))
    with pytest.raises(LivaError) as err:
        list(Liva("t", "a").send("c1", "hi", False))
    assert "pending action" in err.value.message and err.value.status == 409


def test_send_yields_decoded_events(monkeypatch):
    lines = [json.dumps({"type": "message", "content": "hi"}), "", json.dumps({"type": "done"})]
    patch_request(monkeypatch, FakeResponse(lines=lines))
    events = list(Liva("t", "a").send("c1", "hello", True))
    assert [e["type"] for e in events] == ["message", "done"]


def test_send_posts_bypass_flag(monkeypatch):
    seen = patch_request(monkeypatch, FakeResponse(lines=[]))
    list(Liva("t", "a").send("c1", "hello", True))
    assert seen["json"] == {"content": "hello", "bypass": True}
    assert seen["stream"] is True


class StubStreamlit:
    """Records what render() shows; any call it does not define is a test failure."""

    def __init__(self):
        self.shown = []
        self.session_state = {"token": "tok", "username": "admin"}

    def title(self, text):
        self.shown.append(("title", text))

    def info(self, text):
        self.shown.append(("info", text))

    def warning(self, text):
        self.shown.append(("warning", text))


def test_render_degrades_to_a_notice_when_liva_is_down(monkeypatch):
    stub = StubStreamlit()
    monkeypatch.setattr(views_liva, "st", stub)

    class Down:
        def __init__(self, *a, **k):
            pass

        def health(self):
            raise LivaUnavailable("down")

    monkeypatch.setattr(views_liva, "Liva", Down)
    views_liva.render(api=None)  # must not raise
    assert any(kind == "info" and "unavailable" in text.lower() for kind, text in stub.shown)


def test_render_explains_a_missing_groq_key(monkeypatch):
    stub = StubStreamlit()
    monkeypatch.setattr(views_liva, "st", stub)

    class NoKey:
        def __init__(self, *a, **k):
            pass

        def health(self):
            return {"ok": True, "groq_configured": False}

    monkeypatch.setattr(views_liva, "Liva", NoKey)
    views_liva.render(api=None)
    assert any("GROQ_API_KEY" in text for _, text in stub.shown)


def test_the_liva_page_is_registered():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text()
    assert '"liva": ("Liva", views_liva.render)' in source
    assert "import views_liva" in source


def test_a_stream_that_dies_midway_is_liva_unavailable_not_a_traceback(monkeypatch):
    class Dying(FakeResponse):
        def iter_lines(self, decode_unicode=True):
            yield json.dumps({"type": "message", "content": "partial"})
            raise requests.exceptions.ChunkedEncodingError("cut")

    patch_request(monkeypatch, Dying())
    stream = Liva("t", "a").send("c1", "hi", False)
    assert next(stream)["type"] == "message"
    with pytest.raises(LivaUnavailable):
        next(stream)


def test_a_garbled_stream_line_is_liva_unavailable(monkeypatch):
    patch_request(monkeypatch, FakeResponse(lines=["{not json"]))
    with pytest.raises(LivaUnavailable):
        list(Liva("t", "a").send("c1", "hi", False))

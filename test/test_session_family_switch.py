"""Fork: backends in one session family keep a chat's session across a move."""

import pytest

from kiro_crew.agent_sdk import backends
from kiro_crew.session import detect_provider_switch


class _Map:
    def __init__(self, provider, sid):
        self._provider, self._sid = provider, sid

    def get_provider(self, key):
        return self._provider

    def get(self, key):
        return self._sid


@pytest.fixture(autouse=True)
def _families():
    backends.register_session_family("Claude Seat 1", "claude-code")
    backends.register_session_family("Claude Seat 3", "claude-code")
    yield
    backends._REGISTERED_SESSION_FAMILY.clear()


def test_a_move_inside_one_family_is_not_a_switch():
    assert detect_provider_switch(_Map("Claude Seat 1", "sid"), "k", "Claude Seat 3") is False


def test_a_move_to_a_backend_outside_the_family_is_a_switch():
    assert detect_provider_switch(_Map("Claude Seat 1", "sid"), "k", "Antigravity (Gemini)") is True


def test_backends_with_no_family_still_switch():
    assert detect_provider_switch(_Map("Seat A", "sid"), "k", "Seat B") is True

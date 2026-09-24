"""Shared test setup."""

import pytest

from apple_mail_mcp import envelope_index


@pytest.fixture(autouse=True)
def _no_envelope_index(monkeypatch):
    """Tools use their AppleScript path unless a test builds an Envelope Index.

    Without this the tests would read the Mail database of the machine they
    run on. tests/test_envelope_index.py removes the variable again.
    """
    monkeypatch.setenv("APPLE_MAIL_MCP_NO_INDEX", "1")
    envelope_index.reset()
    yield
    envelope_index.reset()

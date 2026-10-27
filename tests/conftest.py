import pytest

from polledge import i18n


@pytest.fixture(autouse=True)
def english_messages():
    """Tests assert on English messages; restore English after tests that switch language."""
    i18n.set_language("en")
    yield
    i18n.set_language("en")

from types import SimpleNamespace

import pytest

from polledge.ai.vision import transcribe_odds
from polledge.config import AiSettings
from polledge.errors import PollEdgeError
from polledge.odds.paste import parse_pasted_odds

TRANSCRIPT = "Republikáni většina v Senátu i ve Sněmovně reprezentantů\nAno 10.00\nNe 1.02\n"


class FakeAnthropic:
    def __init__(self):
        self.messages = self
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=TRANSCRIPT)])


class FakeOpenAI:
    def __init__(self):
        self.responses = self
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(status="completed", output_text=TRANSCRIPT)


@pytest.mark.parametrize("provider, client", [("anthropic", FakeAnthropic()), ("openai", FakeOpenAI())])
def test_transcription_sends_the_image_and_feeds_the_paste_parser(provider, client):
    text = transcribe_odds(provider, AiSettings(), b"\x89PNG fake", "image/png", client=client)
    content = client.kwargs["messages" if provider == "anthropic" else "input"][0]["content"]
    if provider == "anthropic":
        assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    else:
        assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/png;base64,")
    assert [(s.name, s.odds) for s in parse_pasted_odds(text)] == [("Ano", 10.0), ("Ne", 1.02)]


def test_transcription_rejects_huge_images():
    with pytest.raises(PollEdgeError):
        transcribe_odds("openai", AiSettings(), b"x" * (6 * 1024 * 1024), "image/png", client=FakeOpenAI())

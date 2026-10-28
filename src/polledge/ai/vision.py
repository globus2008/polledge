"""Screenshot of a bookmaker page -> plain text with odds, for the paste import (OpenAI or Claude).

The model only transcribes; the text then goes through the same parser and preview as copied page
text, so nothing is stored before the user checks it.
"""

from __future__ import annotations

import base64

from polledge.ai.review import KEY_ENV
from polledge.errors import PollEdgeError
from polledge.i18n import _

PROMPT = """This is a screenshot of a bookmaker's page with betting odds. Transcribe the odds as plain text:

- For every market: its heading on its own line, exactly as shown.
- Then one line per selection: the selection name, a space and the decimal odds, e.g. "Ano 1.36" or "Petr Pavel 1.33".
- Over/under lines keep their sign as shown: "-47.5 2.35" and "+47.5 1.50".
- Leave out selections without odds (locked or not offered), dates, times, buttons and other page text.
- Write numbers with a decimal point. Output only the transcription - no commentary, no Markdown, no tables."""

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
               "gif": "image/gif"}


def transcribe_odds(provider: str, ai_settings, image: bytes, media_type: str, client=None) -> str:
    """Odds from a screenshot as text in the paste-import format."""
    import os

    if provider not in KEY_ENV:
        raise PollEdgeError(_("Unknown AI provider '{provider}' (use anthropic or openai)", provider=provider))
    if client is None and not os.environ.get(KEY_ENV[provider]):
        raise PollEdgeError(_("Set {key} in the app secrets to use the AI review.", key=KEY_ENV[provider]))
    if len(image) > MAX_IMAGE_BYTES:
        raise PollEdgeError(_("The image is too large (max 5 MB) - crop the screenshot to the odds."))
    data = base64.standard_b64encode(image).decode("ascii")
    if provider == "anthropic":
        return _anthropic(ai_settings.anthropic_model, data, media_type, client)
    return _openai(ai_settings.openai_model, data, media_type, client)


def _anthropic(model: str, data: str, media_type: str, client) -> str:
    import anthropic

    client = client or anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": PROMPT},
            ]}],
        )
    except anthropic.AuthenticationError as exc:
        raise PollEdgeError(_("The AI API key is invalid (ANTHROPIC_API_KEY).")) from exc
    except anthropic.RateLimitError as exc:
        raise PollEdgeError(_("The AI service is rate limited - try again in a minute.")) from exc
    except anthropic.APIStatusError as exc:
        raise PollEdgeError(_("The AI service returned an error: {error}", error=exc.message)) from exc
    except anthropic.APIConnectionError as exc:
        raise PollEdgeError(_("Cannot reach the AI service: {error}", error=exc)) from exc
    if response.stop_reason == "refusal":
        raise PollEdgeError(_("The AI declined to read the image."))
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _openai(model: str, data: str, media_type: str, client) -> str:
    import openai

    client = client or openai.OpenAI()
    try:
        response = client.responses.create(
            model=model,
            input=[{"role": "user", "content": [
                {"type": "input_text", "text": PROMPT},
                {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"},
            ]}],
            reasoning={"effort": "low"},
            max_output_tokens=8000,
        )
    except openai.AuthenticationError as exc:
        raise PollEdgeError(_("The AI API key is invalid (OPENAI_API_KEY).")) from exc
    except openai.RateLimitError as exc:
        raise PollEdgeError(_("The AI service is rate limited - try again in a minute.")) from exc
    except openai.APIStatusError as exc:
        raise PollEdgeError(_("The AI service returned an error: {error}", error=exc.message)) from exc
    except openai.APIConnectionError as exc:
        raise PollEdgeError(_("Cannot reach the AI service: {error}", error=exc)) from exc
    if getattr(response, "status", "completed") == "incomplete":
        raise PollEdgeError(_("The AI answer was cut off - try again."))
    return response.output_text.strip()

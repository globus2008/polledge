"""AI review of a value signal: a second opinion on the model probability and the Kelly stake.

The reviewer gets everything PollEdge knows about the signal (model, market, assumptions, polls),
searches the web for current news and returns a structured verdict with a stake multiplier.
Two interchangeable providers: Anthropic (Claude) and OpenAI. The review is an opinion, not a
guarantee - the application never places bets.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from polledge.errors import PollEdgeError
from polledge.i18n import _

log = logging.getLogger(__name__)

VERDICTS = ("bet", "reduce", "skip")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "stake_multiplier": {"type": "number"},
        "probability_estimate": {"type": "number"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "stake_multiplier", "probability_estimate", "confidence", "summary", "reasons",
                 "risks", "sources"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are a sceptical risk analyst reviewing a sports-betting style value signal on an election market.
A poll-based model (or, in "market" mode, a reference prediction market) produced a probability; a
licensed bookmaker offers odds; quarter-Kelly gives a recommended stake. Your job is a second opinion
on whether that stake is justified.

How to work:
- Search the web for current, relevant facts: candidacies and eligibility, legal rulings, alliances,
  recent polls, campaign events, and the bookmaker's settlement rules if they matter. Prefer recent,
  reputable sources and note their dates.
- Compare the model probability with the market probabilities you are given (a deep, liquid market
  disagreeing strongly with the model is evidence that the model misses something). Look for
  structural gaps: who is assumed to stand, scenario weights, runoff assumptions, time to the election,
  how many polls exist.
- Give your own probability estimate for the outcome.
- Set stake_multiplier between 0 and 1, applied to the recommended quarter-Kelly stake:
  1 = the stake is justified as computed, 0 = do not bet. Choose "bet" (multiplier >= 0.75),
  "reduce" (0 < multiplier < 0.75) or "skip" (multiplier = 0). A bet only makes sense if your
  probability estimate times the odds clearly exceeds 1; be conservative when evidence is thin.
- Be concrete and brief. Write summary, reasons and risks in Czech (the user reads Czech).
  List the sources you relied on (title and URL).
This is analysis only; never encourage betting more than the recommended stake."""


@dataclass
class ReviewResult:
    verdict: str
    stake_multiplier: float
    probability_estimate: float
    confidence: str
    summary: str
    reasons: list[str]
    risks: list[str]
    sources: list[dict]
    provider: str
    model: str
    usage: dict = field(default_factory=dict)

    @classmethod
    def from_json(cls, text: str, provider: str, model: str, usage: dict | None = None) -> "ReviewResult":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PollEdgeError(_("The AI returned an unreadable answer: {error}", error=exc)) from exc
        verdict = data.get("verdict") if data.get("verdict") in VERDICTS else "skip"
        multiplier = min(1.0, max(0.0, float(data.get("stake_multiplier") or 0.0)))
        if verdict == "skip":
            multiplier = 0.0
        return cls(
            verdict=verdict,
            stake_multiplier=multiplier,
            probability_estimate=min(1.0, max(0.0, float(data.get("probability_estimate") or 0.0))),
            confidence=data.get("confidence", "low"),
            summary=str(data.get("summary", "")),
            reasons=[str(r) for r in data.get("reasons", [])],
            risks=[str(r) for r in data.get("risks", [])],
            sources=[s for s in data.get("sources", []) if isinstance(s, dict) and s.get("url")],
            provider=provider,
            model=model,
            usage=usage or {},
        )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("verdict", "stake_multiplier", "probability_estimate", "confidence",
                                              "summary", "reasons", "risks", "sources", "provider", "model", "usage")}


def user_prompt(context: dict) -> str:
    return ("Review this signal. All numbers PollEdge has are below (probabilities as fractions, "
            "stakes in the user's currency).\n\n" + json.dumps(context, ensure_ascii=False, indent=2, default=str))


class Reviewer(ABC):
    provider: str

    def __init__(self, model: str, max_searches: int = 5):
        self.model = model
        self.max_searches = max_searches

    @abstractmethod
    def review(self, context: dict) -> ReviewResult:
        """Return the structured review of one signal."""


class AnthropicReviewer(Reviewer):
    """Claude via the Anthropic SDK: web search server tool + JSON-schema output, adaptive thinking."""

    provider = "anthropic"
    MAX_CONTINUATIONS = 4

    def __init__(self, model: str, max_searches: int = 5, client=None):
        super().__init__(model, max_searches)
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def review(self, context: dict) -> ReviewResult:
        import anthropic

        messages: list[dict] = [{"role": "user", "content": user_prompt(context)}]
        try:
            for _attempt in range(self.MAX_CONTINUATIONS + 1):
                with self.client.beta.messages.stream(
                    model=self.model,
                    max_tokens=32000,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high", "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
                    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": self.max_searches}],
                    # A safety decline is retried server-side on the recommended fallback model.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                ) as stream:
                    response = stream.get_final_message()
                if response.stop_reason != "pause_turn":
                    break
                # The server-side search loop paused: send the turn back unchanged and it resumes.
                messages = [messages[0], {"role": "assistant", "content": response.content}]
        except anthropic.AuthenticationError as exc:
            raise PollEdgeError(_("The AI API key is invalid (ANTHROPIC_API_KEY).")) from exc
        except anthropic.RateLimitError as exc:
            raise PollEdgeError(_("The AI service is rate limited - try again in a minute.")) from exc
        except anthropic.APIStatusError as exc:
            raise PollEdgeError(_("The AI service returned an error: {error}", error=exc.message)) from exc
        except anthropic.APIConnectionError as exc:
            raise PollEdgeError(_("Cannot reach the AI service: {error}", error=exc)) from exc

        if response.stop_reason == "refusal":
            raise PollEdgeError(_("The AI declined to review this signal."))
        if response.stop_reason == "max_tokens":
            raise PollEdgeError(_("The AI answer was cut off - try again."))
        text = next((b.text for b in response.content if b.type == "text"), "")
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        return ReviewResult.from_json(text, self.provider, response.model, usage)


class OpenAIReviewer(Reviewer):
    """OpenAI Responses API: web_search tool + JSON-schema text format, reasoning effort high."""

    provider = "openai"

    def __init__(self, model: str, max_searches: int = 5, client=None):
        super().__init__(model, max_searches)
        if client is None:
            import openai

            client = openai.OpenAI()
        self.client = client

    def review(self, context: dict) -> ReviewResult:
        import openai

        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=SYSTEM_PROMPT,
                input=user_prompt(context),
                tools=[{"type": "web_search"}],
                reasoning={"effort": "high"},
                text={"format": {"type": "json_schema", "name": "signal_review", "schema": REVIEW_SCHEMA,
                                 "strict": True}},
                max_output_tokens=32000,
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
        usage = {}
        if getattr(response, "usage", None) is not None:
            usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        return ReviewResult.from_json(response.output_text, self.provider, response.model, usage)


KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def available_providers() -> list[str]:
    """Providers whose API key is present in the environment (Streamlit secrets are copied there)."""
    return [p for p, env in KEY_ENV.items() if os.environ.get(env)]


def make_reviewer(provider: str, settings) -> Reviewer:
    if provider not in KEY_ENV:
        raise PollEdgeError(_("Unknown AI provider '{provider}' (use anthropic or openai)", provider=provider))
    if not os.environ.get(KEY_ENV[provider]):
        raise PollEdgeError(_("Set {key} in the app secrets to use the AI review.", key=KEY_ENV[provider]))
    if provider == "anthropic":
        return AnthropicReviewer(settings.anthropic_model, settings.max_searches)
    return OpenAIReviewer(settings.openai_model, settings.max_searches)

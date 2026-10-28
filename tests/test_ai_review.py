"""AI review: request construction for both providers (fake clients, no network), parsing, storage."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from polledge.ai.review import AnthropicReviewer, OpenAIReviewer, ReviewResult, make_reviewer
from polledge.ai.service import build_context, record_reviewed_bet, reviewed_stake, reviews_for, run_review
from polledge.config import AiSettings, Settings, load_election
from polledge.db import PaperBetRow, make_session_factory
from polledge.errors import PollEdgeError
from polledge.odds.base import OddsQuote
from polledge.services import build_market_comparisons

ROOT = Path(__file__).resolve().parents[1]
ANSWER = {
    "verdict": "reduce", "stake_multiplier": 0.3, "probability_estimate": 0.55, "confidence": "medium",
    "summary": "Model je přesebevědomý.", "reasons": ["Trh je hluboký."], "risks": ["Kandidatura."],
    "sources": [{"title": "Le Monde", "url": "https://example.org/a"}],
}


class FakeAnthropic:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        stop_reason, text = self.answers.pop(0)
        message = SimpleNamespace(
            stop_reason=stop_reason, model="claude-opus-5-5",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )

        class Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get_final_message=lambda: message)

            def __exit__(self_inner, *exc):
                return False

        return Ctx()


class FakeOpenAI:
    def __init__(self, text, status="completed"):
        self.calls = []
        response = SimpleNamespace(output_text=text, model="gpt-5.5", status=status,
                                   usage=SimpleNamespace(input_tokens=900, output_tokens=150))
        self.responses = SimpleNamespace(create=lambda **kw: (self.calls.append(kw), response)[1])


@pytest.fixture
def market():
    us = load_election(ROOT / "config" / "elections" / "us_pres_2028.yaml")
    t = datetime(2026, 10, 28, 9)
    quotes = [OddsQuote(us.id, "Polymarket", "winner", "HARRIS", 1 / 0.06, t),
              OddsQuote(us.id, "Polymarket", "winner", "VANCE", 1 / 0.18, t),
              OddsQuote(us.id, "Betano", "winner", "HARRIS", 25.0, t),
              OddsQuote(us.id, "Betano", "winner", "VANCE", 5.0, t)]
    settings = Settings()
    (comp,) = build_market_comparisons(us, quotes, settings)
    outcome = next(o for o in comp.outcomes if o.outcome == "HARRIS")
    assert outcome.is_signal
    return us, comp, outcome, settings


def test_context_contains_the_signal(market):
    us, comp, outcome, settings = market
    ctx = build_context(us, comp, outcome, None, settings)
    assert ctx["signal"]["best_odds"] == 25.0 and ctx["signal"]["best_bookmaker"] == "Betano"
    assert ctx["signal"]["prediction_market_probability"] == pytest.approx(0.06)
    assert ctx["stake"]["recommended_stake"] == pytest.approx(outcome.stake, abs=0.01)
    json.dumps(ctx)  # must be serialisable for the prompt


def test_anthropic_request_and_pause_turn(market):
    us, comp, outcome, settings = market
    client = FakeAnthropic([("pause_turn", ""), ("end_turn", json.dumps(ANSWER))])
    result = AnthropicReviewer("claude-opus-5-5", 3, client=client).review(build_context(us, comp, outcome, None, settings))
    assert result.verdict == "reduce" and result.stake_multiplier == 0.3 and result.provider == "anthropic"
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["thinking"] == {"type": "adaptive"}
    assert first["tools"][0]["type"] == "web_search_20260209" and first["tools"][0]["max_uses"] == 3
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert len(client.calls) == 2 and client.calls[1]["messages"][1]["role"] == "assistant"  # resumed


def test_anthropic_refusal_is_reported(market):
    us, comp, outcome, settings = market
    reviewer = AnthropicReviewer("claude-opus-5-5", client=FakeAnthropic([("refusal", "")]))
    with pytest.raises(PollEdgeError):
        reviewer.review(build_context(us, comp, outcome, None, settings))


def test_openai_request(market):
    us, comp, outcome, settings = market
    client = FakeOpenAI(json.dumps(ANSWER))
    result = OpenAIReviewer("gpt-5.5", client=client).review(build_context(us, comp, outcome, None, settings))
    assert result.provider == "openai" and result.probability_estimate == 0.55
    call = client.calls[0]
    assert call["tools"] == [{"type": "web_search"}] and call["reasoning"] == {"effort": "high"}
    assert call["text"]["format"]["type"] == "json_schema" and call["text"]["format"]["strict"] is True
    with pytest.raises(PollEdgeError):
        OpenAIReviewer("gpt-5.5", client=FakeOpenAI("", status="incomplete")).review({})


def test_result_is_sanitised():
    bad = {**ANSWER, "verdict": "skip", "stake_multiplier": 0.9, "probability_estimate": 1.7}
    r = ReviewResult.from_json(json.dumps(bad), "openai", "gpt-5.5")
    assert r.stake_multiplier == 0.0 and r.probability_estimate == 1.0  # skip means no stake
    r = ReviewResult.from_json(json.dumps({**ANSWER, "stake_multiplier": 3}), "openai", "gpt-5.5")
    assert r.stake_multiplier == 1.0  # never more than the Kelly stake
    with pytest.raises(PollEdgeError):
        ReviewResult.from_json("not json", "openai", "gpt-5.5")


def test_review_stored_and_bet_recorded(market):
    us, comp, outcome, settings = market
    sessions = make_session_factory(":memory:")
    reviewer = OpenAIReviewer("gpt-5.5", client=FakeOpenAI(json.dumps({**ANSWER, "stake_multiplier": 0.5})))
    with sessions() as s:
        row = run_review(s, reviewer, us, comp, outcome, None, settings)
        assert reviews_for(s, us.id, "winner", "HARRIS")[0].id == row.id
        bet = record_reviewed_bet(s, row, outcome)
        assert bet.stake == reviewed_stake(row) == round(outcome.stake * 0.5)
        assert row.bet_id == bet.id and s.query(PaperBetRow).count() == 1


def test_make_reviewer_needs_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(PollEdgeError, match="OPENAI_API_KEY"):
        make_reviewer("openai", AiSettings())
    with pytest.raises(PollEdgeError):
        make_reviewer("gemini", AiSettings())

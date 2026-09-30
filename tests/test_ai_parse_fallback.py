"""Weak NLP parses get a second pass through Mealie's AI parser."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mealie.cleanup import parse_with_ai_fallback  # noqa: E402


def _res(text, food, conf):
    return {"input": text, "confidence": {"average": conf},
            "ingredient": {"food": {"name": food} if food else None}}


class Recorder:
    def __init__(self, nlp, ai=None, ai_error=None):
        self.nlp, self.ai, self.ai_error = nlp, ai, ai_error
        self.calls = []

    def __call__(self, texts, parser):
        self.calls.append((parser, list(texts)))
        if parser == "nlp":
            return [self.nlp[t] for t in texts]
        if self.ai_error:
            raise self.ai_error
        return [self.ai[t] for t in texts]


TEXTS = ["2 eggs", "salt and pepper, to taste", "1 can black beans, drained"]
NLP = {
    "2 eggs": _res("2 eggs", "egg", 0.99),
    "salt and pepper, to taste": _res("salt and pepper, to taste", None, 0.4),
    "1 can black beans, drained": _res("1 can black beans, drained", "black beans drained", 0.6),
}


def test_only_weak_lines_go_to_ai_and_replace_nlp():
    ai = {t: _res(t, f, 0.99) for t, f in [
        ("salt and pepper, to taste", "salt and pepper"),
        ("1 can black beans, drained", "black bean")]}
    rec = Recorder(NLP, ai)
    out = parse_with_ai_fallback(TEXTS, rec)
    assert rec.calls[1] == ("openai", TEXTS[1:])
    assert [r["parser"] for r in out] == ["nlp", "openai", "openai"]
    assert out[2]["ingredient"]["food"]["name"] == "black bean"


def test_ai_failure_keeps_nlp():
    out = parse_with_ai_fallback(TEXTS, Recorder(NLP, ai_error=RuntimeError("quota")))
    assert [r["parser"] for r in out] == ["nlp"] * 3
    assert out[2]["ingredient"]["food"]["name"] == "black beans drained"


def test_ai_answer_without_food_or_wrong_echo_is_rejected():
    ai = {
        "salt and pepper, to taste": _res("salt and pepper, to taste", None, 0.99),
        "1 can black beans, drained": _res("something else", "black bean", 0.99),
    }
    out = parse_with_ai_fallback(TEXTS, Recorder(NLP, ai))
    assert [r["parser"] for r in out] == ["nlp"] * 3


def test_disabled_or_all_confident_makes_no_ai_call():
    rec = Recorder(NLP)
    parse_with_ai_fallback(TEXTS, rec, enabled=False)
    rec2 = Recorder({"2 eggs": NLP["2 eggs"]})
    parse_with_ai_fallback(["2 eggs"], rec2)
    assert [c[0] for c in rec.calls + rec2.calls] == ["nlp", "nlp"]


def test_malformed_ai_entries_keep_nlp():
    ai = {
        "salt and pepper, to taste": {"input": "salt and pepper, to taste",
                                      "ingredient": {"food": "salt"}},  # food not an object
        "1 can black beans, drained": "garbage",
    }
    out = parse_with_ai_fallback(TEXTS, Recorder(NLP, ai))
    assert [r["parser"] for r in out] == ["nlp"] * 3

"""Provider cascade for the Jev (System One) decision layer.

Every request goes to ONE provider; the cascade falls through on any error and returns the first
success. Every answer records the `provider` that served it and the `model` the provider echoed
(which tells you which version actually answered — Jev aliases drift weekly).

Channels (in default order):
  1. typesafe  — official API, `TYPESAFE_API_KEY`
  2. cloudflare— Workers AI free tier, `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`
  3. openrouter— `OPENROUTER_API_KEY` (needs prepaid credits, same $0.042/M)
  4. heuristic — last-ditch local `noul` only (low confidence, never used for actions)
"""
from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

from .questions import Question

log = logging.getLogger("jev")

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
CLOUDFLARE_RUN_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
CLOUDFLARE_MODEL = "typesafe/jev"
OPENROUTER_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODEL = "typesafe/jev-1.13"

load_dotenv()  # project-local .env (keys never committed; canonical store = api-providers.md)

# Shared keep-alive transport. 2026-09-22 (jev-ultrafast lesson): per-call httpx.post() rebuilt
# DNS + TCP + TLS on every decision (~150-400 ms of pure overhead per step) and starved the
# machine of sockets. One pooled client (HTTP/2 when the `h2` package is present, clean fallback
# otherwise) is reused by ALL providers AND the optional text-helper for the whole process.
_TRANSPORT_TIMEOUT = 30.0
try:
    _CLIENT = httpx.Client(http2=True, timeout=_TRANSPORT_TIMEOUT)
except Exception:
    _CLIENT = httpx.Client(timeout=_TRANSPORT_TIMEOUT)


def session() -> httpx.Client:
    """Return the shared pooled client (decision providers + text-helper)."""
    return _CLIENT


class ProviderError(Exception):
    """A single channel failed (transient or auth/input). Cascade should move on."""


class ProviderUnavailable(ProviderError):
    """Channel cannot be used at all (missing key/account)."""


class NoProviderAvailable(ProviderError):
    """Every configured channel failed."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("No Jev provider available:\n  - " + "\n  - ".join(errors or ["none configured"]))


@dataclass
class Answer:
    key: str
    type: str
    raw: dict[str, Any]
    provider: str
    model: str | None = None
    choice: str | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    noul: float | None = None
    score: float | None = None
    legend: dict[str, str] | None = None

    @classmethod
    def from_raw(cls, key: str, type_: str, raw: dict[str, Any], provider: str, model: str | None = None) -> Answer:
        a = cls(key=key, type=type_, raw=raw, provider=provider, model=model)
        if type_ == "choice":
            a.choice = raw.get("choice")
            a.probabilities = raw.get("probabilities")
            a.confidence = raw.get("confidence")
        elif type_ == "noul":
            a.noul = raw.get("noul")
        elif type_ == "score":
            a.score = raw.get("score")
            a.legend = raw.get("legend")
            a.probabilities = raw.get("probabilities")
            a.confidence = raw.get("confidence")
        return a

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "provider": self.provider, "model": self.model}
        if self.choice is not None:
            d["choice"] = self.choice
        if self.probabilities is not None:
            d["probabilities"] = self.probabilities
        if self.confidence is not None:
            d["confidence"] = self.confidence
        if self.noul is not None:
            d["noul"] = self.noul
        if self.score is not None:
            d["score"] = self.score
        if self.legend is not None:
            d["legend"] = self.legend
        return d


@dataclass
class DecisionResponse:
    answers: list[Answer]
    provider: str
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None

    def by_key(self) -> dict[str, Answer]:
        return {a.key: a for a in self.answers}

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "answers": {a.key: a.to_dict() for a in self.answers},
        }


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _retrying_post(
    url: str, *, json: dict[str, Any], api_key: str, timeout: float = 30.0, retries: int = 2
) -> httpx.Response:
    headers = _headers(api_key)
    attempt = 0
    while True:
        r = _CLIENT.post(url, json=json, headers=headers, timeout=timeout)
        if r.status_code in (429, 529) and attempt < retries:
            attempt += 1
            log.warning("Jev provider overloaded (%s) — retry %d", r.status_code, attempt)
            time.sleep(0.4 * (2 ** attempt))
            continue
        return r


class BaseProvider:
    name = "base"

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        raise NotImplementedError


class TypeSafeProvider(BaseProvider):
    name = "typesafe"

    def __init__(self, api_key: str | None = None, model: str = "jev-latest", base_url: str = TYPESAFE_URL) -> None:
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "").strip()
        self.default_model = model
        self.base_url = base_url

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        if not self.api_key:
            raise ProviderUnavailable("typesafe: TYPESAFE_API_KEY not set")
        payload = {
            "model": model or self.default_model,
            "state": state,
            "questions": {q.key: q.to_api() for q in questions},
        }
        r = _retrying_post(self.base_url, json=payload, api_key=self.api_key)
        if r.status_code == 401:
            raise ProviderError("typesafe: 401 Unauthorized (bad key)")
        if r.status_code == 422:
            raise ProviderError(f"typesafe: 422 body rejected: {r.text[:200]}")
        if r.status_code >= 400:
            raise ProviderError(f"typesafe: HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return _normalize(data, self.name)


class CloudflareProvider(BaseProvider):
    name = "cloudflare"

    def __init__(
        self, account_id: str | None = None, api_token: str | None = None, model: str = CLOUDFLARE_MODEL
    ) -> None:
        self.account_id = account_id or os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        self.api_token = api_token or os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.account_id and self.api_token)

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        if not self.account_id or not self.api_token:
            raise ProviderUnavailable("cloudflare: CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN not set")
        model_name = model or self.model
        input_payload = {
            "state": state,
            "questions": {q.key: q.to_api() for q in questions},
        }
        url = CLOUDFLARE_RUN_URL.format(account_id=self.account_id)
        r = _CLIENT.post(url, json={"model": model_name, "input": input_payload},
                         headers=_headers(self.api_token), timeout=60.0)
        if r.status_code in (404, 400, 405):  # some accounts require model in the path
            fallback = f"{url}/{model_name}"
            r = _CLIENT.post(fallback, json=input_payload, headers=_headers(self.api_token), timeout=60.0)
        if r.status_code >= 400:
            raise ProviderError(f"cloudflare: HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        if not data.get("success"):
            errs = data.get("errors") or [{"message": "unknown error"}]
            raise ProviderError(f"cloudflare: {errs}")
        return _normalize(data.get("result") or data, self.name)


class OpenRouterProvider(BaseProvider):
    name = "openrouter"

    def __init__(self, api_key: str | None = None, model: str = OPENROUTER_MODEL) -> None:
        keys = os.getenv("OPENROUTER_API_KEY", "") if api_key is None else api_key
        self.api_key = (keys.split(",")[0] if "," in keys else keys).strip()
        self.model = model

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        if not self.api_key:
            raise ProviderUnavailable("openrouter: OPENROUTER_API_KEY not set")
        payload = {
            "model": model or self.model,
            "state": state,
            "questions": {q.key: q.to_api() for q in questions},
            "provider": {"allow_fallbacks": True},
        }
        r = _CLIENT.post(OPENROUTER_URL, json=payload, headers=_headers(self.api_key), timeout=60.0)
        if r.status_code == 402:
            raise ProviderError("openrouter: 402 Payment Required (needs prepaid credits)")
        if r.status_code >= 400:
            raise ProviderError(f"openrouter: HTTP {r.status_code}: {r.text[:300]}")
        return _normalize(r.json(), self.name)


class LocalHeuristicProvider(BaseProvider):
    """Last-ditch local fallback.

    Only `noul` is attempted, via character-bigram overlap between the state text and the
    criteria['true']/['false'] descriptions. It is deliberately low-confidence and NEVER used
    for actions or decisions that drive anything. Choice/Score raise to force human/LLM escalation.
    """

    name = "heuristic"

    @property
    def available(self) -> bool:
        return True

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        state_text = state if isinstance(state, str) else _json_text(state)
        answers: list[Answer] = []
        for q in questions:
            if q.type == "noul":
                noul_val = self._guess_noul(q, state_text)
                raw = {"type": "noul", "noul": noul_val}
            else:
                raise ProviderUnavailable(
                    f"heuristic provider only answers noul questions (got {q.type!r} for {q.key!r}) "
                    "— escalate to a cloud provider or a human."
                )
            answers.append(Answer.from_raw(q.key, q.type, raw, self.name, model="heuristic-v1"))
        return DecisionResponse(answers=answers, provider=self.name, model="heuristic-v1")

    @staticmethod
    def _guess_noul(q: Question, state_text: str) -> float:
        true_desc = ""
        false_desc = ""
        if isinstance(q.criteria, dict):
            true_desc = str(q.criteria.get("true", ""))
            false_desc = str(q.criteria.get("false", ""))
        if not true_desc and not false_desc:
            return 0.5
        sim_true = _bigram_similarity(state_text, true_desc)
        sim_false = _bigram_similarity(state_text, false_desc)
        if abs(sim_true - sim_false) < 1e-9:
            return 0.5
        p = sim_true / (sim_true + sim_false)
        return max(0.0, min(1.0, p))


def _bigram_similarity(a: str, b: str) -> float:
    a_l, b_l = a.lower(), b.lower()
    a_bg = {a_l[i:i + 2] for i in range(len(a_l) - 1)}
    b_bg = {b_l[i:i + 2] for i in range(len(b_l) - 1)}
    if not a_bg or not b_bg:
        return 0.0
    return len(a_bg & b_bg) / math.sqrt(len(a_bg) * len(b_bg))


def _json_text(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _normalize(data: dict[str, Any], provider: str) -> DecisionResponse:
    answers: list[Answer] = []
    model = data.get("model")
    for key, raw in (data.get("answers") or {}).items():
        type_ = raw.get("type", "")
        # OpenRouter//some gateways may omit `type`; infer from fields
        if not type_:
            if "choice" in raw:
                type_ = "choice"
            elif "noul" in raw:
                type_ = "noul"
            elif "score" in raw:
                type_ = "score"
        answers.append(Answer.from_raw(key, type_, raw, provider, model=model))
    usage = data.get("usage") or {}
    return DecisionResponse(answers=answers, provider=provider, model=model, usage=usage)


def _as_number(value: Any, label: str) -> float:
    """Coerce to a finite float or raise a ProviderError (bool is rejected on purpose)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderError(f"malformed {label}: {value!r}")
    return float(value)


def _validate_response(resp: DecisionResponse, questions: list[Question]) -> None:
    """Structural validation of a provider's answers (#7, jev-ultrafast lesson).

    Deliberately TOLERANT so the cascade keeps working: an answer that is present must have the
    right SHAPE (choice within the offered options, numbers inside range, probabilities sane),
    but a gateway may omit a question entirely — decide_plan defaults those. A malformed answer
    raises ProviderError so DecisionLayer fails over to the next channel instead of acting on
    garbage (a hallucinated element id or an out-of-range score is worse than a retry).
    """
    if not questions:
        return
    by_key = resp.by_key()
    for q in questions:
        ans = by_key.get(q.key)
        if ans is None:
            continue  # tolerated omission (gateway batching); decide_plan applies its default
        if q.type == "choice":
            keys = list(q.criteria.keys()) if isinstance(q.criteria, dict) else [q.criteria]
            if ans.choice is not None and ans.choice not in keys:
                raise ProviderError(f"answer {q.key!r} chose unmatched option {ans.choice!r} (offered {keys})")
            _validate_probabilities(ans, keys, q.key)
        if q.type == "score" and ans.score is not None and isinstance(q.criteria, list):
            s = _as_number(ans.score, f"{q.key}.score")
            if not 0 <= s <= len(q.criteria) - 1:
                raise ProviderError(f"answer {q.key!r} score {s} outside 0..{len(q.criteria) - 1}")
        if q.type == "noul" and ans.noul is not None:
            n = _as_number(ans.noul, f"{q.key}.noul")
            if not 0.0 <= n <= 1.0:
                raise ProviderError(f"answer {q.key!r} noul {n} outside 0..1")
        if ans.confidence is not None:
            c = _as_number(ans.confidence, f"{q.key}.confidence")
            if not 0.0 <= c <= 1.0:
                raise ProviderError(f"answer {q.key!r} confidence {c} outside 0..1")


def _validate_probabilities(ans: Answer, keys: list[str], q_key: str) -> None:
    probs = ans.probabilities
    if not isinstance(probs, dict) or not probs:
        return
    for k, v in probs.items():
        _as_number(v, f"{q_key}.probabilities[{k!r}]")
    covered = set(probs)
    if covered == set(keys):
        total = sum(float(p) for p in probs.values())
        if not 0.95 <= total <= 1.05:
            raise ProviderError(f"answer {q_key!r} probabilities sum {total:.3f} != 1.0")
        if ans.choice is not None and ans.choice != max(probs, key=probs.get):
            raise ProviderError(f"answer {q_key!r} choice {ans.choice!r} != argmax {max(probs, key=probs.get)!r}")


class DecisionLayer:
    """Fails over across providers per call; returns the first full success."""

    def __init__(self, providers: Iterable[BaseProvider] | None = None) -> None:
        self.providers: list[BaseProvider] = list(providers) if providers is not None else self._default_providers()

    @staticmethod
    def _default_providers() -> list[BaseProvider]:
        return [
            TypeSafeProvider(),
            CloudflareProvider(),
            OpenRouterProvider(),
            LocalHeuristicProvider(),
        ]

    def status(self) -> list[dict[str, Any]]:
        out = []
        for p in self.providers:
            out.append({
                "provider": p.name,
                "configured": bool(getattr(p, "available", True)),
                "default_model": getattr(p, "model", None) or getattr(p, "default_model", None),
            })
        return out

    def ask(self, state: Any, questions: list[Question], model: str | None = None) -> DecisionResponse:
        errors: list[str] = []
        for provider in self.providers:
            if not getattr(provider, "available", True):
                errors.append(f"{provider.name}: not configured (skipped)")
                continue
            try:
                started = time.perf_counter()
                resp = provider.ask(state, questions, model=model)
                _validate_response(resp, list(questions))
                resp.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                log.info("Jev decision served by %s (%s) in %sms",
                         provider.name, resp.model, resp.latency_ms)
                return resp
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                log.warning("Jev provider %s failed: %s", provider.name, exc)
        raise NoProviderAvailable(errors)
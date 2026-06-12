"""Vietnamese financial sentiment classification.

Combines a PhoBERT-based sentiment model with a small Vietnamese financial
lexicon that nudges the raw model output for domain-specific phrases (e.g.
"bán ròng" / net foreign selling should skew negative even when the model
itself is ambiguous), plus a lightweight stock-ticker extractor.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable

logger = logging.getLogger("nlp_pipeline.classifier")

# Model used for the base sentiment signal. Override via env for experiments
# (e.g. "5CD-AI/vietnamese-sentiment-visobert").
DEFAULT_MODEL_NAME = os.environ.get(
    "SENTIMENT_MODEL_NAME", "wonrax/phobert-base-vietnamese-sentiment"
)

POSITIVE = "positive"
NEGATIVE = "negative"
NEUTRAL = "neutral"

# Maps raw model label strings (seen across common Vietnamese sentiment
# checkpoints) to our normalized labels and the sign applied to the model's
# confidence score.
_LABEL_NORMALIZATION = {
    "pos": (POSITIVE, 1.0),
    "positive": (POSITIVE, 1.0),
    "label_2": (POSITIVE, 1.0),
    "neg": (NEGATIVE, -1.0),
    "negative": (NEGATIVE, -1.0),
    "label_0": (NEGATIVE, -1.0),
    "neu": (NEUTRAL, 0.0),
    "neutral": (NEUTRAL, 0.0),
    "label_1": (NEUTRAL, 0.0),
}

# Vietnamese financial lexicon: phrase (lowercase, diacritics included) ->
# sentiment "force" in [-1.0, 1.0] applied on top of the model score. Phrases
# are matched as case-insensitive substrings against the input text.
FINANCIAL_LEXICON: dict[str, float] = {
    # Strong negative — distress / sell-offs
    "phá sản": -0.9,
    "vỡ nợ": -0.85,
    "bán tháo": -0.75,
    "bán ròng": -0.6,
    "giảm sàn": -0.8,
    "lao dốc": -0.7,
    "thua lỗ": -0.7,
    "nợ xấu": -0.5,
    "thanh lý bắt buộc": -0.7,
    "cảnh báo": -0.3,
    "kiểm soát đặc biệt": -0.6,
    # Strong positive — growth / buy-side momentum
    "lợi nhuận kỷ lục": 0.85,
    "tăng trần": 0.8,
    "mua ròng": 0.6,
    "vượt đỉnh": 0.6,
    "tăng trưởng mạnh": 0.6,
    "lãi lớn": 0.6,
    "lãi đậm": 0.6,
    "kết quả kinh doanh khả quan": 0.5,
    "vượt kế hoạch": 0.5,
    "cổ tức cao": 0.4,
}

# Default basket of recognized tickers + special index aliases. Extend via
# the TICKER_DICT env var (comma-separated) to cover more symbols without a
# code change.
DEFAULT_TICKERS = ["FPT", "VCB", "HPG", "VNM", "VIC", "MWG", "MSN", "VHM", "TCB", "GAS", "CTG", "BID"]

_INDEX_PATTERN = re.compile(r"\bVN[\s-]?INDEX\b|\bVN30\b", re.IGNORECASE)


def _tickers_from_env() -> list[str]:
    raw = os.environ.get("TICKER_DICT", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    return tickers or list(DEFAULT_TICKERS)


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class TickerExtractor:
    """Finds stock tickers (and the VN-Index) mentioned in a piece of text."""

    def __init__(self, tickers: Iterable[str] | None = None):
        self.tickers = sorted({t.upper() for t in (tickers or _tickers_from_env())}, key=len, reverse=True)
        pattern = r"\b(" + "|".join(re.escape(t) for t in self.tickers) + r")\b"
        self._ticker_re = re.compile(pattern)

    def extract(self, text: str) -> list[str]:
        """Returns the sorted, de-duplicated tickers (and "INDEX") mentioned in ``text``."""
        upper = text.upper()
        found = set(self._ticker_re.findall(upper))
        if _INDEX_PATTERN.search(upper):
            found.add("INDEX")
        return sorted(found)


class SentimentClassifier:
    """PhoBERT sentiment model + Vietnamese financial lexicon overrides.

    The Hugging Face pipeline is loaded lazily on first use so importing this
    module (e.g. for unit tests of the lexicon/ticker logic) doesn't require
    the model weights to be downloaded.
    """

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or DEFAULT_MODEL_NAME
        self._pipeline = None

    def _load_pipeline(self):
        if self._pipeline is None:
            from transformers import pipeline

            logger.info(f"Loading sentiment model '{self.model_name}'...")
            self._pipeline = pipeline(
                "sentiment-analysis", model=self.model_name, tokenizer=self.model_name
            )
        return self._pipeline

    def _model_score(self, text: str) -> tuple[float, str]:
        """Runs the PhoBERT pipeline and returns (score in [-1, 1], normalized label)."""
        pipe = self._load_pipeline()
        # PhoBERT has a 256-subword limit; truncate generously on characters first.
        result = pipe(text[:1000], truncation=True)[0]
        raw_label = str(result["label"]).strip().lower()
        confidence = float(result["score"])
        label, sign = _LABEL_NORMALIZATION.get(raw_label, (NEUTRAL, 0.0))
        return clamp(confidence * sign), label

    def _lexicon_force(self, text: str) -> float:
        """Sums lexicon "forces" for every financial phrase found in ``text``."""
        lowered = text.lower()
        total = 0.0
        for phrase, force in FINANCIAL_LEXICON.items():
            if phrase in lowered:
                total += force
        return clamp(total)

    @staticmethod
    def _label_for_score(score: float) -> str:
        if score >= 0.15:
            return POSITIVE
        if score <= -0.15:
            return NEGATIVE
        return NEUTRAL

    def classify(self, text: str) -> dict:
        """Classifies ``text``, returning ``{"score", "label", "intensity"}``.

        ``score`` is in [-1.0, 1.0] (bearish -> bullish), ``label`` is one of
        positive/negative/neutral, and ``intensity`` (0..1) reflects how
        strong the signal is, taking both model confidence and lexicon
        matches into account.
        """
        if not text or not text.strip():
            return {"score": 0.0, "label": NEUTRAL, "intensity": 0.0}

        model_score, _model_label = self._model_score(text)
        lexicon_force = self._lexicon_force(text)

        # Lexicon matches are domain knowledge we trust more than the generic
        # model for financial phrasing, so they're additive but dominate when
        # both signals agree, and pull the score toward themselves when they
        # disagree with the model.
        combined = clamp(model_score + lexicon_force)
        label = self._label_for_score(combined)
        intensity = clamp(max(abs(model_score), abs(lexicon_force)), 0.0, 1.0)
        return {"score": combined, "label": label, "intensity": intensity}

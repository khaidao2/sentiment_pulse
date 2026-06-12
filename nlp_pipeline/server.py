"""FastAPI service exposing the sentiment classifier for ad-hoc/inline use.

Run with: uvicorn nlp_pipeline.server:app
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from nlp_pipeline.classifier import SentimentClassifier, TickerExtractor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nlp_pipeline.server")

app = FastAPI(title="Sentiment Pulse NLP Service")

_classifier = SentimentClassifier()
_ticker_extractor = TickerExtractor()


class AnalyzeRequest(BaseModel):
    text: str


class AnalyzeResponse(BaseModel):
    score: float
    label: str
    intensity: float
    tickers: list[str]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    result = _classifier.classify(request.text)
    tickers = _ticker_extractor.extract(request.text)
    return AnalyzeResponse(
        score=result["score"],
        label=result["label"],
        intensity=result["intensity"],
        tickers=tickers,
    )

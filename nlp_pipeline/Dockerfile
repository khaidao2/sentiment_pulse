# Image for the NLP service: FastAPI sentiment server and the streaming
# news/community -> sentiment-scores worker. Build from the repo root:
#   docker build -f nlp_pipeline/Dockerfile -t sentiment-pulse-nlp .
FROM python:3.12-slim

WORKDIR /app

# Runtime deps: classifier (transformers/torch) + generated producer
# (kafka-python, fastavro) + API server (fastapi/uvicorn).
COPY nlp_pipeline/requirements.txt /tmp/nlp_requirements.txt
COPY api_crawler/sentiment_scores/requirements.txt /tmp/sentiment_scores_requirements.txt
RUN pip install --no-cache-dir -r /tmp/nlp_requirements.txt -r /tmp/sentiment_scores_requirements.txt

# Application code.
COPY nlp_pipeline/ /app/nlp_pipeline/
COPY api_crawler/sentiment_scores/ /app/api_crawler/sentiment_scores/

# Default: run the FastAPI server.
# Override for the streaming worker: python -m nlp_pipeline.worker
CMD ["uvicorn", "nlp_pipeline.server:app", "--host", "0.0.0.0", "--port", "8080"]

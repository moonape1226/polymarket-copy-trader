FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3 websockets>=13.0

COPY src/__init__.py ./src/__init__.py
COPY src/ws_feed.py ./src/ws_feed.py
COPY weather_predictor/ ./weather_predictor/

CMD ["python", "-m", "weather_predictor.predictor"]

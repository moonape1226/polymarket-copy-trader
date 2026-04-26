FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3

COPY scanner.py .
COPY analyze_copy_decisions.py .

CMD ["python", "scanner.py"]

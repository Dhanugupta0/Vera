FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    VERA_STATE_PATH=/tmp/vera_state.json

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY vera ./vera

EXPOSE 8080

# One worker, deliberately. All context and conversation state is in-process;
# a second worker would answer half the judge's calls from an empty store.
CMD ["sh", "-c", "exec uvicorn vera.app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --timeout-keep-alive 65"]

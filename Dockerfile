FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY pytest.ini ./
COPY samples ./samples
# The CRM simulator is needed by `demo-chaos` and the chaos test, which start
# a private instance of it.
COPY crm-sim/server.py crm-sim/calendar.json ./crm-sim/

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 callvault \
    && mkdir /keys && chown callvault:callvault /keys && chmod 700 /keys
USER callvault

CMD ["python", "-m", "app.cli", "worker"]

# Stage 1: Builder
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y curl \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Stage 2: Runtime
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY . .

# Session state is a SQLite file. On App Service only /home survives a restart or a
# redeploy (with WEBSITES_ENABLE_APP_SERVICE_STORAGE=true), so the default lives
# there rather than in the image's own filesystem. Override EF_DB_PATH to move it.
ENV EF_DB_PATH=/home/data/ef_chat.db

# main.py reads PORT; App Service also needs WEBSITES_PORT set to the same value.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "chatbot/main.py"]

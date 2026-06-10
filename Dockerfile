FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
COPY app ./app
COPY mcp_servers ./mcp_servers
COPY demo_service ./demo_service
COPY static ./static
COPY aiops-docs ./aiops-docs

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9900"]

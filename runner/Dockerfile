FROM --platform=linux/amd64 node:22.16.0-bookworm-slim AS node
FROM --platform=linux/amd64 python:3.12.10-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global --registry=https://registry.npmjs.org @openai/codex@0.157.1 \
    && npm cache clean --force \
    && codex --version \
    && useradd --uid 10001 --create-home runner
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py ./main.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8081/health')); raise SystemExit(d['status'] != 'ready')"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log"]

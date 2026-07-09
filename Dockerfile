FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY mneme/ ./mneme/
RUN pip install --no-cache-dir .

# 컨테이너 기본값 — compose/실행 시 env로 덮어쓴다
ENV WIKI_DIR=/wiki \
    DB_PATH=/data/state.db \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

VOLUME ["/data", "/wiki"]
EXPOSE 8080

CMD ["python", "-m", "mneme.server"]

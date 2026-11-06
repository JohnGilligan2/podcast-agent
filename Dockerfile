# ---- build stage: install dependencies into a virtualenv ----------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml ./
COPY src ./src
ARG EXTRAS=dev
RUN pip install ".[${EXTRAS}]"

# ---- runtime stage --------------------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PATH="/opt/venv/bin:$PATH" PODCAST_AGENT_HOME=/app
WORKDIR /app
RUN useradd --create-home --uid 1000 agent
COPY --from=build /opt/venv /opt/venv
COPY src ./src
COPY data ./data
COPY tests ./tests
COPY pyproject.toml ./
RUN mkdir -p /app/output && chown -R agent:agent /app
USER agent

# PODCAST_AGENT_HOME=/app tells the package where data/ and output/ live.
ENTRYPOINT ["python", "-m", "podcast_agent"]
CMD ["run"]

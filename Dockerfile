# ---- build stage: install dependencies into a virtualenv ----------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml ./
COPY src ./src
ARG EXTRAS=dev,gui
RUN pip install ".[${EXTRAS}]"

# ---- runtime stage --------------------------------------------------------------------
FROM python:3.12-slim
ARG BUILD_SHA=""
ENV PODCAST_AGENT_BUILD=${BUILD_SHA}
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 PATH="/opt/venv/bin:$PATH" PODCAST_AGENT_HOME=/app
WORKDIR /app
RUN useradd --create-home --uid 1000 agent
COPY --from=build /opt/venv /opt/venv
COPY src ./src
COPY data ./data
COPY tests ./tests
COPY examples/sample_run/all_reports.json ./examples/sample_run/all_reports.json
COPY pyproject.toml ./
RUN mkdir -p /app/output /app/var && chown -R agent:agent /app
USER agent

# PODCAST_AGENT_HOME=/app tells the package where data/, output/ and var/ (results store) live.
EXPOSE 8501
ENTRYPOINT ["python", "-m", "podcast_agent"]
CMD ["run"]

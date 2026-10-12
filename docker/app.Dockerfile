# Image shared by satin-cma-orchestrator, satin-cc-orchestrator (+ reconcile sidecar) and satin-slack-bot.
FROM python:3.12-slim-bookworm

ARG CLAUDE_VERSION=2.1.296

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --home-dir /home/satin --shell /bin/bash satin \
 && mkdir -p /opt/satin/hooks /data /etc/satin \
 && chown 1000:1000 /data

COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src

# (B) orchestrator hook: asks Kubernetes for a Sandbox and returns without waiting.
COPY docker/hooks/spawn-runner /opt/satin/hooks/spawn-runner

USER satin
ENV HOME=/home/satin PATH=/home/satin/.local/bin:$PATH
# Pinned native `claude` binary (needed for `claude self-hosted-runner orchestrator`).
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
 && claude --version

ENTRYPOINT ["satin"]

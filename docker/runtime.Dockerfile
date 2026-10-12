# Sandbox runtime image shared by (A) Managed Agents and (B) Claude Code.
# Use cases extend this with `FROM <this image>` and add tools / /opt/satin/startup.sh.
FROM python:3.12-slim-bookworm

ARG CLAUDE_VERSION=2.1.296
ARG IMAGE_ID=dev

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 1000 --create-home --home-dir /home/satin --shell /bin/bash satin \
 && mkdir -p /workspace /opt/satin /run/satin \
 && chown 1000:1000 /workspace \
 && echo "${IMAGE_ID}" > /etc/satin-image-id

COPY pyproject.toml README.md /src/
COPY src /src/src
RUN pip install --no-cache-dir /src && rm -rf /src

COPY docker/startup.sample.sh /opt/satin/startup.sh

USER satin
ENV HOME=/home/satin PATH=/home/satin/.local/bin:$PATH
# Pinned native `claude` binary (needed by mode cc).
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_VERSION}" \
 && claude --version

WORKDIR /workspace
ENTRYPOINT ["satin-sandbox"]

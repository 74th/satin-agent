FROM python:3.12-slim-bookworm
RUN useradd --uid 1000 --create-home satin
COPY pyproject.toml /src/
COPY echo_mcp /src/echo_mcp
RUN pip install --no-cache-dir /src && rm -rf /src
USER 1000
EXPOSE 8080
ENTRYPOINT ["python", "-m", "echo_mcp.server"]

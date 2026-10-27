# PollEdge monitoring container (e.g. Synology Container Manager on a home network).
# It only makes outgoing connections (Wikipedia, bookmakers, the database) - no ports, no public IP.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POLLEDGE_LANG=cs

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .
COPY config ./config

RUN useradd --create-home --uid 1000 polledge && mkdir -p /app/logs && chown polledge /app/logs
USER polledge

# The database URL comes from the environment (POLLEDGE_DATABASE_URL), never from the image.
CMD ["polledge", "watch", "--every", "3600"]

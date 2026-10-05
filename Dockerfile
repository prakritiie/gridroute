# One image for every Python service (gateway, workers, sim); the compose `command` picks the role.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY core ./core
COPY data/__init__.py data/download.py ./data/
COPY gateway ./gateway
COPY workers ./workers
COPY sim ./sim
COPY ml ./ml
COPY eval ./eval
COPY tools ./tools
COPY bench ./bench
RUN pip install ".[bench]"

CMD ["python", "-m", "tools.audit"]

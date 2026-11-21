# syntax=docker/dockerfile:1
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app

ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir /app/source && tar -xzf /tmp/sqlglot.tar.gz --strip-components=1 -C /app/source
COPY requirements.txt /app/
RUN python -m pip install --disable-pip-version-check --no-cache-dir --prefix=/install -r requirements.txt

RUN python -m compileall -q /app/source/sqlglot /app/source/tests

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 HOME=/tmp
WORKDIR /app
COPY --from=build /install/ /usr/local/
COPY --from=build /app/source/ /app/source/
COPY coverage.json.gz /app/
COPY selector.py patches.py probe.py comparisons.py operands.py replay_cache.py mapflow.py declarations.py dispatch.py branches.py /app/
USER 65534:65534
ENTRYPOINT ["python", "/app/selector.py"]

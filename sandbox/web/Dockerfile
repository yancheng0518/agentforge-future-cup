ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0:0
COPY requirements.lock /dependencies/requirements.lock
COPY wheels/ /dependencies/wheels/
RUN python -I -B -m pip --isolated --disable-pip-version-check --no-cache-dir install --no-index --no-deps --only-binary=:all: --require-hashes --no-compile --find-links=/dependencies/wheels -r /dependencies/requirements.lock
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /tmp

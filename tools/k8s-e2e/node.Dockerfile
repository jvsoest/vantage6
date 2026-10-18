# Node image with the vantage6 sources of this repository, for end-to-end testing.
# Build from the root of the repository:
#   docker build -f tools/k8s-e2e/node.Dockerfile -t vantage6-node:e2e .
ARG BASE=ghcr.io/vantage6/infrastructure/node:4.15.2
FROM ${BASE}

RUN pip install --no-cache-dir kubernetes==31.0.0

# the packages are installed in editable mode, so replacing the sources suffices
COPY vantage6-common /vantage6/vantage6-common
COPY vantage6-client /vantage6/vantage6-client
COPY vantage6-algorithm-tools /vantage6/vantage6-algorithm-tools
COPY vantage6 /vantage6/vantage6
COPY vantage6-node /vantage6/vantage6-node

RUN mkdir -p /mnt/log /mnt/data /mnt/config \
  && chgrp -R 0 /mnt \
  && chmod -R g=u /mnt

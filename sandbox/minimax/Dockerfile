# AI Meeting Room Phase 1.3 — fixed MiniMax Code image definition.
# NODE_BASE_DIGEST is intentionally required at build time; an unpinned tag is
# not an acceptable production image. The digest is UNKNOWN until a real,
# reviewed build is performed on the target Docker/Colima runtime.
ARG NODE_BASE_IMAGE=node:22.22.3-bookworm-slim
ARG NODE_BASE_DIGEST=sha256:111d09056e51bb52d1bfca06a3e73476d6022b156dc4c36c5379503cd307660b
FROM ${NODE_BASE_IMAGE}@${NODE_BASE_DIGEST}

ARG MINIMAX_CODE_VERSION=0.4.2
RUN test "${MINIMAX_CODE_VERSION}" = "0.4.2" \
    && npm install --global --omit=dev "@minimax-ai/code@${MINIMAX_CODE_VERSION}" \
    && useradd --create-home --home-dir /home/mcode --shell /bin/sh mcode \
    && mkdir -p /workspace /home/mcode/.minimax /run \
    && chown -R mcode:mcode /workspace /home/mcode /run

USER mcode
ENV HOME=/home/mcode \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    MINIMAX_CODE_VERSION=0.4.2
WORKDIR /workspace
ENTRYPOINT ["sleep", "infinity"]

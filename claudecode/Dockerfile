FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.222

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils \
        build-essential \
        ca-certificates \
        coreutils \
        curl \
        file \
        gdb \
        git \
        jq \
        less \
        procps \
        python3 \
        python3-pip \
        ripgrep \
    && npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace /logs /home/node/.claude \
    && chown -R node:node /workspace /logs /home/node/.claude

# Keep benchmark behavior reproducible and avoid optional telemetry/update traffic.
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/node \
    CLAUDE_CONFIG_DIR=/home/node/.claude

USER node
WORKDIR /workspace

# Runs Claude Code with the working copy of the plugin, apart from any Claude Code on the host
FROM node:22-bookworm-slim

# Keep equal to the version CI validates with (a test checks it)
ARG CLAUDE_CODE_VERSION=2.1.305

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 socat \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Create the mount points as the session user, so the named volumes start writable
RUN mkdir -p /home/node/.claude /workspace \
    && chown -R node:node /home/node/.claude /workspace

COPY --chmod=0755 entrypoint.sh /usr/local/bin/emet-dev-entrypoint

USER node
ENV CLAUDE_CONFIG_DIR=/home/node/.claude \
    DISABLE_AUTOUPDATER=1
WORKDIR /workspace

ENTRYPOINT ["emet-dev-entrypoint"]

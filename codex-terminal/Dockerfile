ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install system packages and development tools
RUN apk add --no-cache \
    bash \
    curl \
    nano \
    nodejs \
    npm \
    python3 \
    py3-pip \
    git \
    vim \
    wget \
    jq \
    tree \
    tmux \
    ttyd \
    yq

# Install Codex CLI via npm
RUN npm install -g @openai/codex@latest \
    && npm cache clean --force

# Create directory for Codex configuration and set working directory
RUN mkdir -p /config/codex-config
WORKDIR /config

# Copy startup script
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Use exec form for better signal handling
CMD ["/run.sh"]

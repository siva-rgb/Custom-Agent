# The Claude arm's image (FR-179, DECISION-d0269633): the Python claude_agent_sdk and the pinned
# Claude Code CLI, on the same base as this SDK's workspace image, so both arms have the same
# shell, Python and tools. Built locally by the owner, or with the owner's approval:
#
#   docker build -t adr12-claude:cli-2.1.267 evals/adr12
#
# Nothing in a gate pulls or builds it, and it holds no credential: the gateway's key reaches only
# a container's environment, when the comparison runs one.
FROM python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

# bash: Claude Code's Bash tool runs its commands in bash, which Alpine does not ship (M50 live
# run 1, where every Claude-arm command failed without it). This SDK's arm needs no shell.
RUN apk add --no-cache nodejs npm bash \
 && adduser -D -u 1000 agent \
 && mkdir /work \
 && chown agent:agent /work

WORKDIR /opt/adr12
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
RUN pip install --no-cache-dir claude-agent-sdk==0.2.152
COPY claude_driver.py ./driver.py

USER agent
WORKDIR /work
ENTRYPOINT ["python", "/opt/adr12/driver.py"]

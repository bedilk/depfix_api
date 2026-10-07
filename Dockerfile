# =============================================================================
#  depfix — application container
#  Multi-stage build: install deps in a builder, then copy to a slim runtime.
# =============================================================================

ARG PYTHON_VERSION=3.11
ARG NODE_VERSION=20.19.2

# ---- builder ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System deps only needed at build time. curl/xz-utils are for fetching the
# Node.js tarball below, not for the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md requirements.txt ./
COPY src ./src

RUN pip install --prefix=/install -r requirements.txt \
 && pip install --prefix=/install --no-deps .

# Node.js + npm are needed at runtime: npm installs a target repo's
# dependencies and runs its test suite for real as part of `depfix fix`'s
# verification step (`depfix.verify.manager`/`runner`). Debian's `nodejs`
# apt package doesn't bundle npm, and Debian's separate `npm` package pulls
# in an unrelated tree of ~300 JS library packages (webpack, babel, jest, ...)
# as hard dependencies -- so install the upstream binary tarball instead,
# which contains just node + npm + npx.
ARG NODE_VERSION
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) NODE_ARCH=x64 ;; \
         arm64) NODE_ARCH=arm64 ;; \
         *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSLo /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" \
    && mkdir -p /opt/node \
    && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 \
    && rm /tmp/node.tar.xz

# ---- runtime ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:${PATH}" \
    PYTHONPATH="/install/lib/python3.11/site-packages"

# git is used by the shallow-clone service and repo scanner
# (`depfix scan --repo`) to fetch and inspect commits.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd --create-home --uid 1001 depfix
WORKDIR /app

COPY --from=builder /opt/node /usr/local
COPY --from=builder /install /install
COPY --from=builder /app/src /app/src

USER depfix

# Default: show help. Override with `docker run ... depfix --codebase /work`
ENTRYPOINT ["depfix"]
CMD ["--help"]

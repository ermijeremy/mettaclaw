# syntax=docker/dockerfile:1

# ==========================================
# Stage 1: Build Environment
# Heavy tools stay here only
# ==========================================
FROM docker.io/library/swipl:latest AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      build-essential \
      python3 \
      python3-pip \
      python3-dev \
      ca-certificates \
      pkg-config \
      cmake \
      libopenblas-dev \
      libblas-dev \
      liblapack-dev \
      gfortran \
      libgflags-dev \
 && rm -rf /var/lib/apt/lists/*

# Build FAISS (static)
RUN git clone --depth 1 https://github.com/facebookresearch/faiss.git /faiss
WORKDIR /faiss
RUN cmake -B build \
      -DFAISS_ENABLE_GPU=OFF \
      -DFAISS_ENABLE_PYTHON=OFF \
      -DBUILD_SHARED_LIBS=OFF \
 && cmake --build build --config Release --parallel 2 \
 && cmake --install build

# Build PeTTa
RUN git clone --depth 1 https://github.com/trueagi-io/PeTTa.git /PeTTa
WORKDIR /PeTTa
RUN sh build.sh

# Build Python wheels here so final image does not need compilers
WORKDIR /tmp/wheels
RUN pip3 wheel --no-cache-dir --wheel-dir /tmp/wheels \
      janus-swi \
      openai \
      aiogram \
      requests \
      websocket-client \
      PyYAML \
      chromadb


# ==========================================
# Stage 2: Production Runtime
# Lean and non-root
# ==========================================
FROM docker.io/library/swipl:latest AS final

# Only runtime packages
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      ca-certificates \
      tini \
      libopenblas0-pthread \
      libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Fixed UID/GID for predictable host volume permissions
RUN groupadd --system --gid 10001 mettagroup \
 && useradd --system --uid 10001 --gid 10001 \
      --home-dir /app \
      --create-home \
      --shell /usr/sbin/nologin \
      mettauser

WORKDIR /app

# Copy artifacts from build stage
COPY --from=build /PeTTa /app/PeTTa
COPY --from=build /usr/local/lib/libfaiss.a /usr/local/lib/
COPY --from=build /tmp/wheels /tmp/wheels

# Install Python dependencies from local wheels only
RUN pip3 install --no-cache-dir --break-system-packages /tmp/wheels/* \
 && rm -rf /tmp/wheels

# Copy app source
COPY . /app/mettaclaw

# Link MeTTaClaw into PeTTa repo layout
RUN mkdir -p /app/PeTTa/repos \
 && ln -s /app/mettaclaw /app/PeTTa/repos/mettaclaw \
 && cp /app/mettaclaw/run.metta /app/PeTTa/run.metta

# Create only the directories that should be writable at runtime
RUN mkdir -p \
      /app/data \
      /app/mettaclaw/memory \
      /app/PeTTa/chroma_db \
      /app/PeTTa/logs

# Lock down code and allow writes only where needed
RUN chown -R root:root /app \
 && chmod -R a=rX,u+w /app \
 && chown -R 10001:10001 \
      /app/data \
      /app/mettaclaw/memory \
      /app/PeTTa/chroma_db \
      /app/PeTTa/logs \
 && chmod 700 \
      /app/data \
      /app/mettaclaw/memory \
      /app/PeTTa/chroma_db \
      /app/PeTTa/logs

# Runtime env
ENV PYTHONPATH=/app/mettaclaw:/app/mettaclaw/src:/app/mettaclaw/channels
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app/PeTTa

# Optional healthcheck placeholder
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os; assert os.path.isdir('/app/mettaclaw')" || exit 1

# Minimal init process
ENTRYPOINT ["/usr/bin/tini", "--"]

# Run permanently as non-root
USER 10001:10001

CMD ["sh", "run.sh", "run.metta", "default"]
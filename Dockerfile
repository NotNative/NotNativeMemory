FROM python:3.12-slim

WORKDIR /app

# Install CPU-only PyTorch first to avoid pulling ~2GB of NVIDIA CUDA
# libraries. The model runs on CPU - GPU libs would sit on disk unused.
# Then install remaining deps with transformers pinned <5.0 (the model's
# custom rope embedding code is incompatible with transformers 5.x).
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY server.py .
COPY lib/ lib/
COPY config/ config/
COPY scripts/ scripts/

# Embedding model is mounted as a volume at runtime, not baked in.
# Default MEMORY_MODEL_PATH=models/gte-base-en-v1.5 resolves to
# /app/models/gte-base-en-v1.5 via lib/embeddings.py path resolution.

EXPOSE 9500

# Run in foreground mode (attached to container PID 1).
# PID file and background spawning logic in server.py are irrelevant
# inside Docker - the container runtime handles lifecycle.
CMD ["python", "server.py", "--foreground"]

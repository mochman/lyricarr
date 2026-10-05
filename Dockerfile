FROM python:3.12-slim

LABEL org.opencontainers.image.source=https://github.com/mochman/lyricarr
LABEL org.opencontainers.image.description="Lyricarr CPU Image"
LABEL org.opencontainers.image.licenses=MIT

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch 

RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torchaudio

WORKDIR /app
COPY pyproject.toml README.md ./
COPY lyricarr ./lyricarr
RUN pip install --no-cache-dir .

ENV TORCH_HOME=/cache/torch \
    HF_HOME=/cache/hf \
    LYRICARR_LIBRARY=/music \
    LYRICARR_DEVICE=cpu \
    LYRICARR_WORK=/tmp/lyricarr
VOLUME ["/music", "/cache"]

ENTRYPOINT ["lyricarr"]

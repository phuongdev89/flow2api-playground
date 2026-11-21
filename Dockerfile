FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-lama.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ffmpeg cho crop viền WM trên video; WITH_WATERMARK_TOOLS=1 cài thêm LaMa+torch (~1GB).
ARG WITH_WATERMARK_TOOLS=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$WITH_WATERMARK_TOOLS" = "1" ]; then pip install --no-cache-dir -r requirements-lama.txt; fi

COPY app ./app
COPY static ./static
COPY main.py ./

ENV PLAYGROUND_HOST=0.0.0.0 \
    PLAYGROUND_PORT=8000

CMD ["python", "main.py"]

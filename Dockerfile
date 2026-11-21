FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt requirements-lama.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ffmpeg cho crop viền WM trên video; WITH_WATERMARK_TOOLS=1 cài thêm LaMa+torch.
# Torch lấy bản CPU-only từ index riêng của PyTorch — wheel mặc định trên PyPI
# kèm CUDA/NVIDIA libs (~6-9GB) và là nguyên nhân làm full disk root fs.
ARG WITH_WATERMARK_TOOLS=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$WITH_WATERMARK_TOOLS" = "1" ]; then \
         pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
         && pip install --no-cache-dir simple-lama-inpainting; \
       fi

COPY app ./app
COPY static ./static
COPY main.py ./

ENV PLAYGROUND_HOST=0.0.0.0 \
    PLAYGROUND_PORT=8000

CMD ["python", "main.py"]

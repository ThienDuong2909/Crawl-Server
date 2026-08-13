FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOWNLOAD_DIR=/app/download DOUYIN_COOKIE_FILE=/app/secrets/douyin_cookie.txt PORT=8000
WORKDIR /app
RUN groupadd -r app && useradd -r -g app app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md COOKIE_AUTO_REFRESH.md ./
COPY douyin_nwm_tool ./douyin_nwm_tool
COPY crawlers ./crawlers
RUN pip install --no-cache-dir -e .
RUN mkdir -p /app/download /app/secrets && chown -R app:app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "douyin_nwm_tool.main:app", "--host", "0.0.0.0", "--port", "8000"]

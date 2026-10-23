FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY run.py ./
COPY metasync_node ./metasync_node
ENV METASYNC_STATE_DIR=/data METASYNC_PORT=8790
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=4)"
CMD ["python3", "run.py", "serve"]

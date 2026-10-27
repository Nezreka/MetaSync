FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    RELAY_HOST=0.0.0.0 \
    RELAY_PORT=8791

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY metasync_relay/ ./metasync_relay/

RUN useradd --uid 10001 --no-create-home relay
USER relay

EXPOSE 8791

CMD ["python", "-m", "metasync_relay"]

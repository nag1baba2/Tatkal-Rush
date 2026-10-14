FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /mnt

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The locustfile and the run data are mounted at run time (see docker-compose.yml).
ENTRYPOINT ["locust"]

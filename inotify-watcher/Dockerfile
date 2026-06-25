FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY watcher.py .

ENV CONFIG_PATH=/config/config.yaml

CMD ["python", "-u", "watcher.py"]

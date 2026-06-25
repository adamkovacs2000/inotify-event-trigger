FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY receiver.py .

ENV CONFIG_PATH=/config/config.yaml
ENV PORT=8080

EXPOSE 8080

CMD ["python", "-u", "receiver.py"]

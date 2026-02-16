FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tracker.py .

# Create an empty db file so the container doesn't crash on first run
RUN echo "{}" > tracker_db.json

CMD ["python", "tracker.py"]

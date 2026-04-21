FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5001

EXPOSE $PORT

CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 4 --timeout 120

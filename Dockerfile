FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . ./

EXPOSE 5000

CMD ["gunicorn", "--workers", "1", "--threads", "1", "--bind", "0.0.0.0:5000", "wsgi:app"]

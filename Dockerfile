FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

ENV DJANGO_DEBUG=0 \
    DJANGO_SECRET_KEY=docker-build-secret \
    DJANGO_ALLOWED_HOSTS=localhost

RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["gunicorn", "vodlab.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]

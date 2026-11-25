FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai.py bot.py cards.py coaching.py config.py db.py embeds.py patch_notes.py riot_api.py stats.py ./
COPY assets ./assets

RUN mkdir -p /data

CMD ["python", "bot.py"]

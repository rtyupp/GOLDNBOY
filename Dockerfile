FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MPLBACKEND=Agg
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# defaults are copied into /app/data only if missing (so a mounted persistent disk is not overwritten)
RUN mkdir -p /app/defaults && cp data/events.csv /app/defaults/events.csv && chmod +x scripts/entrypoint.sh
ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["python", "-m", "goldbot", "run"]

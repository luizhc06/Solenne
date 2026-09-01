FROM python:3.12-slim
WORKDIR /app
# ffmpeg fica de fora do python:3.12-slim - /paragif e /extrairaudio (cogs/videotools.py)
# rodam ele via subprocess, entao precisa instalar antes de qualquer coisa em Python.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY cogs/ ./cogs/
CMD ["python", "bot.py"]

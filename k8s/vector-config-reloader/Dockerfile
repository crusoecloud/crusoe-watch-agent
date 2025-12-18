FROM python:3.11-slim

# Add app
WORKDIR /app
COPY app /app
COPY requirements.txt /app/

# Install dependencies
RUN pip install -r /app/requirements.txt

# Unbuffer Python so logs flush to stdout immediately
ENV PYTHONUNBUFFERED=1

# Run script by default
CMD ["python", "-u", "/app/vector_config_reloader_app.py"]

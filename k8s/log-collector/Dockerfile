FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Create logs directory
RUN mkdir -p /logs

# Run as non-root user for security
RUN useradd -m -u 1000 logcollector && \
    chown -R logcollector:logcollector /app /logs

USER logcollector

# Run the application
CMD ["python3", "log_collector_app.py"]
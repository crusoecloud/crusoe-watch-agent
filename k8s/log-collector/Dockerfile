FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ .

# Create logs directory
RUN mkdir -p /logs && \
    chmod -R 755 /logs

# Run the application
CMD ["python3", "log_collector_app.py"]
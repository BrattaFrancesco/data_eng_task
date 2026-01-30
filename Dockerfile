# Use official Python image
FROM python:3.12.2-slim

# Set working directory inside container
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the code
COPY src/ ./src/
COPY log/ ./log/
COPY artifacts/ ./artifacts/

# Default command: run your pipeline
CMD ["python", "src/feature_builder.py"]
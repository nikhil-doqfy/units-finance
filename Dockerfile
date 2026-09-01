FROM python:3.12-slim
ENV PYTHONUNBUFFERED 1

# No PDF generation in Finance yet -- no WeasyPrint system deps needed (unlike
# units-backend's docker_config/python_config/Dockerfile).
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

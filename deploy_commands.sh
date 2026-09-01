#!/bin/bash

# Apply database migrations
echo "Apply database migrations"
python3 manage.py migrate

# Start server
echo "Starting server"
gunicorn finance_service.wsgi:application --bind 0.0.0.0:8001 --timeout 120 --workers 3

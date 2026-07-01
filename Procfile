web: python manage.py migrate --noinput && gunicorn neatbackend.wsgi:application --worker-class gthread --workers 2 --threads 4 --timeout 120 --log-file -

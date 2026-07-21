web: python manage.py migrate --noinput && gunicorn neatbackend.asgi:application -k uvicorn_worker.UvicornWorker --workers 2 --timeout 120 --log-file -

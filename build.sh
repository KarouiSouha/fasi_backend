#!/usr/bin/env bash
set -o errexit

pip install -r requirements/base.txt
pip install -r requirements/production.txt

python manage.py collectstatic --noinput
python manage.py migrate

# Create the superuser if is not exist
python manage.py shell -c "
from django.contrib.auth import get_user_model
U = get_user_model()
import os
email = os.environ.get('DJANGO_SUPERUSER_EMAIL')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
if email and password and not U.objects.filter(email=email).exists():
    U.objects.create_superuser(email=email, password=password)
    print('Superuser créé :', email)
else:
    print('Superuser déjà existant')
" || true

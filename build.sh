#!/usr/bin/env bash
set -o errexit

pip install -r requirements/base.txt
pip install -r requirements/production.txt

python manage.py collectstatic --noinput
python manage.py migrate
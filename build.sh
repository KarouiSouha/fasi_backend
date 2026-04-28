#!/usr/bin/env bash
export PYTHON_VERSION=3.12.0
set -o errexit
pip install -r requirements/base.txt
pip install -r requirements/production.txt
python manage.py collectstatic --noinput
python manage.py migrate


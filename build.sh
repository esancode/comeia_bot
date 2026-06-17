#!/usr/bin/env bash
# Saia do script se ocorrer algum erro
set -o errexit

pip install -r requirements.txt
playwright install chromium

#!/usr/bin/env bash
# Saia do script se ocorrer algum erro
set -o errexit

export PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright
pip install -r requirements.txt
playwright install chromium

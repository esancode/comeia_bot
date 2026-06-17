#!/usr/bin/env bash
set -o errexit

export PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright
pip install -r requirements.txt
playwright install chromium

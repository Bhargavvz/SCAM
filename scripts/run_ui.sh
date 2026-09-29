#!/usr/bin/env bash
# Start the web demo on http://localhost:8501
cd "$(dirname "$0")/.."
exec .venv/bin/python -m streamlit run interface/app.py --server.headless true "$@"

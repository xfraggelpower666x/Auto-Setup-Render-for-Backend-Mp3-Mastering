#!/usr/bin/env bash

echo "FORCE PYTHON 3.11 ENV"

pip install --upgrade pip

pip install fastapi==0.110.0
pip install uvicorn[standard]==0.29.0
pip install pydantic==2.6.4
pip install pydantic-core==2.16.3
pip install python-multipart==0.0.9

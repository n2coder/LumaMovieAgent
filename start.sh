#!/bin/bash
export PYTHONPATH="C:/Users/Administrator/AppData/Roaming/Python/Python311/site-packages:$PYTHONPATH"
exec "C:/Program Files/Python311/python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload

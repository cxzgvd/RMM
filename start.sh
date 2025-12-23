#!/bin/bash
export APP_MASTER_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")
python3 server.py

# app_main.py
# -*- coding: utf-8 -*-
"""Compatibility launcher for the AI glasses backend."""

from aiglasses.app_main import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)

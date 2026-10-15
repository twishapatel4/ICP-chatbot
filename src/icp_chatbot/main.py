"""ASGI entry point: uvicorn icp_chatbot.main:app"""

from icp_chatbot.api.app import create_app

app = create_app()

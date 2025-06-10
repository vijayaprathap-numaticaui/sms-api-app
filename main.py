import logging
import os

import requests
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
VOTERLINK_URL = os.getenv(
    "BACKEND_URL", "https://backend-voterlink-hxe8d7axbhdugxen.westus2-01.azurewebsites.net"
)

VOTERLINK_URL_V1 = 'http://74.179.100.214'
BRAND = os.getenv("BRAND", "fullerton-mayor")
BRAND_V1 = os.getenv("BRAND", "fredjung")
CONVERSATIONS = {}

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/sms")
async def message(request: Request, From: str = Form(...), Body: str = Form(...)):
    """Send a dynamic reply to an incoming text message"""

    out_response = MessagingResponse()

    # Get response from AI
    conv_history = []
    payload = {
        "question": Body,
        "user_id": From,
        "conversation_history": conv_history,
        "channel": "sms",
    }
    try:
        ai_response = requests.post(f"{VOTERLINK_URL}/chat/{BRAND}", json=payload)
        ai_response_data = ai_response.json()
        out_msg = ai_response_data["response"]
    except Exception as exc:
        logger.error(f"Error in getting response from AI backend: {str(exc)}")
        out_msg = "Sorry, I am unable to process your request at the moment."

    out_response.message(out_msg)
    return Response(content=str(out_response), media_type="application/xml")


@app.post("/sms/v1")
async def message(request: Request, From: str = Form(...), Body: str = Form(...)):
    """Send a dynamic reply to an incoming text message"""

    out_response = MessagingResponse()

    # Get response from AI
    conv_history = []
    payload = {
        "question": Body,
        "user_id": From,
        "conversation_history": conv_history,
        "channel": "sms",
    }
    try:
        ai_response = requests.post(f"{VOTERLINK_URL_V1}/chat/{BRAND_V1}", json=payload)
        ai_response_data = ai_response.json()
        out_msg = ai_response_data["response"]
    except Exception as exc:
        logger.error(f"Error in getting response from AI backend: {str(exc)}")
        out_msg = "Sorry, I am unable to process your request at the moment."

    out_response.message(out_msg)
    return Response(content=str(out_response), media_type="application/xml")

@app.get("/")
async def root():
    return {"message": "hello world"}
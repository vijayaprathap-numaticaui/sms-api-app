import logging
import os
import re
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE")
TWILIO_ENABLED = os.getenv("TWILIO_ENABLED", "false").lower() == "true"
DIRECTUS_URL = os.getenv("DIRECTUS_URL")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN")

VOTERLINK_URL = os.getenv(
    "BACKEND_URL", "https://backend-voterlink-hxe8d7axbhdugxen.westus2-01.azurewebsites.net"
)

VOTERLINK_URL_V1 = 'http://74.179.100.214'
BRAND = os.getenv("BRAND", "fullerton-mayor")
BRAND_V1 = os.getenv("BRAND", "fredjung")
CONVERSATIONS = {}

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SMSRequest(BaseModel):
    to: str
    message: str
    campaignId: str
    contactId: str

def format_for_sms(html: str) -> str:
    text = re.sub(r"<p>(?:&nbsp;|\s| )*</p>", "\n", html)
    text = re.sub(r"<p[^>]*>", "", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def update_delivered_status(campaign_id: str, contact_id: str):
    try:
        params = {
            "filter": f"""{{"sms_campaign_id":{{"_eq":"{campaign_id}"}}, "sms_contact_contact_id":{{"_eq":"{contact_id}"}}}}""",
            "limit": "1"
        }

        get_res = requests.get(
            f"{DIRECTUS_URL}/items/sms_campaign_sms_contact",
            params=params,
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )

        item = get_res.json().get("data", [{}])[0]
        item_id = item.get("id")

        if not item_id:
            raise Exception("No matching record found in Directus")

        delivered_at_utc = datetime.now(timezone.utc).isoformat()

        patch_res = requests.patch(
            f"{DIRECTUS_URL}/items/sms_campaign_sms_contact/{item_id}",
            json={"delivered_at": delivered_at_utc},
            headers={
                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                "Content-Type": "application/json",
            },
        )

        if not patch_res.ok:
            raise Exception(f"Directus update failed: {patch_res.text}")

        logger.info(f"Delivered_at updated in UTC: {delivered_at_utc}")

    except Exception as e:
        logger.error(f"Error updating Directus delivered status: {e}")


@app.get("/")
async def root():
    return {"message": "hello world"}

@app.post("/send-sms")
async def send_sms(sms_data: SMSRequest):
    """Send SMS using Twilio and update delivery status"""

    if not TWILIO_ENABLED:
        logger.warning("Twilio is disabled. SMS not sent.")
        return {
            "success": False,
            "message": "Twilio sending is currently disabled via TWILIO_ENABLED flag."
        }

    try:
        plain_text = format_for_sms(sms_data.message)

        logger.info(f"Sending SMS:\nTo: {sms_data.to}\nFrom: {TWILIO_PHONE}\nMessage:\n{plain_text}")

        twilio_response = twilio_client.messages.create(
            body=plain_text,
            from_=TWILIO_PHONE,
            to=sms_data.to,
        )

        await update_delivered_status(sms_data.campaignId, sms_data.contactId)

        return {
            "success": True,
            "sid": twilio_response.sid,
            "formatted_message": plain_text
        }

    except Exception as e:
        logger.error(f"Twilio Send Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

import logging
import os
import re
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_ENABLED = os.getenv("TWILIO_ENABLED", "false").lower() == "true"
DIRECTUS_URL = os.getenv("DIRECTUS_URL")
DIRECTUS_ADMIN_TOKEN = os.getenv("DIRECTUS_ADMIN_TOKEN")
MONGODB_URI = os.getenv("MONGODB_CONNECTION_STRING")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# MongoDB Setup
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["voterlink"]
sms_sessions_collection = db["sms_chat_sessions"]

BRAND_CONFIG = {
    "fullerton": {
        "twilio_number": os.getenv("FULLERTON_TWILIO_PHONE"),
        "ai_url": os.getenv("FULLERTON_AI_URL"),
    },
    "fredjung": {
        "twilio_number": os.getenv("FREDJUNG_TWILIO_PHONE"),
        "ai_url": os.getenv("FREDJUNG_AI_URL"),
    },
}

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
    brand: str

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

def get_brand_collection(brand: str):
    return db[f"sms_chat_sessions_{brand.lower()}"]

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
            raise Exception("No matching Directus record")

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
        logger.info(f"Delivered_at updated: {delivered_at_utc}")
    except Exception as e:
        logger.error(f"Error updating Directus: {e}")

async def get_or_create_sms_session(user_id: str, brand: str):
    collection = get_brand_collection(brand)
    session = await collection.find_one({"user_id": user_id})
    if session:
        return str(session["_id"])
    new_session = {
        "user_id": user_id,
        "brand": brand,
        "created_at": datetime.utcnow(),
        "interactions": []
    }
    result = await collection.insert_one(new_session)
    return str(result.inserted_id)

async def store_sms_interaction(user_id: str, session_id: str, message: str, reply: str, brand: str, channel: str = "sms"):
    collection = get_brand_collection(brand)
    interaction_user = {
        "type": "user",
        "message": message,
        "timestamp": datetime.utcnow(),
        "channel": channel,
        "brand": brand
    }
    interaction_bot = {
        "type": "bot",
        "message": reply,
        "timestamp": datetime.utcnow(),
        "channel": channel,
        "brand": brand
    }
    await collection.update_one(
        {"_id": ObjectId(session_id)},
        {
            "$push": {"interactions": {"$each": [interaction_user, interaction_bot]}},
            "$set": {"last_updated": datetime.utcnow()}
        }
    )

@app.get("/")
async def root():
    return {"message": "SMS AI Chatbot Running"}

@app.post("/send-sms")
async def send_sms(sms_data: SMSRequest):
    brand_config = BRAND_CONFIG.get(sms_data.brand)
    if not brand_config:
        raise HTTPException(status_code=400, detail="Invalid brand name")

    if not TWILIO_ENABLED:
        return {"success": False, "message": "Twilio is disabled"}

    try:
        plain_text = format_for_sms(sms_data.message)
        twilio_response = twilio_client.messages.create(
            body=plain_text,
            from_=brand_config["twilio_number"],
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

@app.post("/sms/{brand}")
async def receive_sms(brand: str = Path(...), From: str = Form(...), Body: str = Form(...)):
    out_response = MessagingResponse()
    config = BRAND_CONFIG.get(brand)
    if not config:
        out_response.message("Invalid brand.")
        return Response(content=str(out_response), media_type="application/xml")

    session_id = await get_or_create_sms_session(From, brand)

    # Get response from AI
    payload = {
        "question": Body,
        "user_id": From,
        "conversation_history": [],
        "channel": "sms"
    }

    try:
        ai_response = requests.post(f"{config['ai_url']}/chat/{brand}", json=payload, verify=False)
        ai_data = ai_response.json()
        reply = ai_data.get("response", "No reply.")
        await store_sms_interaction(From, session_id, Body, reply, brand)

    except Exception as e:
        logger.error(f"AI backend error: {e}")
        reply = "Sorry, something went wrong."

    out_response.message(reply)
    return Response(content=str(out_response), media_type="application/xml")

@app.get("/messages/{phone}/{brand}")
async def get_messages(phone: str, brand: str):
    collection = get_brand_collection(brand)
    sessions = await collection.find({"user_id": phone}).sort("last_updated", -1).to_list(length=1)
    if not sessions:
        return {"success": True, "messages": []}
    return {"success": True, "messages": sessions[0].get("interactions", [])}

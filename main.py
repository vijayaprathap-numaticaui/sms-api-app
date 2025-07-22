import logging
import os
import re
import requests
import jwt
import hashlib
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response, Path, Depends, HTTPException, Header, Query
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
# NEXTAUTH_SECRET = os.getenv("NEXTAUTH_SECRET")
NEXTAUTH_SECRET = "f5ba7b348cfe199eb683c74d1b9f2f53"

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# MongoDB Setup
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client["voterlink"]
sms_sessions_collection = db["sms_chat_sessions"]

BRAND_CONFIG = {
    "fullerton-mayor": {
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

class ChatSessionResponse(BaseModel):
    sessions: List[Dict[str, Any]]
    total: int

class CampaignsResponse(BaseModel):
    campaigns: List[Dict[str, Any]]

class GroupsResponse(BaseModel):
    groups: List[Dict[str, Any]]

class ManagersResponse(BaseModel):
    managers: List[Dict[str, Any]]

class CreateManagerRequest(BaseModel):
    email: str
    password: str
    first_name: str
    last_name: str
    brand: str

def format_for_sms(html: str) -> str:
    text = re.sub(r"<p>(?:&nbsp;|\s| )*</p>", "\n", html)
    text = re.sub(r"<p[^>]*>", "", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def get_brand_collection():
    return db["sms_chat_sessions"]

import jwt
from jwt import InvalidTokenError
from fastapi import Header, HTTPException
import os
import re

def verify_jwt_token(authorization: str = Header(None)):
    """Verify JWT token from Authorization header using NEXTAUTH_SECRET"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, NEXTAUTH_SECRET, algorithms=["HS256"])
        return payload
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def normalize_phone_number(phone: str) -> str:
    """Remove spaces, dashes, and formatting characters from phone number"""
    return re.sub(r"[^\d+]", "", phone)

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
    collection = get_brand_collection()
    session = await collection.find_one({"user_id": user_id, "brand": brand})
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
    collection = get_brand_collection()
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

async def store_campaign_message(user_id: str, message: str, brand: str, campaign_id: str, contact_id: str, channel: str = "sms"):
    """Store a campaign message as type 'campaign' in MongoDB"""
    try:
        session_id = await get_or_create_sms_session(user_id, brand)
        
        campaign_interaction = {
            "type": "campaign",
            "message": message,
            "timestamp": datetime.utcnow(),
            "channel": channel,
            "brand": brand
        }
        
        collection = get_brand_collection()
        await collection.update_one(
            {"_id": ObjectId(session_id)},
            {
                "$push": {"interactions": campaign_interaction},
                "$set": {"last_updated": datetime.utcnow()}
            }
        )
        
        logger.info(f"Campaign message stored for user {user_id}, campaign {campaign_id}")
        return True
    except Exception as e:
        logger.error(f"Error storing campaign message: {e}")
        return False

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
        
        await store_campaign_message(
            user_id=sms_data.to,
            message=plain_text,
            brand=sms_data.brand,
            campaign_id=sms_data.campaignId,
            contact_id=sms_data.contactId
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

#Admin APIs
@app.get("/api/chat-sessions", response_model=ChatSessionResponse)
async def get_chat_sessions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user_data = Depends(verify_jwt_token)
):
    """Get all chat sessions with brand filtering"""
    try:
        user_brand = user_data.get("brand")
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")
        
        if not user_brand and not is_admin:
            raise HTTPException(status_code=400, detail="User brand not found")
        
        skip = (page - 1) * limit
        collection = get_brand_collection()
        
        filter_query = {
            'interactions': {
                '$elemMatch': {
                    'type': 'user'
                }
            }
        }
        
        if not is_admin and user_brand:
            filter_query['brand'] = user_brand
        
        sessions_cursor = collection.find(filter_query).sort("last_updated", -1).skip(skip).limit(limit)
        sessions = await sessions_cursor.to_list(length=limit)
        total = await collection.count_documents(filter_query)
        
        transformed_sessions = []
        for session in sessions:
            transformed_sessions.append({
                "_id": str(session["_id"]),
                "user_id": session["user_id"],
                "brand": session["brand"],
                "created_at": session["created_at"],
                "interactions": session.get("interactions", []),
                "last_updated": session["last_updated"]
            })
        
        return ChatSessionResponse(sessions=transformed_sessions, total=total)
    except Exception as e:
        logger.error(f"Error fetching chat sessions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/chat-sessions/campaign/{campaign_id}", response_model=ChatSessionResponse)
async def get_campaign_chat_sessions(
    campaign_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user_data = Depends(verify_jwt_token)
):
    """Get chat sessions for specific campaign"""
    try:
        user_brand = user_data.get("brand")
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")
        
        if not user_brand and not is_admin:
            raise HTTPException(status_code=400, detail="User brand not found")
        
        params = {
            "fields": "sms_contact_contact_id.contact_number",
            "filter": f'{{"sms_campaign_id":{{"id":{{"_eq":"{campaign_id}"}}}}}}',
        }
        response = requests.get(
            f"{DIRECTUS_URL}/items/sms_campaign_sms_contact",
            params=params,
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )
        
        if not response.ok:
            raise HTTPException(status_code=500, detail="Failed to fetch campaign contacts")
        
        campaign_contacts = response.json().get("data", [])
        
        contact_numbers = []
        for contact in campaign_contacts:
            if contact.get("sms_contact_contact_id") and contact["sms_contact_contact_id"].get("contact_number"):
                phone = normalize_phone_number(contact["sms_contact_contact_id"]["contact_number"])
                if phone:
                    contact_numbers.append(phone)
        
        if not contact_numbers:
            return ChatSessionResponse(sessions=[], total=0)
        
        skip = (page - 1) * limit
        collection = get_brand_collection()
        
        filter_query = {
            'user_id': {'$in': contact_numbers},
            'interactions': {
                '$elemMatch': {
                    'type': 'user'
                }
            }
        }
        
        if not is_admin and user_brand:
            filter_query['brand'] = user_brand
        
        sessions_cursor = collection.find(filter_query).sort("last_updated", -1).skip(skip).limit(limit)
        sessions = await sessions_cursor.to_list(length=limit)
        total = await collection.count_documents(filter_query)
        
        transformed_sessions = []
        for session in sessions:
            transformed_sessions.append({
                "_id": str(session["_id"]),
                "user_id": session["user_id"],
                "brand": session["brand"],
                "created_at": session["created_at"],
                "interactions": session.get("interactions", []),
                "last_updated": session["last_updated"]
            })
        
        return ChatSessionResponse(sessions=transformed_sessions, total=total)
    except Exception as e:
        logger.error(f"Error fetching campaign chat sessions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/chat-sessions/no-campaign", response_model=ChatSessionResponse)
async def get_no_campaign_chat_sessions(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user_data = Depends(verify_jwt_token)
):
    """Get chat sessions not associated with any campaign"""
    try:
        user_brand = user_data.get("brand")
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")

        if not user_brand and not is_admin:
            raise HTTPException(status_code=400, detail="User brand not found")

        response = requests.get(
            f"{DIRECTUS_URL}/items/sms_campaign_sms_contact",
            params={"fields": "sms_contact_contact_id.contact_number"},
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )

        campaign_phone_numbers = []
        if response.ok:
            campaign_contacts = response.json().get("data", [])
            for contact in campaign_contacts:
                if contact.get("sms_contact_contact_id") and contact["sms_contact_contact_id"].get("contact_number"):
                    phone = normalize_phone_number(contact["sms_contact_contact_id"]["contact_number"])
                    if phone:
                        campaign_phone_numbers.append(phone)

        skip = (page - 1) * limit
        collection = get_brand_collection()

        filter_query = {
            'user_id': {'$nin': campaign_phone_numbers},
            'interactions': {
                '$elemMatch': {
                    'type': 'user'
                }
            }
        }

        if not is_admin and user_brand:
            filter_query['brand'] = user_brand

        sessions_cursor = collection.find(filter_query).sort("last_updated", -1).skip(skip).limit(limit)
        sessions = await sessions_cursor.to_list(length=limit)
        total = await collection.count_documents(filter_query)

        transformed_sessions = []
        for session in sessions:
            transformed_sessions.append({
                "_id": str(session["_id"]),
                "user_id": session["user_id"],
                "brand": session["brand"],
                "created_at": session["created_at"],
                "interactions": session.get("interactions", []),
                "last_updated": session["last_updated"]
            })

        return ChatSessionResponse(sessions=transformed_sessions, total=total)
    except Exception as e:
        logger.error(f"Error fetching no-campaign chat sessions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/chat-sessions/{session_id}")
async def get_chat_session(
    session_id: str,
    user_data = Depends(verify_jwt_token)
):
    """Get specific chat session by ID"""
    try:
        user_brand = user_data.get("brand")
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")

        if not user_brand and not is_admin:
            raise HTTPException(status_code=400, detail="User brand not found")

        if not ObjectId.is_valid(session_id):
            raise HTTPException(status_code=400, detail="Invalid session ID format")

        collection = get_brand_collection()

        filter_query = {"_id": ObjectId(session_id)}
        if not is_admin and user_brand:
            filter_query['brand'] = user_brand

        session = await collection.find_one(filter_query)

        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")

        transformed_session = {
            "_id": str(session["_id"]),
            "user_id": session["user_id"],
            "brand": session["brand"],
            "created_at": session["created_at"],
            "interactions": session.get("interactions", []),
            "last_updated": session["last_updated"]
        }

        return transformed_session
    except Exception as e:
        logger.error(f"Error fetching chat session: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/chat-sessions/by-phone/{phone_number}")
async def get_chat_session_by_phone(
    phone_number: str,
    user_data = Depends(verify_jwt_token)
):
    """Get chat session by phone number"""
    try:
        user_brand = user_data.get("brand")
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")

        if not user_brand and not is_admin:
            raise HTTPException(status_code=400, detail="User brand not found")

        normalized_phone = normalize_phone_number(phone_number)
        collection = get_brand_collection()

        filter_query = {"user_id": normalized_phone}
        if not is_admin and user_brand:
            filter_query['brand'] = user_brand

        session = await collection.find_one(filter_query)

        if not session:
            raise HTTPException(status_code=404, detail="Chat session not found")

        transformed_session = {
            "_id": str(session["_id"]),
            "user_id": session["user_id"],
            "brand": session["brand"],
            "created_at": session["created_at"],
            "interactions": session.get("interactions", []),
            "last_updated": session["last_updated"]
        }

        return transformed_session
    except Exception as e:
        logger.error(f"Error fetching chat session by phone: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/campaigns", response_model=CampaignsResponse)
async def get_campaigns(user_data = Depends(verify_jwt_token)):
    """Get campaigns for user"""
    try:
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")
        user_id = user_data.get("smsUserId")

        if not is_admin and not user_id:
            raise HTTPException(status_code=400, detail="SMS user profile not found")

        if is_admin:
            params = {"sort": "-date_created"}
        else:
            params = {
                "filter": f'{{"user_id":{{"id":{{"_eq":"{user_id}"}}}}}}',
                "sort": "-date_created"
            }

        response = requests.get(
            f"{DIRECTUS_URL}/items/sms_campaign",
            params=params,
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )

        if not response.ok:
            raise HTTPException(status_code=500, detail="Failed to fetch campaigns")

        campaigns = response.json().get("data", [])
        return CampaignsResponse(campaigns=campaigns)
    except Exception as e:
        logger.error(f"Error fetching campaigns: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/groups", response_model=GroupsResponse)
async def get_groups(user_data = Depends(verify_jwt_token)):
    """Get groups for user"""
    try:
        is_admin = user_data.get("role") == os.getenv("NEXT_PUBLIC_BUSINESS_ADMIN_ROLE_ID")
        user_id = user_data.get("smsUserId")

        if not is_admin and not user_id:
            raise HTTPException(status_code=400, detail="SMS user profile not found")

        if is_admin:
            params = {"sort": "-date_created"}
        else:
            params = {
                "filter": f'{{"user_id":{{"id":{{"_eq":"{user_id}"}}}}}}',
                "sort": "-date_created"
            }

        response = requests.get(
            f"{DIRECTUS_URL}/items/sms_group",
            params=params,
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )

        if not response.ok:
            raise HTTPException(status_code=500, detail="Failed to fetch groups")

        groups = response.json().get("data", [])
        return GroupsResponse(groups=groups)
    except Exception as e:
        logger.error(f"Error fetching groups: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/admin/managers", response_model=ManagersResponse)
async def get_managers(user_data = Depends(verify_jwt_token)):
    """Get managed users for admin"""
    try:
        user_id = user_data.get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found")

        params = {
            "filter": f'{{"managed_by":{{"_eq":"{user_id}"}}}}',
            "fields": "id,first_name,last_name,email,status"
        }

        response = requests.get(
            f"{DIRECTUS_URL}/users",
            params=params,
            headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
        )

        if not response.ok:
            raise HTTPException(status_code=500, detail="Failed to fetch managers")

        managed_users = response.json().get("data", [])

        managers_with_brand = []
        for user in managed_users:
            try:
                brand_response = requests.get(
                    f"{DIRECTUS_URL}/items/sms_user",
                    params={
                        "filter": f'{{"user_id":{{"_eq":"{user["id"]}"}}}}',
                        "fields": "brand",
                        "limit": "1"
                    },
                    headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
                )

                brand = None
                if brand_response.ok:
                    brand_data = brand_response.json().get("data", [])
                    if brand_data:
                        brand = brand_data[0].get("brand")

                managers_with_brand.append({
                    **user,
                    "brand": brand
                })
            except Exception as e:
                logger.error(f"Error fetching brand for user {user['id']}: {e}")
                managers_with_brand.append({
                    **user,
                    "brand": None
                })

        return ManagersResponse(managers=managers_with_brand)
    except Exception as e:
        logger.error(f"Error fetching managers: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/admin/managers")
async def create_manager(
    manager_data: CreateManagerRequest,
    user_data = Depends(verify_jwt_token)
):
    """Create a new manager"""
    try:
        user_id = user_data.get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="User ID not found")

        create_user_data = {
            "email": manager_data.email,
            "password": manager_data.password,
            "first_name": manager_data.first_name,
            "last_name": manager_data.last_name,
            "role": os.getenv("NEXT_PUBLIC_BUSINESS_MANAGER_ROLE_ID"),
            "managed_by": user_id,
            "status": "active"
        }

        response = requests.post(
            f"{DIRECTUS_URL}/users",
            json=create_user_data,
            headers={
                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                "Content-Type": "application/json"
            }
        )

        if not response.ok:
            error_data = response.json()
            raise HTTPException(status_code=400, detail=f"Failed to create user: {error_data}")

        new_user = response.json().get("data")

        if new_user:
            await asyncio.sleep(0.1)

            try:
                existing_response = requests.get(
                    f"{DIRECTUS_URL}/items/sms_user",
                    params={
                        "filter": f'{{"user_id":{{"_eq":"{new_user["id"]}"}}}}',
                        "limit": "1"
                    },
                    headers={"Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}"}
                )

                if existing_response.ok:
                    existing_data = existing_response.json().get("data", [])

                    if existing_data:
                        sms_user_id = existing_data[0]["id"]
                        update_response = requests.patch(
                            f"{DIRECTUS_URL}/items/sms_user/{sms_user_id}",
                            json={
                                "first_name": manager_data.first_name,
                                "last_name": manager_data.last_name,
                                "brand": manager_data.brand
                            },
                            headers={
                                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                                "Content-Type": "application/json"
                            }
                        )
                        if not update_response.ok:
                            logger.error(f"Failed to update sms_user: {update_response.text}")
                    else:
                        create_response = requests.post(
                            f"{DIRECTUS_URL}/items/sms_user",
                            json={
                                "user_id": new_user["id"],
                                "first_name": manager_data.first_name,
                                "last_name": manager_data.last_name,
                                "brand": manager_data.brand
                            },
                            headers={
                                "Authorization": f"Bearer {DIRECTUS_ADMIN_TOKEN}",
                                "Content-Type": "application/json"
                            }
                        )
                        if not create_response.ok:
                            logger.error(f"Failed to create sms_user: {create_response.text}")

            except Exception as sms_error:
                logger.error(f"Error handling sms_user record: {sms_error}")

        return {
            "success": True,
            "message": "Manager created successfully",
            "user": new_user
        }

    except Exception as e:
        logger.error(f"Error creating manager: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

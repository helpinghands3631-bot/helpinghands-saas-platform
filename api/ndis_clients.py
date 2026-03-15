"""Helping Hands NDIS Client Management API"""
import os
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel, EmailStr
from notion_client import AsyncClient as NotionClient
import httpx

app = FastAPI(title="Helping Hands NDIS API", version="1.0.0")

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_CLIENTS_DB = os.getenv("NOTION_CLIENTS_DB", "")
NOTION_SHIFTS_DB = os.getenv("NOTION_SHIFTS_DB", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_KEY = os.getenv("API_KEY", "helpinghands-secret")

notion = NotionClient(auth=NOTION_TOKEN)


def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


class NDISClient(BaseModel):
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    ndis_number: Optional[str] = None
    plan_manager: Optional[str] = None
    support_categories: list[str] = []
    address: Optional[str] = None
    emergency_contact: Optional[str] = None


class Shift(BaseModel):
    client_name: str
    worker_name: str
    date: str  # ISO format
    start_time: str
    end_time: str
    support_type: str
    notes: Optional[str] = None
    kilometres: float = 0.0


class ShiftComplete(BaseModel):
    shift_id: str
    actual_end_time: str
    notes: Optional[str] = None
    incidents: Optional[str] = None
    kilometres_actual: float = 0.0


@app.get("/health")
async def health():
    return {"status": "ok", "service": "helpinghands-ndis", "timestamp": datetime.utcnow().isoformat()}


@app.post("/clients", dependencies=[Depends(verify_api_key)])
async def create_client(client: NDISClient):
    """Register a new NDIS client."""
    try:
        page = await notion.pages.create(
            parent={"database_id": NOTION_CLIENTS_DB},
            properties={
                "Name": {"title": [{"text": {"content": client.name}}]},
                "Email": {"email": client.email},
                "Phone": {"phone_number": client.phone},
                "NDIS Number": {"rich_text": [{"text": {"content": client.ndis_number or ""}}]},
                "Plan Manager": {"rich_text": [{"text": {"content": client.plan_manager or ""}}]},
                "Status": {"select": {"name": "Active"}},
                "Created": {"date": {"start": datetime.utcnow().isoformat()}}
            }
        )
        await _notify(f"New NDIS client registered: {client.name}")
        return {"id": page["id"], "name": client.name, "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/shifts", dependencies=[Depends(verify_api_key)])
async def log_shift(shift: Shift):
    """Log a new support shift."""
    try:
        page = await notion.pages.create(
            parent={"database_id": NOTION_SHIFTS_DB},
            properties={
                "Title": {"title": [{"text": {"content": f"{shift.client_name} - {shift.date}"}}]},
                "Client": {"rich_text": [{"text": {"content": shift.client_name}}]},
                "Worker": {"rich_text": [{"text": {"content": shift.worker_name}}]},
                "Date": {"date": {"start": shift.date}},
                "Start Time": {"rich_text": [{"text": {"content": shift.start_time}}]},
                "End Time": {"rich_text": [{"text": {"content": shift.end_time}}]},
                "Support Type": {"select": {"name": shift.support_type}},
                "Kilometres": {"number": shift.kilometres},
                "Status": {"select": {"name": "Scheduled"}}
            }
        )
        return {"id": page["id"], "status": "logged"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/shifts/{shift_id}/complete", dependencies=[Depends(verify_api_key)])
async def complete_shift(shift_id: str, data: ShiftComplete):
    """Mark a shift as complete."""
    try:
        await notion.pages.update(
            page_id=shift_id,
            properties={
                "Status": {"select": {"name": "Completed"}},
                "Actual End": {"rich_text": [{"text": {"content": data.actual_end_time}}]},
                "Notes": {"rich_text": [{"text": {"content": data.notes or ""}}]},
                "Incidents": {"rich_text": [{"text": {"content": data.incidents or "None"}}]},
                "Kilometres Actual": {"number": data.kilometres_actual}
            }
        )
        await _notify(f"Shift {shift_id[:8]} completed")
        return {"id": shift_id, "status": "completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/clients", dependencies=[Depends(verify_api_key)])
async def list_clients(status: str = "Active"):
    """List all active NDIS clients."""
    try:
        resp = await notion.databases.query(
            database_id=NOTION_CLIENTS_DB,
            filter={"property": "Status", "select": {"equals": status}}
        )
        clients = []
        for page in resp.get("results", []):
            props = page["properties"]
            clients.append({
                "id": page["id"],
                "name": props.get("Name", {}).get("title", [{}])[0].get("plain_text", ""),
                "email": props.get("Email", {}).get("email"),
                "ndis_number": props.get("NDIS Number", {}).get("rich_text", [{}])[0].get("plain_text", "")
            })
        return {"clients": clients, "count": len(clients)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _notify(msg: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
        except Exception:
            pass

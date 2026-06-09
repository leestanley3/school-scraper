import os
import re
import json
import httpx
import asyncio
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import anthropic

app = FastAPI(title="School Staff Scraper")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
REOON_API_KEY = os.environ.get("REOON_API_KEY")
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "changeme123")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

class ScrapeRequest(BaseModel):
    account_id: str
    staff_page_url: str
    school_name: str
    urn: Optional[str] = None

class BulkScrapeRequest(BaseModel):
    schools: list[ScrapeRequest]

# ── Zoho auth ────────────────────────────────────────────────────────────────

async def get_zoho_token():
    async with httpx.AsyncClient() as client:
        r = await client.post("https://accounts.zoho.com/oauth/v2/token", params={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token"
        })
        return r.json().get("access_token")

# ── Scraping ─────────────────────────────────────────────────────────────────

async def fetch_page(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SchoolScraper/1.0)"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        r = await client.get(url, headers=headers)
        return r.text

def extract_email_pattern(html: str, domain: str) -> Optional[str]:
    emails = re.findall(r'[a-zA-Z0-9_.+-]+@' + re.escape(domain), html)
    if not emails:
        return None
    for email in emails:
        local = email.split("@")[0]
        if re.match(r'^[a-z]\.[a-z]+$', local):
            return "f.lastname"
        if re.match(r'^[a-z]{2}\.[a-z]+$', local):
            return "fi.lastname"
        if re.match(r'^[a-z]+\.[a-z]+$', local):
            return "firstname.lastname"
        if re.match(r'^[a-z]+$', local) and len(local) > 4:
            return "firstname"
    return None

def apply_email_pattern(pattern: str, first: str, last: str, domain: str) -> Optional[str]:
    if not first or not last:
        return None
    f = first.lower().replace(" ", "")
    l = last.lower().replace(" ", "").replace("'", "").replace("-", "")
    fi = f[0] if f else ""
    fi2 = f[:2] if len(f) >= 2 else fi
    mapping = {
        "f.lastname": f"{fi}.{l}",
        "fi.lastname": f"{fi2}.{l}",
        "firstname.lastname": f"{f}.{l}",
        "firstname": f,
    }
    local = mapping.get(pattern)
    return f"{local}@{domain}" if local else None

# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_staff_with_claude(html: str, school_name: str) -> dict:
    # Strip HTML tags for cleaner input, keep enough context
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'\s+', ' ', clean).strip()[:8000]

    prompt = f"""You are a precise data extraction engine for UK school staff directories.
Extract staff for: {school_name}

Find these roles (and synonyms):
1. Headteacher / Head Teacher / Principal / Head of School / Executive Head
2. SENCo / SENDCo / Inclusion Manager / Special Educational Needs Coordinator / Head of Learning Support
3. HR Manager / Human Resources Manager / HR Officer / Business Manager / School Business Manager / Office Manager

For each person found return a JSON object. If a role is not found, return null fields.

Rules:
- Extract Title (Mr/Mrs/Ms/Miss/Dr/Prof), First Name, Last Name
- If only initial given (e.g. "Mr J. Smith"), use "J." as first_name
- Do NOT guess or infer — only extract what is explicitly stated
- exact_role_found = the exact text used on the page for their role

Respond ONLY with valid JSON, no markdown, no explanation:
{{
  "headteacher": {{"title": null, "first_name": null, "last_name": null, "exact_role_found": null}},
  "senco": {{"title": null, "first_name": null, "last_name": null, "exact_role_found": null}},
  "hr_manager": {{"title": null, "first_name": null, "last_name": null, "exact_role_found": null}}
}}

Text to analyse:
{clean}"""

    message = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r'^```json|^```|```$', '', raw, flags=re.MULTILINE).strip()
    return json.loads(raw)

# ── Reoon verification ────────────────────────────────────────────────────────

async def verify_email_reoon(email: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://emailverifier.reoon.com/api/v1/verify", params={
            "email": email,
            "key": REOON_API_KEY,
            "mode": "quick"
        })
        data = r.json()
        return {
            "email": email,
            "status": data.get("status", "unknown"),
            "is_valid": data.get("status") in ["valid", "safe"],
        }

# ── Zoho CRM write-back ───────────────────────────────────────────────────────

async def get_existing_contacts(account_id: str, token: str) -> list:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://www.zohoapis.com/crm/v3/Contacts/search",
            headers={"Authorization": f"Zoho-oauthtoken {token}"},
            params={"criteria": f"Account_Name.id:equals:{account_id}"}
        )
        data = r.json()
        return data.get("data", [])

async def upsert_contact(account_id: str, person: dict, role: str, email_info: dict, token: str):
    existing = await get_existing_contacts(account_id, token)
    match = None
    for c in existing:
        if c.get("Last_Name", "").lower() == (person.get("last_name") or "").lower():
            match = c
            break

    payload = {
        "Last_Name": person.get("last_name") or "Unknown",
        "First_Name": person.get("first_name"),
        "Salutation": person.get("title"),
        "Title": person.get("exact_role_found"),
        "Account_Name": {"id": account_id},
        "Email": email_info.get("email") if email_info and email_info.get("is_valid") else None,
        "Description": f"Scraped role: {person.get('exact_role_found')} | Email status: {email_info.get('status', 'not checked') if email_info else 'not generated'}"
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    async with httpx.AsyncClient() as client:
        if match:
            r = await client.put(
                f"https://www.zohoapis.com/crm/v3/Contacts/{match['id']}",
                headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
                json={"data": [payload]}
            )
        else:
            r = await client.post(
                "https://www.zohoapis.com/crm/v3/Contacts",
                headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
                json={"data": [payload]}
            )
    return r.json()

async def update_account_fields(account_id: str, email_pattern: str, token: str):
    async with httpx.AsyncClient() as client:
        await client.put(
            f"https://www.zohoapis.com/crm/v3/Accounts/{account_id}",
            headers={"Authorization": f"Zoho-oauthtoken {token}", "Content-Type": "application/json"},
            json={"data": [{"id": account_id, "Email_Pattern": email_pattern, "Last_Scraped": "today"}]}
        )

# ── Core scrape logic ─────────────────────────────────────────────────────────

async def process_school(req: ScrapeRequest) -> dict:
    result = {"account_id": req.account_id, "school": req.school_name, "status": "ok", "contacts_written": []}
    try:
        html = await fetch_page(req.staff_page_url)
        domain = re.sub(r'^https?://(www\.)?', '', req.staff_page_url).split('/')[0]
        email_pattern = extract_email_pattern(html, domain)
        staff = extract_staff_with_claude(html, req.school_name)
        token = await get_zoho_token()

        role_map = {
            "headteacher": staff.get("headteacher"),
            "senco": staff.get("senco"),
            "hr_manager": staff.get("hr_manager"),
        }

        for role, person in role_map.items():
            if not person or not person.get("last_name"):
                continue
            email_info = None
            if email_pattern:
                generated = apply_email_pattern(email_pattern, person.get("first_name", ""), person.get("last_name", ""), domain)
                if generated:
                    email_info = await verify_email_reoon(generated)
            await upsert_contact(req.account_id, person, role, email_info, token)
            result["contacts_written"].append({
                "role": role,
                "name": f"{person.get('title','')} {person.get('first_name','')} {person.get('last_name','')}".strip(),
                "email": email_info.get("email") if email_info else None,
                "email_valid": email_info.get("is_valid") if email_info else None,
            })

        if email_pattern:
            await update_account_fields(req.account_id, email_pattern, token)
            result["email_pattern"] = email_pattern

    except Exception as e:
        import traceback
        result["status"] = "error"
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()
        print(f"ERROR processing {req.school_name}: {str(e)}")
        print(traceback.format_exc())

    return result

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/scrape")
async def scrape_get(account_id: str, staff_page_url: str, school_name: str, secret: str, urn: str = None):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid secret")
    scrape_req = ScrapeRequest(account_id=account_id, staff_page_url=staff_page_url, school_name=school_name, urn=urn)
    return await process_school(scrape_req)

@app.post("/scrape/bulk")
async def scrape_bulk(req: BulkScrapeRequest, x_webhook_secret: str = Header(None)):
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    results = []
    for school in req.schools:
        result = await process_school(school)
        results.append(result)
        await asyncio.sleep(1)  # polite delay between requests
    return {"processed": len(results), "results": results}

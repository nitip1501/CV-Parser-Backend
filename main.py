from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pdfplumber
import io
import os
from docx import Document
from tempfile import NamedTemporaryFile
from dotenv import load_dotenv
import google.generativeai as genai
import requests
import json
import re
from hubspot import HubSpot
from hubspot.crm.properties import PropertyCreate
from hubspot.crm.contacts import (
    PublicObjectSearchRequest, Filter, FilterGroup,
    SimplePublicObjectInputForCreate, SimplePublicObjectInput
)

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Or set to your frontend URL like ["http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#hubspot token
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN")

# Initialize Gemini client
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
MODEL = "gemini-2.5-flash"
model = genai.GenerativeModel(model_name=MODEL)
hubspot_client = HubSpot(access_token=os.getenv("HUBSPOT_TOKEN"))
FOLDER_ID="249026326717"

# Improved prompt: prevents Gemini from wrapping response in code blocks
RESUME_PROMPT = """
You are a resume parser. Extract the following fields from the resume text below and output ONLY a VALID JSON object that exactly matches this schema (no extra text, formatting, or comments):

{
  "name": "",         // Full name of the candidate
  "email": "",        // Email address
  "phone": "",        // Contact number
  "job_title": "",    // Current or most recent job title
  "skills": [],       // List of key technical and soft skills
  "experience": "",   // Brief summary of professional experience
  "company": "",      // Current or most recent employer
  "location": ""      // City and state/country
}

Resume Text:
"""

def extract_text_from_pdf(file_bytes):
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text

def extract_text_from_docx(file_bytes):
    with NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
        tmp.write(file_bytes)
        tmp.flush()
        doc = Document(tmp.name)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

def upload_bytes_to_hs(data: bytes, filename: str, folder_id: str) -> str:
    url = "https://api.hubapi.com/files/v3/files"
    headers = {
        "Authorization": f"Bearer {os.getenv('HUBSPOT_TOKEN')}"
    }

    options = {
        "access": "PRIVATE",
        "overwrite": False,
    }

    files = {
        "file": (filename, io.BytesIO(data)),
        "fileName": (None, filename),
        "folderId": (None, folder_id),
        "access": (None, "PRIVATE"),
        "overwrite": (None, "false"),
        "options": (None, json.dumps(options), "application/json")
    }

    resp = requests.post(url, headers=headers, files=files)
    print(resp)

    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise HTTPException(status_code=resp.status_code, detail=f"HubSpot upload failed: {resp.text}")

    file_url = resp.json().get("url")
    if not file_url:
        raise HTTPException(status_code=500, detail="File uploaded, but URL not returned by HubSpot.")

    return file_url

@app.post("/parse_resume/")
async def parse_resume(file: UploadFile = File(...)):
    data = await file.read()
    content_type = file.content_type
    text = ""

    #text extraction
    try:
        if content_type == "application/pdf":
            text = extract_text_from_pdf(data)
        elif content_type in [
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword"
        ]:
            text = extract_text_from_docx(data)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract text: {str(e)}")

    if not text.strip():
        raise HTTPException(status_code=400, detail="No text extracted from the file.")
    
    #file upload to hubspot
    try:
        file_url = upload_bytes_to_hs(data, file.filename,FOLDER_ID)
        # print("Upload Complete",file_url)
    except Exception as e:
        raise HTTPException(500, f"File upload failed: {e}")

    # Compose prompt
    prompt = RESUME_PROMPT + "\n\n" + text

    try:
        # Send to Gemini
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        )

        # print(response)

        raw = response.text.strip()
        # safe cleanup
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)

        name = parsed.get("name", "").strip()
        parts = name.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

        #skills setup
        prop = hubspot_client.crm.properties.core_api.get_by_name(
            object_type="contacts", property_name="skills"
        )

        # print(existing)

        existing = {opt.value for opt in prop.options}
        incoming = set(parsed.get("skills", []))

        # Always defined
        combined = existing.union(incoming)

        print(combined)

        opts_payload = [{"label": v, "value": v} for v in sorted(combined)]

        update_prop = PropertyCreate(
            name="skills",
            label="Skills",
            group_name="contactinformation",
            type="enumeration",
            field_type="checkbox",
            options=opts_payload
        )
        response = hubspot_client.crm.properties.core_api.update(
            object_type="contacts",
            property_name="skills",
            property_update=update_prop
        )

        # print(response)

        selected = incoming
        skills_str = ";".join(selected)

        email = parsed["email"]

        req = PublicObjectSearchRequest(
            filter_groups=[FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])],
            properties=["email"], limit=1
        )

        search_res = hubspot_client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if search_res.results:
            # Update
            contact_id = search_res.results[0].id
            hubspot_client.crm.contacts.basic_api.update(
                contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties={
                        "firstname": firstname,
                        "lastname": lastname,
                        "phone": parsed["phone"],
                        "jobtitle": parsed["job_title"],
                        "company": parsed["company"],
                        "skills": skills_str,
                        "resume_file_url": file_url
                    }
                )
            )
        else:
            # Create
            in_props = {
                "firstname": firstname,
                "lastname": lastname,
                "email": email,
                "phone": parsed["phone"],
                "jobtitle": parsed["job_title"],
                "company": parsed["company"],
                "skills": skills_str,
                "resume_file_url": file_url
            }
            hs_obj = hubspot_client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=in_props)
            )
            contact_id = hs_obj.id

        return JSONResponse(content=parsed)


        # contact_input = SimplePublicObjectInputForCreate(
        #     properties={
        #         "firstname": firstname,
        #         "lastname": lastname,
        #         "email": parsed["email"],
        #         "phone": parsed["phone"],
        #         "jobtitle": parsed["job_title"],
        #         "company": parsed["company"],
        #         "skills": skills_str
        #     }
        # )

        # # contact creation
        # try:
        #     hs_response = hubspot_client.crm.contacts.basic_api.create(
        #         simple_public_object_input_for_create=contact_input
        #     )
        #     contact_id = hs_response.id
        #     # print("contact created", hs_response)
        # except Exception as e:
        #     raise HTTPException(status_code=500, detail=f"HubSpot contact create failed: {e}")
        
        # #contact updation for file_url
        # try:
        #     hs_update_response = hubspot_client.crm.contacts.basic_api.update(
        #         contact_id,
        #         simple_public_object_input=SimplePublicObjectInput(properties={"resume_file_url": file_url})
        #     )
        #     # print("file_url updated" ,hs_update_response)
        # except Exception as e:
        #     raise HTTPException(status_code=500, detail=f"Failed to update resume URL on contact {contact_id}: {e}")

        # return JSONResponse(content=parsed)

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Gemini returned invalid JSON.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI parsing failed: {str(e)}")
    
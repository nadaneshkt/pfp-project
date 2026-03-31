from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
import psycopg2
import os
import json
import base64
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

app = FastAPI()

mimo_client = OpenAI(
    api_key=os.getenv("MIMO_API_KEY"),
    base_url="https://api.xiaomimimo.com/v1"
)

def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# ── MCP TOOLS ──────────────────────────────────────────────
# These tools are called by the agent — the LLM never touches this math directly

def mcp_get_brand_specs(brand: str, size: str, cur):
    """MCP Tool: Fetches real-world brand measurements from AlloyDB"""
    cur.execute(
        "SELECT waist_cm, inseam_cm, hip_cm, shoulder_cm FROM brands WHERE LOWER(brand)=LOWER(%s) AND size_label=%s",
        (brand, size)
    )
    return cur.fetchone()

def mcp_calculate_fit(user_waist: float, brand_waist: float) -> dict:
    """MCP Tool: Mathematical fit comparison — LLM cannot override this result"""
    diff = user_waist - brand_waist
    if abs(diff) <= 2:
        verdict = "PERFECT"
    elif diff > 2:
        verdict = "LOOSE"
    else:
        verdict = "TIGHT"
    return {"verdict": verdict, "difference_cm": round(diff, 1)}

def mcp_find_sister_brands(waist, inseam, hip, shoulder, cur) -> list:
    """MCP Tool: Vector similarity search across brands using pgvector"""
    vector = f'[{waist},{inseam},{hip},{shoulder}]'
    cur.execute(
        "SELECT brand, size_label, (fit_vector <=> %s::vector) AS distance FROM brands ORDER BY distance ASC LIMIT 3",
        (vector,)
    )
    return [{"brand": r[0], "size": r[1], "score": round(1 - r[2], 2)} for r in cur.fetchall()]

def mcp_get_recommended_size(brand: str, waist: float, cur) -> str:
    """MCP Tool: Finds next available size up when fit is TIGHT"""
    cur.execute(
        "SELECT size_label FROM brands WHERE LOWER(brand)=LOWER(%s) AND waist_cm >= %s ORDER BY waist_cm ASC LIMIT 1",
        (brand, waist)
    )
    rec = cur.fetchone()
    return rec[0] if rec else None

# ── A2A AGENT CARD ─────────────────────────────────────────

@app.get("/.well-known/agent.json")
def agent_card():
    """A2A Discovery: Store Concierge Agent Card"""
    return JSONResponse({
        "name": "PFP Store Concierge Agent",
        "description": "Verifies garment fit using real brand measurement data",
        "version": "1.0",
        "protocol": "A2A",
        "skills": [
            {
                "id": "verify_fit",
                "name": "Verify Fit",
                "description": "Accepts user measurements, returns fit verdict without storing PII",
                "input_schema": {
                    "brand": "string",
                    "size": "string",
                    "waist_cm": "float",
                    "inseam_cm": "float",
                    "hip_cm": "float",
                    "shoulder_cm": "float"
                },
                "output_schema": {
                    "fit_verdict": "PERFECT|TIGHT|LOOSE",
                    "recommended_size": "string",
                    "difference_cm": "float",
                    "sister_brands": "array"
                }
            }
        ],
        "privacy": "Measurements are processed transiently. No PII stored."
    })

@app.post("/a2a/verify_fit")
async def a2a_verify_fit(payload: dict):
    """A2A Endpoint: Agent B (Store Concierge) — called by Agent A (Personal Stylist)"""
    conn = get_db()
    cur = conn.cursor()

    specs = mcp_get_brand_specs(payload["brand"], payload["size"], cur)
    if not specs:
        conn.close()
        return {"error": "Brand not found"}

    fit = mcp_calculate_fit(payload["waist_cm"], specs[0])
    sisters = mcp_find_sister_brands(
        payload["waist_cm"], payload["inseam_cm"],
        payload["hip_cm"], payload["shoulder_cm"], cur
    )

    recommended_size = payload["size"]
    if fit["verdict"] == "TIGHT":
        recommended_size = mcp_get_recommended_size(payload["brand"], payload["waist_cm"], cur) or payload["size"]

    conn.close()

    return {
        "a2a_artifact": {
            "fit_verdict": fit["verdict"],
            "recommended_size": recommended_size,
            "difference_cm": fit["difference_cm"],
            "sister_brands": sisters,
            "privacy_note": "Measurements processed and discarded. Session ID only."
        }
    }

# ── MAIN ENDPOINT ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return open("index.html").read()

@app.post("/analyze")
async def analyze(
    photo: UploadFile = File(...),
    brand: str = Form(...),
    size: str = Form(...),
    waist: float = Form(...),
    inseam: float = Form(...),
    hip: float = Form(...),
    shoulder: float = Form(...)
):
    # Agent A (Personal Stylist) calls Agent B via A2A
    import httpx
    a2a_response = httpx.post(
        "http://localhost:8000/a2a/verify_fit",
        json={
            "brand": brand,
            "size": size,
            "waist_cm": waist,
            "inseam_cm": inseam,
            "hip_cm": hip,
            "shoulder_cm": shoulder
        }
    )
    fit_result = a2a_response.json()["a2a_artifact"]

    # Save to history
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO fit_history (session_id, brand, size_label, fit_verdict, recommended_size) VALUES (%s, %s, %s, %s, %s)",
        ("demo_session", brand, size, fit_result["fit_verdict"], fit_result["recommended_size"])
    )
    conn.commit()
    conn.close()

    # Korean 24-season color analysis via MiMo Vision
    image_bytes = await photo.read()
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    mime_type = photo.content_type

    try:
        color_completion = mimo_client.chat.completions.create(
            model="mimo-v2-omni",
            messages=[
                {
                    "role": "system",
                    "content": "You are a certified Korean Personal Color analyst. Always respond in valid JSON only, no extra text, no markdown."
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_b64}"
                            }
                        },
                        {
                            "type": "text",
                            "text": (
                                "Analyze this person using the Korean Personal Color Analysis 24-season system. "
                                "Evaluate: skin value (light/medium/dark), undertone (warm/cool/neutral), "
                                "chroma (vivid/bright/muted/soft/dull/deep/pale), "
                                "contrast level (high/medium/low) between skin, hair and eyes. "
                                "Determine their exact season from these 12 types: "
                                "True Spring, Light Spring, Bright Spring, "
                                "True Summer, Light Summer, Soft Summer, "
                                "True Autumn, Dark Autumn, Soft Autumn, "
                                "True Winter, Dark Winter, Bright Winter. "
                                "Return JSON only with these exact keys: "
                                '{"undertone":"warm/cool/neutral",'
                                '"value":"light/medium/dark",'
                                '"chroma":"vivid/bright/muted/soft/dull/deep/pale",'
                                '"contrast":"high/medium/low",'
                                '"season_24":"exact season name",'
                                '"recommended_colors":[{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"}],'
                                '"avoid_colors":[{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"},{"name":"color name","hex":"#hexcode"}],'
                                '"clothing_style_tips":"2 sentence styling advice",'
                                '"one_line_summary":"string"}'
                            )
                        }
                    ]
                }
            ],
            max_completion_tokens=800
        )
        color_text = color_completion.choices[0].message.content.strip()
        color_text = color_text.replace("```json", "").replace("```", "").strip()
        color_data = json.loads(color_text)
    except Exception as e:
        color_data = {"one_line_summary": f"Color analysis unavailable: {str(e)}"}

    return {
        "fit_verdict": fit_result["fit_verdict"],
        "recommended_size": fit_result["recommended_size"],
        "waist_difference_cm": fit_result["difference_cm"],
        "sister_brands": fit_result["sister_brands"],
        "color_analysis": color_data
    }
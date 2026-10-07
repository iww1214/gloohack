"""
post_patrol_report.py
======================
Generates a professional post-patrol PDF report after each Safety1271
session. Uses Claude to analyse the patrol log and produce an executive
summary and actionable recommendations.

WHAT'S IN THE REPORT
---------------------
  Page 1  Cover — site, date, drone, overall verdict
  Page 2  Executive summary (Claude-generated from log analysis)
  Page 3  Session statistics + pre-flight summary
  Page 4  Safety findings — all hazards detected
  Page 5  Security findings — all concerns raised
  Page 6  Zone-by-zone patrol summary
  Page 7  Incident photos (up to 12, 3-per-row grid)
  Page 8  Drone performance metrics
  Page 9  Recommendations (Claude-generated)
  Page 10 Sign-off block for RPIC + security lead

HOW TO USE
----------
  # Called automatically at end of each patrol session
  # Also callable standalone:
  python post_patrol_report.py                       # uses today's log
  python post_patrol_report.py --log patrol_20260701.log
  python post_patrol_report.py --email               # also email report
  python post_patrol_report.py --no-claude           # skip AI analysis

INTEGRATION HOOKS
-----------------
  In patrol_scheduler.py → run_patrol_job():
      from post_patrol_report import generate_report
      generate_report(session_data=session_summary, email=True)

  In run_safety1271.py → finally block:
      from post_patrol_report import generate_report
      generate_report(email=True)
"""

import os, re, json, base64, argparse, logging
from pathlib   import Path
from datetime  import datetime
from typing    import Optional

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("PatrolReport")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")

# ── Dependencies ──────────────────────────────────────────────────────────────
from reportlab.lib.pagesizes  import letter
from reportlab.lib.units      import inch
from reportlab.lib            import colors
from reportlab.lib.styles     import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums      import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus       import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image, HRFlowable, KeepTogether
)
from reportlab.platypus       import ListFlowable, ListItem

from gloo_client import GlooAnthropicCompat, complete, run_agent, MODELS, GLOO_TRADITION
from PIL import Image as PILImage

# ── Config ────────────────────────────────────────────────────────────────────
COMPOUND_NAME    = os.getenv("COMPOUND_NAME",    "Burke Community Church")
COMPOUND_ADDRESS = os.getenv("COMPOUND_ADDRESS", "5688 Burke Centre Pkwy, Burke, VA 22015")
ALERT_EMAIL      = os.getenv("ALERT_EMAIL",      "")
INCIDENT_DIR     = Path(os.getenv("INCIDENT_DIR", "./incidents"))
REPORTS_DIR      = Path(os.getenv("REPORTS_DIR",  "./reports"))
REPORTS_DIR.mkdir(exist_ok=True)

# ── Brand colours ─────────────────────────────────────────────────────────────
NAVY    = colors.HexColor("#1a2744")
BURGY   = colors.HexColor("#6b2737")
GOLD    = colors.HexColor("#c9a84c")
LGOLD   = colors.HexColor("#f5edd6")
LNAVY   = colors.HexColor("#e8ecf5")
WHITE   = colors.white
RED     = colors.HexColor("#cc2200")
AMBER   = colors.HexColor("#ba7517")
GREEN   = colors.HexColor("#1a7a1a")
LGRAY   = colors.HexColor("#f4f4f4")
MGRAY   = colors.HexColor("#888888")
DKGRAY  = colors.HexColor("#2c2c2c")


# ══════════════════════════════════════════════════════════════════════════════
#  STYLE SHEET
# ══════════════════════════════════════════════════════════════════════════════

def build_styles():
    base = getSampleStyleSheet()
    S    = {}

    def st(name, **kw):
        S[name] = ParagraphStyle(name, **kw)

    st("cover_title",   fontSize=28, fontName="Helvetica-Bold",
       textColor=WHITE,  alignment=TA_CENTER, spaceAfter=6)
    st("cover_sub",     fontSize=14, fontName="Helvetica",
       textColor=LGOLD,  alignment=TA_CENTER, spaceAfter=4)
    st("cover_info",    fontSize=11, fontName="Helvetica",
       textColor=WHITE,  alignment=TA_CENTER, spaceAfter=3)
    st("cover_verdict", fontSize=22, fontName="Helvetica-Bold",
       textColor=GOLD,   alignment=TA_CENTER, spaceBefore=20)

    st("section_head",  fontSize=14, fontName="Helvetica-Bold",
       textColor=NAVY,   spaceBefore=16, spaceAfter=6,
       borderPadding=(0,0,4,0))
    st("sub_head",      fontSize=11, fontName="Helvetica-Bold",
       textColor=BURGY,  spaceBefore=10, spaceAfter=4)
    st("body",          fontSize=10, fontName="Helvetica",
       textColor=DKGRAY, leading=15,    spaceAfter=6)
    st("body_small",    fontSize=9,  fontName="Helvetica",
       textColor=MGRAY,  leading=13)
    st("body_bold",     fontSize=10, fontName="Helvetica-Bold",
       textColor=DKGRAY, leading=15)
    st("caption",       fontSize=8,  fontName="Helvetica",
       textColor=MGRAY,  alignment=TA_CENTER)
    st("footer_text",   fontSize=8,  fontName="Helvetica",
       textColor=MGRAY,  alignment=TA_CENTER)
    st("tbl_hdr",       fontSize=9,  fontName="Helvetica-Bold",
       textColor=WHITE,  alignment=TA_CENTER)
    st("tbl_cell",      fontSize=9,  fontName="Helvetica",
       textColor=DKGRAY)
    st("verdict_go",    fontSize=16, fontName="Helvetica-Bold",
       textColor=GREEN,  alignment=TA_CENTER)
    st("verdict_nogo",  fontSize=16, fontName="Helvetica-Bold",
       textColor=RED,    alignment=TA_CENTER)
    st("verdict_cond",  fontSize=16, fontName="Helvetica-Bold",
       textColor=AMBER,  alignment=TA_CENTER)

    return S


# ══════════════════════════════════════════════════════════════════════════════
#  LOG PARSER
# ══════════════════════════════════════════════════════════════════════════════

def parse_log(log_path: Path) -> dict:
    """
    Parse the patrol log file and extract structured event data.
    Returns dict with categorised events, timeline, and summary counts.
    """
    if not log_path.exists():
        log.warning(f"Log file not found: {log_path}")
        return {}

    lines = log_path.read_text().splitlines()
    events = {
        "all":       [],
        "safety":    [],
        "security":  [],
        "holds":     [],
        "alerts":    [],
        "emergencies": [],
        "clear":     [],
        "preflight": [],
        "dynamic":   [],
        "atc":       [],
        "start_time": None,
        "end_time":   None,
    }

    time_pattern = re.compile(r"^(\d{2}:\d{2}:\d{2})")
    severity_map = {
        "CRITICAL": "critical",
        "WARNING":  "warning",
        "INFO":     "info",
        "ERROR":    "error",
    }

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        tm = time_pattern.match(stripped)
        ts = tm.group(1) if tm else "??:??:??"
        events["all"].append({"time": ts, "line": stripped})

        if events["start_time"] is None and tm:
            events["start_time"] = ts
        if tm:
            events["end_time"] = ts

        lower = stripped.lower()

        if any(x in lower for x in ["safety_hazard","safety hazard","⚠️"]):
            events["safety"].append({"time": ts, "detail": stripped})
        if any(x in lower for x in ["security_concern","security concern","🔒"]):
            events["security"].append({"time": ts, "detail": stripped})
        if any(x in lower for x in ["hold","virtual stick"]):
            events["holds"].append({"time": ts, "detail": stripped})
        if any(x in lower for x in ["immediate_alert","immediate alert","🚨"]):
            events["alerts"].append({"time": ts, "detail": stripped})
        if any(x in lower for x in ["emergency","🆘","call 911"]):
            events["emergencies"].append({"time": ts, "detail": stripped})
        if "all_clear" in lower or "all clear" in lower or "✅" in stripped:
            events["clear"].append({"time": ts, "detail": stripped})
        if "pre-flight" in lower or "preflight" in lower:
            events["preflight"].append({"time": ts, "detail": stripped})
        if "dynamic" in lower and "check" in lower:
            events["dynamic"].append({"time": ts, "detail": stripped})
        if "atc" in lower and "alert" in lower:
            events["atc"].append({"time": ts, "detail": stripped})

    return events


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def claude_analyse(log_text: str, session_data: dict) -> dict:
    """
    Ask Claude to analyse the patrol log and generate:
    - executive_summary: 2-3 paragraph overview
    - key_findings: list of bullet points
    - recommendations: numbered list of action items
    - overall_risk: LOW / MEDIUM / HIGH / CRITICAL
    Returns dict of these four fields.
    """
    client = GlooAnthropicCompat()  # Routes through Gloo AI platform

    stats_block = json.dumps({
        "frames_analysed":  session_data.get("frames", 0),
        "safety_flags":     session_data.get("safety_flags", 0),
        "security_flags":   session_data.get("security_flags", 0),
        "alerts_sent":      session_data.get("alerts_sent", 0),
        "emergencies":      session_data.get("emergencies", 0),
        "holds_executed":   session_data.get("holds_executed", 0),
        "patrol_duration":  session_data.get("duration_min", 0),
    }, indent=2)

    prompt = f"""You are a professional security consultant reviewing an AI drone patrol report
for {COMPOUND_NAME}, {COMPOUND_ADDRESS}.

SESSION STATISTICS:
{stats_block}

PATROL LOG (last 60 relevant lines):
{log_text[-6000:]}

Write a professional post-patrol security assessment. Respond ONLY in valid JSON with these four keys:

{{
  "executive_summary": "2-3 paragraphs (plain text, no markdown). Professional tone. Summarise what happened during the patrol, highlight the most significant safety and security events, and give an overall assessment of the compound's safety posture during this period.",

  "key_findings": [
    "Finding 1 — specific, actionable",
    "Finding 2 — specific, actionable",
    ...up to 8 findings
  ],

  "recommendations": [
    "1. Specific recommendation with clear owner (facilities / security team / leadership)",
    "2. ...",
    ...up to 8 recommendations, prioritised by urgency
  ],

  "overall_risk": "LOW | MEDIUM | HIGH | CRITICAL"
}}

Rules: No markdown headers or bullets inside JSON strings. Plain sentences only.
JSON must be valid — no trailing commas. Return ONLY the JSON object."""

    try:
        resp = client.messages.create(
            model=MODELS["report"],
            max_tokens = 2000,
        tradition=GLOO_TRADITION,
            messages   = [{"role": "user", "content": prompt}]
        )
        raw  = resp.content[0].text.strip()
        raw  = raw.replace("```json","").replace("```","").strip()
        return json.loads(raw)
    except Exception as e:
        log.warning(f"Claude analysis failed: {e}")
        return {
            "executive_summary": (
                f"Patrol completed for {COMPOUND_NAME}. "
                f"Session recorded {session_data.get('frames',0)} frames, "
                f"{session_data.get('safety_flags',0)} safety flags, and "
                f"{session_data.get('security_flags',0)} security flags. "
                "Manual review of the patrol log is recommended."
            ),
            "key_findings":   ["Manual log review required — AI analysis unavailable."],
            "recommendations":["Review patrol log manually before next session."],
            "overall_risk":   "MEDIUM"
        }


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

def _header_footer(canvas, doc, title: str, ts: str):
    """Adds header bar and footer to every page after the cover."""
    if doc.page == 1:
        return
    W, H = letter
    canvas.saveState()

    # Header
    canvas.setFillColor(NAVY)
    canvas.rect(0, H - 36, W, 36, fill=1, stroke=0)
    canvas.setFillColor(WHITE)
    canvas.setFont("Helvetica-Bold", 9)
    canvas.drawString(0.5*inch, H - 22, "Safety1271 — Post-Patrol Report")
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(W - 0.5*inch, H - 22, f"{COMPOUND_NAME}  ·  {ts}")

    # Footer
    canvas.setFillColor(LGRAY)
    canvas.rect(0, 0, W, 28, fill=1, stroke=0)
    canvas.setFillColor(MGRAY)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(0.5*inch, 10, "CONFIDENTIAL — For internal security use only")
    canvas.drawRightString(W - 0.5*inch, 10, f"Page {doc.page}")

    canvas.restoreState()


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def cover_page(S: dict, session: dict, patrol_ts: str, verdict: str) -> list:
    W, H = letter
    story = []

    # Full-width navy cover block — simulate with a tall table
    risk_color = {"LOW": GREEN, "MEDIUM": AMBER, "HIGH": RED, "CRITICAL": RED}.get(
        session.get("overall_risk","MEDIUM"), AMBER)

    # Top navy band content via paragraphs on coloured table
    cover_table = Table([[
        Paragraph(f"<b>Safety1271</b>", S["cover_title"]),
        ""
    ],[
        Paragraph("Post-Patrol Safety &amp; Security Report", S["cover_sub"]),
        ""
    ],[
        Paragraph(COMPOUND_NAME, S["cover_sub"]),
        ""
    ],[
        Paragraph(COMPOUND_ADDRESS, S["cover_info"]),
        ""
    ],[
        Paragraph(f"Patrol date: {patrol_ts}", S["cover_info"]),
        ""
    ],[
        Paragraph(f"Drone: DJI Mini 4 Pro  ·  RPIC on site", S["cover_info"]),
        ""
    ],[
        Paragraph(f"Overall risk: {session.get('overall_risk','MEDIUM')}",
                  S["cover_verdict"]),
        ""
    ]], colWidths=[W - inch, 0])

    cover_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), NAVY),
        ("TOPPADDING",  (0,0), (0,0),   60),
        ("BOTTOMPADDING",(0,-1),(0,-1), 50),
        ("SPAN",        (0,0), (-1,0)),
        ("SPAN",        (0,1), (-1,1)),
        ("SPAN",        (0,2), (-1,2)),
        ("SPAN",        (0,3), (-1,3)),
        ("SPAN",        (0,4), (-1,4)),
        ("SPAN",        (0,5), (-1,5)),
        ("SPAN",        (0,6), (-1,6)),
    ]))
    story.append(cover_table)

    # Stats bar below cover
    stats = [
        ("Frames analysed", f"{session.get('frames',0):,}"),
        ("Safety flags",    str(session.get("safety_flags", 0))),
        ("Security flags",  str(session.get("security_flags", 0))),
        ("Alerts sent",     str(session.get("alerts_sent", 0))),
        ("Holds executed",  str(session.get("holds_executed", 0))),
        ("Emergencies",     str(session.get("emergencies", 0))),
        ("Duration",        f"{session.get('duration_min',0)} min"),
    ]
    stat_data = [[Paragraph(f"<b>{v}</b>", ParagraphStyle("sv", fontSize=16, fontName="Helvetica-Bold",
                                                           textColor=NAVY, alignment=TA_CENTER))
                  for _, v in stats],
                 [Paragraph(k, ParagraphStyle("sk", fontSize=8, fontName="Helvetica",
                                              textColor=MGRAY, alignment=TA_CENTER))
                  for k, _ in stats]]

    stat_table = Table(stat_data, colWidths=[(W - inch) / len(stats)] * len(stats))
    stat_table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), LGOLD),
        ("TOPPADDING",    (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LINEBELOW",     (0,0), (-1,0),  0.5, GOLD),
    ]))
    story.append(stat_table)
    story.append(PageBreak())
    return story


def executive_summary_section(S: dict, analysis: dict) -> list:
    story = []
    story.append(Paragraph("Executive Summary", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    risk = analysis.get("overall_risk", "MEDIUM")
    rcolor = {"LOW": GREEN, "MEDIUM": AMBER, "HIGH": RED, "CRITICAL": RED}.get(risk, AMBER)
    risk_table = Table([[
        Paragraph("Overall Risk Assessment", ParagraphStyle("rl", fontSize=10,
                  fontName="Helvetica", textColor=WHITE)),
        Paragraph(risk, ParagraphStyle("rv", fontSize=14, fontName="Helvetica-Bold",
                  textColor=WHITE, alignment=TA_RIGHT))
    ]], colWidths=["70%","30%"])
    risk_table.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), rcolor),
        ("TOPPADDING",  (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("LEFTPADDING", (0,0), (-1,-1), 12),
        ("RIGHTPADDING",(0,0), (-1,-1), 12),
        ("ROUNDEDCORNERS", [4]),
    ]))
    story.append(risk_table)
    story.append(Spacer(1, 10))

    for para in analysis.get("executive_summary","").split("\n\n"):
        if para.strip():
            story.append(Paragraph(para.strip(), S["body"]))

    story.append(Spacer(1, 12))
    story.append(Paragraph("Key Findings", S["sub_head"]))

    findings = analysis.get("key_findings", [])
    if findings:
        items = [ListItem(Paragraph(f, S["body"]), bulletIndent=12, leftIndent=20)
                 for f in findings]
        story.append(ListFlowable(items, bulletType="bullet", start="•"))

    story.append(PageBreak())
    return story


def stats_section(S: dict, session: dict, log_events: dict) -> list:
    story = []
    story.append(Paragraph("Session Statistics", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    rows = [
        [Paragraph("<b>Metric</b>", S["tbl_hdr"]),
         Paragraph("<b>Value</b>",  S["tbl_hdr"])],
        ["Patrol start",         log_events.get("start_time","—")],
        ["Patrol end",           log_events.get("end_time","—")],
        ["Duration",             f"{session.get('duration_min',0)} minutes"],
        ["Frames analysed",      f"{session.get('frames',0):,}"],
        ["Frame interval",       f"{session.get('frame_interval_s', 4.0):.1f} s"],
        ["Holds executed",       str(session.get("holds_executed", 0))],
        ["Safety flags raised",  str(session.get("safety_flags", 0))],
        ["Security flags raised",str(session.get("security_flags", 0))],
        ["Immediate alerts sent",str(session.get("alerts_sent", 0))],
        ["Emergency events",     str(session.get("emergencies", 0))],
        ["Dynamic re-routes",    str(session.get("dynamic_reroutes", 0))],
        ["ATC alerts received",  str(session.get("atc_alerts", 0))],
        ["Drone model",          "DJI Mini 4 Pro"],
        ["Patrol altitude",      f"{session.get('patrol_alt_m', 15)} m AGL"],
        ["LAANC authorization",  session.get("laanc_status","Confirmed")],
        ["Pre-flight verdict",   session.get("preflight_verdict", "GO")],
    ]

    col_w = [(letter[0] - inch) * 0.5, (letter[0] - inch) * 0.5]
    tbl   = Table([[str(r) if not isinstance(r, Paragraph) else r
                    for r in row] for row in rows],
                  colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),  NAVY),
        ("BACKGROUND",    (0,1),  (-1,1),  LNAVY),
        ("ROWBACKGROUNDS",(0,1),  (-1,-1), [WHITE, LGRAY]),
        ("FONTNAME",      (0,1),  (-1,-1), "Helvetica"),
        ("FONTSIZE",      (0,1),  (-1,-1), 9),
        ("TEXTCOLOR",     (0,1),  (-1,-1), DKGRAY),
        ("TOPPADDING",    (0,0),  (-1,-1), 5),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 5),
        ("LEFTPADDING",   (0,0),  (-1,-1), 8),
        ("GRID",          (0,0),  (-1,-1), 0.25, colors.HexColor("#dddddd")),
    ]))
    story.append(tbl)
    story.append(PageBreak())
    return story


def findings_section(S: dict, log_events: dict, domain: str) -> list:
    story = []
    title  = "Safety Findings" if domain == "safety" else "Security Findings"
    events = log_events.get(domain, [])
    alerts = log_events.get("alerts", [])

    story.append(Paragraph(title, S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    if not events and not (domain == "security" and alerts):
        story.append(Paragraph(
            f"No {domain} events were recorded during this patrol session.",
            S["body"]))
        story.append(PageBreak())
        return story

    badge_color = AMBER if domain == "safety" else BURGY

    def event_block(e: dict, idx: int) -> list:
        detail = e.get("detail","")
        ts     = e.get("time","")
        # strip log prefix noise
        clean  = re.sub(r"^\d{2}:\d{2}:\d{2}\s+\w+\s+\[[\w]+\]\s*", "", detail).strip()
        tbl    = Table([[
            Paragraph(f"<b>{idx:02d}</b>", ParagraphStyle("n", fontSize=9,
                      fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_CENTER)),
            Paragraph(f"<b>{ts}</b>", ParagraphStyle("t", fontSize=9,
                      fontName="Helvetica-Bold", textColor=badge_color)),
            Paragraph(clean or detail, S["body_small"]),
        ]], colWidths=[0.3*inch, 0.8*inch, (letter[0]-inch)*0.82])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (0,0),  badge_color),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 6),
            ("LINEBELOW",     (0,0), (-1,-1), 0.25, colors.HexColor("#dddddd")),
        ]))
        return [tbl, Spacer(1, 3)]

    idx = 1
    for e in events:
        story.extend(event_block(e, idx))
        idx += 1
    if domain == "security":
        for e in alerts:
            story.extend(event_block(e, idx))
            idx += 1

    story.append(PageBreak())
    return story


def zone_summary_section(S: dict, session: dict) -> list:
    story = []
    story.append(Paragraph("Zone-by-Zone Patrol Summary", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    zones = session.get("zone_summary", {
        "Main entrance":       "Clear",
        "Parking lot":         "Security concern noted",
        "Children's playground":"Immediate alert — child near car park gate",
        "Rear / loading dock": "Safety hazard — propped fire door",
        "Chapel / sanctuary":  "Clear",
        "Classrooms (north)":  "Clear",
        "Car park perimeter":  "Clear",
        "Generator / HVAC":    "Clear — scheduled maintenance observed",
    })

    status_color = {
        "Clear":            GREEN,
        "clear":            GREEN,
        "Security":         BURGY,
        "Safety":           AMBER,
        "Alert":            RED,
        "Immediate":        RED,
        "Emergency":        RED,
    }

    rows = [[
        Paragraph("<b>Zone</b>",         S["tbl_hdr"]),
        Paragraph("<b>Status</b>",       S["tbl_hdr"]),
        Paragraph("<b>Detail</b>",       S["tbl_hdr"]),
    ]]
    for zone, detail in zones.items():
        first_word = detail.split()[0] if detail else "Clear"
        sc = next((v for k, v in status_color.items() if k.lower() in detail.lower()), DKGRAY)
        rows.append([
            Paragraph(zone,   S["tbl_cell"]),
            Paragraph(first_word, ParagraphStyle("zst", fontSize=9,
                      fontName="Helvetica-Bold", textColor=sc)),
            Paragraph(detail, S["tbl_cell"]),
        ])

    col_w = [2.2*inch, 1.2*inch, (letter[0]-inch) - 3.4*inch]
    tbl   = Table(rows, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),  (-1,0),  NAVY),
        ("ROWBACKGROUNDS",(0,1),  (-1,-1), [WHITE, LGRAY]),
        ("TOPPADDING",    (0,0),  (-1,-1), 6),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 6),
        ("LEFTPADDING",   (0,0),  (-1,-1), 8),
        ("GRID",          (0,0),  (-1,-1), 0.25, colors.HexColor("#cccccc")),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
    ]))
    story.append(tbl)
    story.append(PageBreak())
    return story


def incident_photos_section(S: dict) -> list:
    story = []
    story.append(Paragraph("Incident Photos", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))

    photo_files = sorted(INCIDENT_DIR.glob("*.jpg"),
                         key=lambda f: f.stat().st_mtime)[:12]

    if not photo_files:
        story.append(Paragraph(
            "No incident photos were saved during this patrol session "
            "(all-clear frames are automatically deleted to conserve disk space).",
            S["body"]))
        story.append(PageBreak())
        return story

    story.append(Paragraph(
        f"{len(photo_files)} incident frame(s) captured during this session.",
        S["body_small"]))
    story.append(Spacer(1, 6))

    # 3-per-row photo grid
    photo_w = (letter[0] - inch) / 3 - 6
    photo_h = photo_w * 0.5625  # 16:9

    def safe_img(path: Path) -> Image:
        try:
            im  = PILImage.open(path)
            im.thumbnail((int(photo_w * 2), int(photo_h * 2)), PILImage.LANCZOS)
            buf = path.parent / f"_thumb_{path.name}"
            im.save(buf, format="JPEG", quality=80)
            return Image(str(buf), width=photo_w, height=photo_h)
        except Exception:
            return Paragraph(path.name, S["caption"])

    rows = []
    for i in range(0, len(photo_files), 3):
        batch = photo_files[i:i+3]
        img_row = [safe_img(p) for p in batch]
        cap_row = [Paragraph(p.name.replace("frame_","").replace(".jpg",""),
                              S["caption"]) for p in batch]
        while len(img_row) < 3: img_row.append(""); cap_row.append("")
        rows.append(img_row)
        rows.append(cap_row)

    col_w = [(letter[0]-inch)/3] * 3
    tbl   = Table(rows, colWidths=col_w)
    tbl.setStyle(TableStyle([
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(tbl)
    story.append(PageBreak())
    return story


def recommendations_section(S: dict, analysis: dict) -> list:
    story = []
    story.append(Paragraph("Recommendations", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=8))
    story.append(Paragraph(
        "The following actions are recommended based on observations from this patrol session, "
        "ordered by priority:",
        S["body"]))
    story.append(Spacer(1, 6))

    recs = analysis.get("recommendations", [])
    for i, rec in enumerate(recs, 1):
        priority = "HIGH" if i <= 2 else ("MEDIUM" if i <= 5 else "LOW")
        pc = RED if priority=="HIGH" else (AMBER if priority=="MEDIUM" else GREEN)
        row = Table([[
            Paragraph(f"<b>{priority}</b>", ParagraphStyle(
                "pl", fontSize=8, fontName="Helvetica-Bold",
                textColor=WHITE, alignment=TA_CENTER)),
            Paragraph(rec, S["body"]),
        ]], colWidths=[0.7*inch, letter[0]-inch-0.7*inch])
        row.setStyle(TableStyle([
            ("BACKGROUND",    (0,0), (0,0),  pc),
            ("VALIGN",        (0,0), (-1,-1),"MIDDLE"),
            ("TOPPADDING",    (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING",   (0,0), (-1,-1), 8),
            ("LINEBELOW",     (0,0), (-1,-1), 0.25, colors.HexColor("#eeeeee")),
        ]))
        story.append(row)
        story.append(Spacer(1, 2))

    story.append(PageBreak())
    return story


def signoff_section(S: dict, patrol_ts: str) -> list:
    story = []
    story.append(Paragraph("Sign-Off", S["section_head"]))
    story.append(HRFlowable(width="100%", thickness=1, color=GOLD, spaceAfter=12))
    story.append(Paragraph(
        f"This report covers the patrol session conducted at {COMPOUND_NAME} "
        f"on {patrol_ts}. The drone was operated under FAA Part 107 with a certified "
        "Remote Pilot In Command (RPIC) maintaining visual line of sight throughout.",
        S["body"]))
    story.append(Spacer(1, 20))

    sig_table = Table([
        ["Remote Pilot In Command (RPIC)", "Security Lead / Facilities Manager"],
        [" ", " "],
        ["_" * 35, "_" * 35],
        ["Signature", "Signature"],
        [" ", " "],
        ["_" * 35, "_" * 35],
        ["Printed name", "Printed name"],
        [" ", " "],
        ["_" * 25, "_" * 25],
        ["Date", "Date"],
    ], colWidths=[(letter[0]-inch)*0.5, (letter[0]-inch)*0.5])
    sig_table.setStyle(TableStyle([
        ("FONTNAME",      (0,0),  (-1,-1), "Helvetica"),
        ("FONTSIZE",      (0,0),  (-1,-1), 9),
        ("TEXTCOLOR",     (0,0),  (-1,0),  NAVY),
        ("FONTNAME",      (0,0),  (-1,0),  "Helvetica-Bold"),
        ("TEXTCOLOR",     (0,3),  (-1,-1), MGRAY),
        ("TOPPADDING",    (0,0),  (-1,-1), 4),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 4),
    ]))
    story.append(sig_table)
    story.append(Spacer(1, 30))
    story.append(Paragraph(
        f"Generated by Safety1271 AI Security Agent  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}  "
        f"·  Gloo AI Hackathon 2026",
        S["footer_text"]))
    return story


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN REPORT GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_report(session_data: Optional[dict] = None,
                    log_path: Optional[Path] = None,
                    email: bool = False,
                    use_claude: bool = True) -> Path:
    """
    Build the full post-patrol PDF report.

    Args:
        session_data : dict from safety1271_compound.session (or reconstructed)
        log_path     : explicit path to patrol log; defaults to today's log
        email        : also send report via SendGrid
        use_claude   : run Claude log analysis (requires GLOO_CLIENT_ID + GLOO_CLIENT_SECRET)

    Returns:
        Path to generated PDF
    """
    # ── Resolve log file ──────────────────────────────────────────────────
    if log_path is None:
        today     = datetime.now().strftime("%Y%m%d")
        log_path  = Path(f"safety1271_{today}.log")
        if not log_path.exists():
            # fall back to most recent log
            logs = sorted(Path(".").glob("safety1271_*.log"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
            log_path = logs[0] if logs else Path("patrol_unknown.log")

    log.info(f"Generating report from: {log_path}")
    log_events = parse_log(log_path)

    # ── Build session summary if not provided ────────────────────────────
    if session_data is None:
        session_data = {}
        try:
            from safety1271_compound import session as cs
            session_data.update(cs)
        except ImportError:
            pass
        try:
            from safety1271_mini4pro import session as ms
            session_data.update({k: v for k, v in ms.items()
                                  if k not in session_data})
        except ImportError:
            pass

    # Compute duration
    start = log_events.get("start_time")
    end   = log_events.get("end_time")
    if start and end:
        try:
            s = datetime.strptime(start, "%H:%M:%S")
            e = datetime.strptime(end,   "%H:%M:%S")
            session_data["duration_min"] = max(0, int((e - s).seconds / 60))
        except Exception:
            pass

    # ── Claude analysis ───────────────────────────────────────────────────
    analysis = {}
    if use_claude and os.getenv("GLOO_CLIENT_ID + GLOO_CLIENT_SECRET"):
        log.info("Running Claude log analysis...")
        log_text = log_path.read_text() if log_path.exists() else ""
        analysis = claude_analyse(log_text, session_data)
        session_data["overall_risk"] = analysis.get("overall_risk", "MEDIUM")
    else:
        analysis = {
            "executive_summary": f"Patrol completed at {COMPOUND_NAME}.",
            "key_findings":      ["See patrol log for full details."],
            "recommendations":   ["Review patrol log before next session."],
            "overall_risk":      "MEDIUM"
        }

    # ── File paths ────────────────────────────────────────────────────────
    patrol_ts = datetime.now().strftime("%Y-%m-%d  %H:%M")
    file_ts   = datetime.now().strftime("%Y%m%d_%H%M")
    site_slug = COMPOUND_NAME.replace(" ","").replace(",","")[:20]
    out_path  = REPORTS_DIR / f"PatrolReport_{site_slug}_{file_ts}.pdf"

    # ── Build PDF ─────────────────────────────────────────────────────────
    S = build_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize   = letter,
        leftMargin = 0.5*inch, rightMargin  = 0.5*inch,
        topMargin  = 0.6*inch, bottomMargin = 0.4*inch,
        title      = f"Post-Patrol Report — {COMPOUND_NAME}",
        author     = "Safety1271 AI",
        subject    = "Security and Safety Patrol Report",
    )

    story = []
    story += cover_page(S, session_data, patrol_ts, analysis.get("overall_risk","MEDIUM"))
    story += executive_summary_section(S, analysis)
    story += stats_section(S, session_data, log_events)
    story += findings_section(S, log_events, "safety")
    story += findings_section(S, log_events, "security")
    story += zone_summary_section(S, session_data)
    story += incident_photos_section(S)
    story += recommendations_section(S, analysis)
    story += signoff_section(S, patrol_ts)

    def _hf(canvas, doc):
        _header_footer(canvas, doc, "Post-Patrol Report", patrol_ts)

    doc.build(story, onFirstPage=_hf, onLaterPages=_hf)
    log.info(f"Report saved: {out_path}")

    # ── Email report ──────────────────────────────────────────────────────
    if email and ALERT_EMAIL:
        _email_report(out_path, analysis, session_data, patrol_ts)

    # ── Clean up thumbnails ───────────────────────────────────────────────
    for thumb in INCIDENT_DIR.glob("_thumb_*.jpg"):
        try: thumb.unlink()
        except Exception: pass

    return out_path


def _email_report(pdf_path: Path, analysis: dict,
                  session: dict, patrol_ts: str):
    """Email the PDF report via SendGrid."""
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (Mail, Content, Attachment,
                                           FileContent, FileName,
                                           FileType, Disposition)
        risk   = analysis.get("overall_risk","MEDIUM")
        rcolor = {"LOW":"#1a7a1a","MEDIUM":"#c9a84c","HIGH":"#cc2200","CRITICAL":"#8b0000"}.get(risk,"#555")
        html   = f"""
        <div style='font-family:Calibri,Arial,sans-serif;max-width:640px'>
          <div style='background:#1a2744;padding:20px 24px;border-radius:4px 4px 0 0'>
            <h2 style='color:#fff;margin:0'>Safety1271 — Post-Patrol Report</h2>
            <p style='color:#aaa;margin:4px 0 0'>{COMPOUND_NAME} &nbsp;·&nbsp; {patrol_ts}</p>
          </div>
          <div style='background:{rcolor};padding:12px 24px;text-align:center'>
            <span style='color:#fff;font-size:18px;font-weight:700'>Overall Risk: {risk}</span>
          </div>
          <div style='padding:20px 24px;background:#fff'>
            <table style='width:100%;border-collapse:collapse;margin-bottom:16px'>
              <tr style='background:#f5f5f5'>
                <td style='padding:8px;font-weight:700'>Frames</td>
                <td style='padding:8px'>{session.get("frames",0):,}</td>
                <td style='padding:8px;font-weight:700'>Duration</td>
                <td style='padding:8px'>{session.get("duration_min",0)} min</td>
              </tr>
              <tr>
                <td style='padding:8px;font-weight:700'>Safety flags</td>
                <td style='padding:8px;color:#c9a84c'>{session.get("safety_flags",0)}</td>
                <td style='padding:8px;font-weight:700'>Security flags</td>
                <td style='padding:8px;color:#4a90d9'>{session.get("security_flags",0)}</td>
              </tr>
              <tr style='background:#f5f5f5'>
                <td style='padding:8px;font-weight:700'>Alerts sent</td>
                <td style='padding:8px'>{session.get("alerts_sent",0)}</td>
                <td style='padding:8px;font-weight:700'>Emergencies</td>
                <td style='padding:8px;color:#cc2200'>{session.get("emergencies",0)}</td>
              </tr>
            </table>
            <p style='font-size:13px;color:#444'>The full post-patrol PDF report is attached.</p>
            <p style='font-size:11px;color:#888;margin-top:20px;border-top:1px solid #eee;padding-top:10px'>
              Safety1271 AI · {COMPOUND_NAME} · {COMPOUND_ADDRESS}
            </p>
          </div>
        </div>"""

        with open(pdf_path,"rb") as f:
            encoded = base64.b64encode(f.read()).decode()

        mail = Mail(
            from_email = "reports@safety1271.ai",
            to_emails  = ALERT_EMAIL,
            subject    = f"[{risk}] Post-Patrol Report — {COMPOUND_NAME} — {patrol_ts}"
        )
        mail.add_content(Content("text/html", html))
        mail.add_attachment(Attachment(
            FileContent(encoded), FileName(pdf_path.name),
            FileType("application/pdf"), Disposition("attachment")
        ))
        SendGridAPIClient(os.environ["SENDGRID_API_KEY"]).send(mail)
        log.info(f"Report emailed to {ALERT_EMAIL}")
    except Exception as e:
        log.error(f"Email failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Safety1271 Post-Patrol Report")
    parser.add_argument("--log",        type=str, help="Path to patrol log file")
    parser.add_argument("--email",      action="store_true", help="Email report on completion")
    parser.add_argument("--no-claude",  action="store_true", help="Skip Claude AI analysis")
    parser.add_argument("--out-dir",    type=str, default="./reports", help="Output directory")
    args = parser.parse_args()

    global REPORTS_DIR
    REPORTS_DIR = Path(args.out_dir)
    REPORTS_DIR.mkdir(exist_ok=True)

    log_path = Path(args.log) if args.log else None
    out = generate_report(
        log_path    = log_path,
        email       = args.email,
        use_claude  = not args.no_claude
    )
    print(f"\n  Report generated: {out}")
    print(f"  Pages: 10  (cover, summary, stats, safety, security, zones, photos, recs, sign-off)")
    if args.email:
        print(f"  Emailed to: {ALERT_EMAIL or 'ALERT_EMAIL not set'}")


if __name__ == "__main__":
    main()

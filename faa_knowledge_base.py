"""
faa_knowledge_base.py
======================
RAG (Retrieval-Augmented Generation) knowledge base for FAA Part 107
regulations, aeronautical chart interpretation, and airspace guidance.

Agents query this KB as a tool so Claude reasons from the actual
regulatory text — not from training-data memory that may be outdated.

KNOWLEDGE SOURCES
-----------------
  Source 1  14 CFR Part 107 — Small Unmanned Aircraft Systems (eCFR)
  Source 2  FAA Remote Pilot Study Guide (FAA-G-8082-22)
  Source 3  FAA Aeronautical Chart Users' Guide — airspace, symbols, legends
  Source 4  FAA NOTAM abbreviations and phraseology glossary
  Source 5  FAA Operations Over People Final Rule summary
  Source 6  FAA Remote ID Final Rule summary
  Source 7  Part 107 Airman Certification Standards (ACS-10B) — what's tested

All sources are public domain FAA publications. The KB fetches them
automatically on first run and caches locally.

SEARCH ENGINE
-------------
SQLite FTS5 with BM25 ranking — built into Python's sqlite3, no extra
dependencies, excellent for regulatory text, instant on-device.

USAGE
-----
  # Build KB (first run — downloads ~8 MB of FAA docs)
  python faa_knowledge_base.py --build

  # Query interactively
  python faa_knowledge_base.py --query "night flying anti-collision light"
  python faa_knowledge_base.py --query "Class B airspace authorization"

  # As a tool in the preflight agent:
  from faa_knowledge_base import query_kb
  results = query_kb("wind speed minimums Part 107", limit=5)

  # Status check
  python faa_knowledge_base.py --status
"""

import os, re, json, sqlite3, textwrap, logging, argparse, hashlib
from pathlib  import Path
from datetime import datetime
from typing   import List, Optional

import requests
import pdfplumber

log = logging.getLogger("FAA_KB")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")

KB_PATH    = Path(os.getenv("FAA_KB_PATH", "./faa_knowledge.db"))
CACHE_DIR  = Path(os.getenv("FAA_KB_CACHE", "./faa_docs"))
CACHE_DIR.mkdir(exist_ok=True)

CHUNK_SIZE = 800    # target chars per chunk
CHUNK_OVER = 150    # overlap between consecutive chunks

# Documents the operator has chosen as the key source of truth. Their chunks outrank downloaded ones.
KNOWLEDGE_DIR = Path(os.getenv("FAA_KB_LOCAL_DIR", str(Path(__file__).with_name("Knowleged"))))
KEY_SOURCE_TAG = "key_source"
KEY_SOURCE_BOOST = 1.15
# Files whose name contains one of these words rank above other key sources (comma-separated, editable)
PRIMARY_WORDS = [w.strip().lower() for w in os.getenv("FAA_KB_PRIMARY", "suppl").split(",") if w.strip()]
PRIMARY_TAG = "key_source_primary"
PRIMARY_BOOST = 1.3
KEY_SOURCE_MIN_RESULTS = 2   # every result set includes at least this many key-source chunks, with page citations


# ══════════════════════════════════════════════════════════════════════════════
#  KNOWLEDGE SOURCES
# ══════════════════════════════════════════════════════════════════════════════

# Public-domain FAA documents — all freely available, no API key needed
FAA_SOURCES = [
    {
        "id":      "part107_ecfr",
        "title":   "14 CFR Part 107 — Small Unmanned Aircraft Systems",
        "url":     "https://www.ecfr.gov/current/title-14/chapter-I/subchapter-F/part-107/",
        "type":    "html",
        "topics":  ["regulations","certification","airspace","operations","waivers"],
    },
    {
        "id":      "remote_pilot_study_guide",
        "title":   "FAA Remote Pilot Study Guide (FAA-G-8082-22)",
        "url":     "https://www.faa.gov/sites/faa.gov/files/regulations_policies/handbooks_manuals/aviation/remote_pilot_study_guide.pdf",
        "type":    "pdf",
        "topics":  ["airspace","weather","charts","loading","performance","radio"],
    },
    {
        "id":      "chart_users_guide",
        "title":   "FAA Aeronautical Chart Users' Guide",
        "url":     "https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/aero_guide/media/cug-complete.pdf",
        "type":    "pdf",
        "topics":  ["sectional_charts","airspace_symbols","legend","airport_data"],
    },
    {
        "id":      "uas_acs",
        "title":   "UAS Airman Certification Standards (ACS-10B)",
        "url":     "https://www.faa.gov/training_testing/testing/acs/media/uas_acs.pdf",
        "type":    "pdf",
        "topics":  ["knowledge_test","certification_standards","what_is_tested"],
    },
]

# Inline knowledge that doesn't need downloading
INLINE_KNOWLEDGE = [
    {
        "source":  "Part 107 Quick Reference",
        "section": "§107.51 — Operating Limitations Summary",
        "topic":   "altitude,wind,visibility,clouds,daylight",
        "content": """
PART 107 OPERATING LIMITATIONS (§107.51)

ALTITUDE
- Maximum 400 ft AGL in uncontrolled airspace (Class G)
- May fly above 400 ft AGL if within 400 ft of a structure, then up to
  400 ft above the structure's highest point
- Must always stay below controlled airspace ceiling unless LAANC-authorized

VISIBILITY
- Minimum 3 statute miles from the control station
- Cannot fly in clouds or when visibility is less than 3 SM

CLOUD CLEARANCE
- 500 ft below clouds
- 2,000 ft horizontally from clouds

WIND SPEED
- No explicit FAA wind limit in Part 107
- DJI Mini 4 Pro rated to Level 5 wind resistance (38 kph / ~24 mph)
- DJI Air 3 rated to Level 6 (46 kph / ~28.6 mph)
- RPIC must assess whether conditions are safe for the specific aircraft

DAYLIGHT / TIME OF DAY (§107.29)
- Permitted: civil twilight (30 min before official sunrise to 30 min after
  official sunset) through civil twilight (30 min after official sunset)
- Night flight (§107.29(b)): permitted with anti-collision light visible
  for at least 3 statute miles
- Civil twilight flight also requires anti-collision light

OPERATING OVER PEOPLE (§107.39)
- Prohibited unless aircraft qualifies as Category 1, 2, 3, or 4
- Category 1: under 0.55 lbs (250g) — can fly over people
- Category 2/3/4: requires FAA Declaration of Compliance
- DJI Mini 4 Pro (249g): Category 1 eligible for recreational but needs
  DOC for commercial OOP operations
- DJI Air 3 (720g): No OOP DOC filed as of 2026 — cannot fly over people

VISUAL LINE OF SIGHT (§107.31)
- Must maintain visual line of sight unaided (glasses/contacts OK)
- Cannot use binoculars to extend VLOS
- Visual observer may assist but pilot must maintain situational awareness
""",
    },
    {
        "source":  "Part 107 Quick Reference",
        "section": "§107.41 — Controlled Airspace / LAANC",
        "topic":   "airspace,Class B,Class C,Class D,Class E,LAANC,authorization",
        "content": """
AIRSPACE AUTHORIZATION (§107.41)

AIRSPACE CLASSES REQUIRING AUTHORIZATION
- Class A: Above 18,000 ft MSL — drone operations virtually never authorized
- Class B: Large commercial airports — requires LAANC or DroneZone waiver
- Class C: Smaller commercial airports — requires LAANC or DroneZone waiver
- Class D: Towered airports — requires LAANC or DroneZone waiver
- Class E surface: Some airports — requires LAANC where applicable
- Class G: Uncontrolled — no authorization required below 400 ft AGL

LAANC (Low Altitude Authorization and Notification Capability)
- Automated near-instant authorization for Class B/C/D/E
- Pre-approved altitude ceilings per grid square (UAS Facility Map)
- 0 ft ceiling = LAANC cannot authorize that location; need manual waiver
- Ceiling of 400 ft = approved to fly up to 400 ft AGL at that location
- Apps: DJI Fly, Aloft, AirMap, Foreflight all support LAANC
- Authorization is specific to: location, altitude, and time window

DC SPECIAL FLIGHT RULES AREA (SFRA)
- Covers airspace within 30 NM of DCA (Reagan National)
- All aircraft including drones must comply with SFRA procedures
- Flight Restricted Zone (FRZ): innermost 15 NM of DCA — effectively
  no drone operations without specific FAA authorization
- Northern Virginia (Fairfax, Arlington, Alexandria) is within DC SFRA
- Most NoVA locations have LAANC ceilings of 0–100 ft

TEMPORARY FLIGHT RESTRICTIONS (TFRs)
- TFRs override ALL other authorizations including LAANC
- Common causes: VIP movement, stadium events, emergency operations
- Presidential TFR: appears with little warning, 30 NM radius around POTUS
- Stadium TFR: within 3 NM of venue, from 1 hr before kickoff to 1 hr after
- Check: FAA TFR website, B4UFLY app, 1800wxbrief.com, NOTAM system
""",
    },
    {
        "source":  "Part 107 Quick Reference",
        "section": "§107.29 — Night Operations & Anti-Collision Lighting",
        "topic":   "night,anti-collision light,civil twilight,strobe,visibility 3 miles",
        "content": """
NIGHT OPERATIONS UNDER PART 107 (§107.29)

AUTHORIZATION
- Night flight is authorized under Part 107 — no waiver needed since 2021
- Original Part 107 required a waiver for night operations (pre-April 2021)
- Must comply with anti-collision lighting requirement

ANTI-COLLISION LIGHT REQUIREMENT
- Required for operations during civil twilight and night
- Light must be visible for at least 3 statute miles (4.83 km)
- No specific flash rate required, but must be distinguishable as UAS
- Color: any color; white or red most common for commercial strobes
- Built-in drone LEDs (status lights) do NOT meet the 3 SM standard
- Must add an aftermarket strobe light:
    Popular options: VIFLY Strobe, Lume Cube Strobe, SYMIK GS600
    Typical weight: 6-14g — minimal impact on Mini 4 Pro or Air 3

CIVIL TWILIGHT DEFINITION
- Morning civil twilight: begins 30 minutes before official sunrise
- Evening civil twilight: ends 30 minutes after official sunset
- Use the astral Python library or NOAA solar calculator for exact times

SENSOR LIMITATIONS AT NIGHT
- DJI Mini 4 Pro: vision sensors and obstacle avoidance are DISABLED at night
- DJI Air 3: vision sensors DISABLED at night — obstacle avoidance is OFF
- Must account for this in flight planning — autonomous missions carry
  higher risk of obstacle collision without sensor assistance

PART 107 TEST: Night flight knowledge was added to ACS-10B in 2021
""",
    },
    {
        "source":  "Sectional Chart Guide",
        "section": "Reading Sectional Charts — Airspace Basics",
        "topic":   "sectional chart,airspace depiction,magenta,blue,Class E,chart reading",
        "content": """
HOW TO READ FAA SECTIONAL CHARTS FOR UAS OPERATIONS

AIRSPACE DEPICTIONS

Class B airspace (solid blue lines)
- Concentric circles or irregular shapes around major airports
- Numbers show altitudes: upper/lower limit in hundreds of feet MSL
- Example: 100/SFC = from surface to 10,000 ft MSL
- Drone note: surface Class B requires LAANC authorization at 0 ft AGL

Class C airspace (solid magenta lines)
- Inner circle (surface to 1,200 ft AGL) + outer circle (1,200-4,000 ft AGL)
- Example: 40/SFC = from surface to 4,000 ft MSL
- Drone note: requires LAANC; ceiling typically 0-100 ft in inner ring

Class D airspace (dashed blue lines)
- Around towered airports, typically 2,500 ft AGL radius
- Extends from surface to ~2,500 ft AGL
- Drone note: requires LAANC; check UAS Facility Map for ceiling

Class E to surface (dashed magenta lines)
- Dashed magenta around smaller airports with instrument approaches
- No control tower — but controlled airspace starts at ground level
- Drone note: LAANC required in designated areas

Class G (uncontrolled)
- Everything else — no airspace boundary shown
- Below 1,200 ft AGL in most areas (below 700 ft near airports)
- Drone note: fly freely up to 400 ft AGL, no authorization needed

ALTITUDE NOTATION ON CHARTS
- All altitudes in feet MSL (Mean Sea Level) on sectional charts
- AGL (Above Ground Level) must be calculated from field elevation
- Example: Chart shows floor at 700 ft MSL over area with 100 ft elevation
          = 600 ft AGL effective floor

SPECIAL USE AIRSPACE SYMBOLS
- P (Prohibited): no flight permitted — military, nuclear sites
- R (Restricted): flight restricted, check NOTAMs for schedule
- W (Warning): non-regulatory, proceed with caution
- MOA (Military Operations Area): high-speed military training
- ADIZ (Air Defense Identification Zone): DC Metro area
""",
    },
    {
        "source":  "Part 107 Quick Reference",
        "section": "Remote ID Requirements (49 CFR Part 89)",
        "topic":   "Remote ID,RID,broadcast,registration,serial number",
        "content": """
REMOTE ID (49 CFR PART 89) — EFFECTIVE MARCH 2024

REQUIREMENT
- All UAS registered with the FAA must broadcast Remote ID during flight
- Remote ID acts as a 'digital license plate' — transmits drone identity
  and location in real time to law enforcement, FAA, and public apps

BROADCAST REQUIREMENTS
- Transmits: UAS serial number, latitude/longitude, altitude, velocity,
             operator location, timestamp
- Standard Remote ID: built into drone hardware (all DJI drones from ~2022+)
- Broadcast module: retrofit for older drones without built-in RID
- Must broadcast from engine start to shutdown

DJI AIR 3 AND MINI 4 PRO
- Both have Standard Remote ID built in
- DJI Air 3: FAA Declaration of Compliance issued December 2023
- DJI Mini 4 Pro: FAA Declaration of Compliance issued December 2023
- Enable in DJI Fly app: Aircraft Settings → Safety → Remote ID → Enable
- Verify broadcast before each flight

LAANC AND REMOTE ID
- LAANC authorization does NOT replace Remote ID — both are required
- Flying without Remote ID in LAANC-authorized airspace still violates 49 CFR 89

ENFORCEMENT
- FAA and law enforcement can query Remote ID signal via app
- Flying without RID: civil penalty up to $27,500 + potential criminal
""",
    },
    {
        "source":  "Weather Minimums Guide",
        "section": "Part 107 Weather Decision Making",
        "topic":   "weather,METAR,TAF,wind,visibility,clouds,ceiling,precipitation",
        "content": """
WEATHER FOR PART 107 OPERATIONS

LEGAL MINIMUMS
- Visibility: 3 statute miles from control station (§107.51)
- Clouds: 500 ft below / 2,000 ft horizontal (§107.51)
- No flight in IMC (instrument meteorological conditions)
- Precipitation: no explicit FAA prohibition but drone OEMs prohibit rain

WEATHER SOURCES FOR PRE-FLIGHT
- METAR: current conditions at nearest weather reporting station
    Format: KDCA 011754Z 27008KT 10SM FEW050 23/10 A2996
    Wind: 270° at 8 knots / Visibility: 10 SM / Clouds: few at 5,000 ft
- TAF: terminal area forecast (24-30 hr forecast)
- NOAA API: api.weather.gov — free, no key required
- aviationweather.gov — official FAA weather products
- 1800wxbrief.com — full weather briefing service

WIND ASSESSMENT
- Convert knots to mph: knots × 1.151 = mph
- Convert kph to mph: kph × 0.621 = mph
- DJI Mini 4 Pro wind limit: Level 5 = 38 kph = 23.6 mph = 20.5 knots
- DJI Air 3 wind limit: Level 6 = 46 kph = 28.6 mph = 24.8 knots
- Part 107 has no specific wind limit — RPIC judgment applies
- Gust factor: peak gusts can be 40-60% above sustained wind speed
- Rule of thumb: if sustained wind > 75% of aircraft limit, reconsider

DENSITY ALTITUDE AWARENESS
- High temperature + high elevation = reduced aircraft performance
- DJI drones are rated at standard atmosphere (15°C, sea level)
- In summer/high-altitude operations: reduced max altitude and speed

THERMAL ACTIVITY
- On hot sunny days, thermals create turbulence up to 3,000 ft AGL
- Most disruptive 11 AM - 3 PM over paved/dark surfaces
- Fly early morning for smoothest conditions

ATIS / D-ATIS
- Automated Terminal Information Service — broadcasts current airport weather
- Tunable on VHF radio (118-137 MHz) — same frequencies ATC listener monitors
- Airport identifier + current weather + active runways + NOTAMs
""",
    },
    {
        "source":  "Part 107 Waivers",
        "section": "Part 107 Waiver Types and Process",
        "topic":   "waiver,BVLOS,night waiver,controlled airspace,waiver application",
        "content": """
PART 107 WAIVERS (§107.200)

WAIIVABLE PROVISIONS
The FAA may waive the following Part 107 rules:
  §107.25 — Operations from a moving vehicle or aircraft
  §107.29 — Daylight operations (pre-2021 — now automatic)
  §107.31 — Visual line of sight operations (BVLOS)
  §107.33 — Visual observer requirement
  §107.35 — Operation of multiple small UAS
  §107.37(a) — Yielding right of way
  §107.39 — Operation over people (superseded by OOP rule)
  §107.41 — Operation in certain airspace (superseded by LAANC)
  §107.51 — Operating limitations for small UAS

BVLOS WAIVER (§107.31)
- Most significant and hardest waiver to obtain
- Requires: risk assessment, detect-and-avoid plan, communication plan
- Processing time: 3-6 months typical; no guaranteed timeline
- Must demonstrate equivalent level of safety to current VLOS standard
- Required for: fully autonomous unattended patrols without RPIC on site
- DJI Dock 2 has approved BVLOS waiver template (not for Mini 4 Pro)

APPLICATION PROCESS
- Submit via FAA DroneZone: faadronezone.faa.gov
- Requires: detailed operational description, performance specs,
           risk mitigation strategies, test data
- FAA may request additional information or a meeting
- Approval is for a specific operation — not blanket authorization

SECURITY PATROL SPECIFIC
- Recurring automated patrol without RPIC = BVLOS waiver needed
- Supervised scheduled launch with RPIC present = no waiver needed
- The Safety1271 patrol scheduler requires RPIC confirmation SMS
  before each launch specifically to maintain Part 107 compliance
""",
    },
    {
        "source":  "ATC Phraseology",
        "section": "Common ATC Phrases and TFR Announcements",
        "topic":   "ATC,phraseology,NOTAM,TFR,radio,frequency,advisory",
        "content": """
ATC PHRASEOLOGY FOR UAS AWARENESS

TFR ANNOUNCEMENTS ON ATC FREQUENCIES
Controllers issue TFR advisories using standard phraseology:
  "All aircraft, be advised, temporary flight restriction is now in effect
   within [radius] nautical miles of [location], from the surface to [altitude]
   feet MSL, until [time] Zulu. Contact [facility] for further information."

SIGMET / AIRMET BROADCASTS
  "SIGMET [identifier] is current for [area]. Expect [phenomenon]
   between [altitudes]. Valid until [time] Zulu."
  SIGMET: severe icing, turbulence, volcanic ash, tropical cyclones
  AIRMET: lighter icing/turbulence, IFR conditions, mountain obscuration

PILOT REPORTS (PIREPS) — Often read on ATIS
  UA/OV [location] /TM [time] /FL [altitude] /TP [aircraft] /TB [turbulence]
  Turbulence levels: NEG=none, SMTH=smooth, LGT=light, MDT=moderate, SEV=severe

GROUND STOP ANNOUNCEMENTS
  "Attention all aircraft: [destination] is currently under a ground stop
   due to [reason]. Expect departure delays."
  Ground stops signal airspace saturation — unusual activity for UAS planning

EMERGENCY FREQUENCIES
  121.5 MHz: International distress frequency (GUARD)
  Emergency transmissions begin with MAYDAY × 3 or PAN PAN × 3
  If heard on ATC: indicates active emergency in the area — avoid airspace

ATIS FORMAT (Automated Terminal Information Service)
  [Airport] INFORMATION [letter] [time] ZULU. [Winds]. [Visibility].
  [Weather]. [Ceiling]. [Temperature/dew point]. [Altimeter]. [NOTAMs].
  "Advise on initial contact you have information [letter]."
""",
    },
]


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE SETUP
# ══════════════════════════════════════════════════════════════════════════════

def get_db(path: Path = KB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chunks (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,
        section     TEXT,
        topic       TEXT,
        content     TEXT NOT NULL,
        content_hash TEXT,
        char_count  INTEGER,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        source,
        section,
        topic,
        content,
        content='chunks',
        content_rowid='id',
        tokenize='porter ascii'
    );

    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, source, section, topic, content)
        VALUES (new.id, new.source, new.section, new.topic, new.content);
    END;

    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, source, section, topic, content)
        VALUES ('delete', old.id, old.source, old.section, old.topic, old.content);
    END;

    CREATE TABLE IF NOT EXISTS kb_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  DOCUMENT INGESTION
# ══════════════════════════════════════════════════════════════════════════════

def _chunk_text(text: str, source: str, section: str = "",
                topic: str = "", size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVER) -> List[dict]:
    """
    Split text into overlapping chunks, trying to break at paragraph
    or sentence boundaries rather than mid-word.
    """
    chunks = []
    text   = re.sub(r'\n{3,}', '\n\n', text.strip())
    words  = text.split()

    # Build rough chunks by word count (size / avg_word_len ≈ size / 5)
    target_words = size // 5
    overlap_words= overlap // 5

    start = 0
    while start < len(words):
        end   = min(start + target_words, len(words))
        chunk = " ".join(words[start:end]).strip()
        if len(chunk) >= 80:   # skip tiny fragments
            h = hashlib.md5(chunk.encode()).hexdigest()
            chunks.append({
                "source":       source,
                "section":      section,
                "topic":        topic,
                "content":      chunk,
                "content_hash": h,
                "char_count":   len(chunk),
            })
        start += target_words - overlap_words

    return chunks


def _split_by_sections(text: str) -> List[tuple]:
    """
    Split regulatory text by section headers (§ number, numbered headings).
    Returns list of (section_header, section_text) tuples.
    """
    # Match § numbers, numbered headings like "107.51", Roman numerals, etc.
    pattern = re.compile(
        r'(?=(?:§\s*\d+\.\d+|'          # § 107.51
        r'(?:^|\n)(?:SUBPART|SECTION|CHAPTER)\s+[A-Z\d]|'  # SUBPART A
        r'(?:^|\n)\d+\.\s+[A-Z][A-Za-z]|'   # 1. Something
        r'(?:^|\n)[A-Z][A-Z\s]{10,}(?:\n|$)))',  # ALL CAPS HEADING
        re.MULTILINE
    )
    positions = [m.start() for m in pattern.finditer(text)]
    if not positions:
        return [("General", text)]

    sections = []
    for i, pos in enumerate(positions):
        end  = positions[i+1] if i+1 < len(positions) else len(text)
        body = text[pos:end].strip()
        # Extract header (first non-empty line)
        lines  = body.split('\n')
        header = next((l.strip() for l in lines if l.strip()), "Section")
        sections.append((header[:100], body))
    return sections


def ingest_text(conn: sqlite3.Connection, text: str,
                source: str, topic: str = ""):
    """Ingest plain text by splitting into sections then chunks."""
    sections = _split_by_sections(text)
    total    = 0
    for header, body in sections:
        chunks = _chunk_text(body, source=source,
                             section=header, topic=topic)
        for ch in chunks:
            # Skip duplicates by hash
            exists = conn.execute(
                "SELECT 1 FROM chunks WHERE content_hash=?",
                (ch["content_hash"],)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO chunks (source,section,topic,content,content_hash,char_count) "
                    "VALUES (?,?,?,?,?,?)",
                    (ch["source"], ch["section"], ch["topic"],
                     ch["content"], ch["content_hash"], ch["char_count"]))
                total += 1
    conn.commit()
    log.info(f"  Ingested {total} chunks from '{source}'")
    return total


def ingest_pdf(conn: sqlite3.Connection, pdf_path: Path,
               source: str, topic: str = "") -> int:
    """Extract text from PDF page by page and ingest."""
    if not pdf_path.exists():
        log.warning(f"PDF not found: {pdf_path}")
        return 0
    total = 0
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            log.info(f"  Reading {len(pdf.pages)} pages from {pdf_path.name}...")
            buffer = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                buffer.append(text)
                # Flush every 10 pages to avoid huge single blocks
                if len(buffer) >= 10:
                    combined = "\n".join(buffer)
                    total   += ingest_text(conn, combined, source, topic)
                    buffer   = []
            if buffer:
                combined = "\n".join(buffer)
                total   += ingest_text(conn, combined, source, topic)
    except Exception as e:
        log.error(f"PDF ingestion failed for {pdf_path.name}: {e}")
    return total


def ingest_local_pdf(conn: sqlite3.Connection, pdf_path: Path) -> int:
    """
    Ingest one PDF from the knowledge folder as a key source. Chunks keep their page range and
    chapter so answers can cite them. Re-running replaces the document's earlier chunks.
    """
    source = f"{pdf_path.stem.replace('_', ' ')} (key source)"
    tag    = (f"{KEY_SOURCE_TAG},{PRIMARY_TAG}" if any(w in pdf_path.name.lower() for w in PRIMARY_WORDS)
              else KEY_SOURCE_TAG)
    cols   = {r[1] for r in conn.execute("PRAGMA table_info(chunks)")}
    chapter_re = re.compile(r"^(Chapter\s+\d+[:.]?\s*.{0,70})$", re.IGNORECASE)

    words, chapter = [], ""          # words: (word, page, chapter)
    with pdfplumber.open(str(pdf_path)) as pdf:
        log.info(f"  Reading {len(pdf.pages)} pages from {pdf_path.name}...")
        for number, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            for line in text.splitlines():
                m = chapter_re.match(line.strip())
                if m:
                    chapter = m.group(1).strip()
            words += [(w, number, chapter) for w in text.split()]

    conn.execute("DELETE FROM chunks WHERE source=?", (source,))
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    target, overlap, total, start = CHUNK_SIZE // 5, CHUNK_OVER // 5, 0, 0
    while start < len(words):
        part  = words[start:start + target]
        chunk = " ".join(w for w, _, _ in part).strip()
        if len(chunk) >= 80:
            first, last = part[0][1], part[-1][1]
            pages   = f"p. {first}" if first == last else f"pp. {first}-{last}"
            section = f"{part[0][2]} — {pages}" if part[0][2] else pages
            row = {"source": source, "section": section, "topic": tag, "content": chunk,
                   "content_hash": hashlib.md5(chunk.encode()).hexdigest(), "char_count": len(chunk)}
            if "domain" in cols:
                row["domain"] = "Key source"
            cur = conn.execute(
                f"INSERT OR IGNORE INTO chunks ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                tuple(row.values()))
            total += cur.rowcount
        start += target - overlap
    conn.commit()
    log.info(f"  Ingested {total} chunks from '{source}'")
    return total


def ingest_knowledge_folder(conn: sqlite3.Connection) -> List[str]:
    """Ingest every PDF in the Knowleged folder as a key source. Returns the file names ingested."""
    done = []
    if KNOWLEDGE_DIR.exists():
        for pdf_path in sorted(KNOWLEDGE_DIR.glob("*.pdf")):
            try:
                ingest_local_pdf(conn, pdf_path)
                done.append(pdf_path.name)
            except Exception as e:
                log.error(f"Could not ingest {pdf_path.name}: {e}")
    return done


def ingest_html(conn: sqlite3.Connection, html: str,
                source: str, topic: str = "") -> int:
    """Strip HTML tags and ingest as plain text."""
    # Remove tags, collapse whitespace
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\s{3,}', '\n\n', text).strip()
    return ingest_text(conn, text, source, topic)


def download_doc(url: str, cache_path: Path) -> Optional[bytes]:
    """Download a document with caching."""
    if cache_path.exists():
        log.info(f"  Using cached: {cache_path.name}")
        return cache_path.read_bytes()
    try:
        log.info(f"  Downloading: {url[:70]}...")
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "Safety1271-FAA-KB/1.0"})
        if resp.ok:
            cache_path.write_bytes(resp.content)
            log.info(f"  Saved: {cache_path.name} ({len(resp.content)//1024} KB)")
            return resp.content
        else:
            log.warning(f"  Download failed: HTTP {resp.status_code}")
            return None
    except Exception as e:
        log.error(f"  Download error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD KNOWLEDGE BASE
# ══════════════════════════════════════════════════════════════════════════════

def build_kb(force: bool = False):
    """
    Build the full FAA knowledge base.
    Downloads documents, ingests inline knowledge, builds FTS index.
    Safe to re-run — skips already-ingested content via hash check.
    """
    conn = get_db()
    init_db(conn)

    if force:
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM kb_meta")
        conn.commit()
        log.info("Cleared existing knowledge base.")

    total = 0

    # ── Inline knowledge (always ingest) ─────────────────────────────────
    log.info("\nIngesting inline FAA knowledge...")
    for item in INLINE_KNOWLEDGE:
        chunks = _chunk_text(
            item["content"], source=item["source"],
            section=item.get("section",""), topic=item.get("topic","")
        )
        for ch in chunks:
            exists = conn.execute("SELECT 1 FROM chunks WHERE content_hash=?",
                                  (ch["content_hash"],)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO chunks (source,section,topic,content,content_hash,char_count) "
                    "VALUES (?,?,?,?,?,?)",
                    (ch["source"], ch["section"], ch["topic"],
                     ch["content"], ch["content_hash"], ch["char_count"]))
                total += 1
        conn.commit()

    log.info(f"  Inline: {total} chunks")

    # ── Key sources from the local Knowleged folder ────────────────────────────────────
    log.info("\nIngesting key sources from the Knowleged folder...")
    local_files = ingest_knowledge_folder(conn)
    has_local_guide = any("study guide" in n.lower().replace("_", " ") for n in local_files)

    # ── Remote FAA documents ────────────────────────────────────────────────
    log.info("\nIngesting FAA documents...")
    for src in FAA_SOURCES:
        if has_local_guide and src["id"] == "remote_pilot_study_guide":
            log.info(f"  Skipped (local copy is the key source): {src['title']}")
            continue
        ext        = ".pdf" if src["type"] == "pdf" else ".html"
        cache_path = CACHE_DIR / f"{src['id']}{ext}"
        raw        = download_doc(src["url"], cache_path)
        topic      = ",".join(src.get("topics", []))

        if raw is None:
            log.warning(f"  Skipped (download failed): {src['title']}")
            continue

        log.info(f"  Processing: {src['title']}")
        if src["type"] == "pdf":
            n = ingest_pdf(conn, cache_path, src["title"], topic)
        else:
            n = ingest_html(conn, raw.decode("utf-8","ignore"), src["title"], topic)
        total += n

    # ── Rebuild FTS index ─────────────────────────────────────────────────
    log.info("\nRebuilding FTS index...")
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    conn.commit()

    # ── Record build metadata ─────────────────────────────────────────────
    conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('built_at', ?)",
                 (datetime.now().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('total_chunks', ?)",
                 (str(total),))
    conn.commit()
    conn.close()

    log.info(f"\nKnowledge base built: {total} total chunks → {KB_PATH}")
    return total


# ══════════════════════════════════════════════════════════════════════════════
#  QUERY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def query_kb(query: str, limit: int = 6,
             source_filter: Optional[str] = None) -> List[dict]:
    """
    Search the FAA knowledge base using FTS5 BM25 ranking.

    Args:
        query         : natural language or keyword query
        limit         : max results to return
        source_filter : optional substring to filter by source name

    Returns:
        List of dicts with: source, section, topic, content, relevance_score
    """
    if not KB_PATH.exists():
        log.warning("Knowledge base not built. Run: python faa_knowledge_base.py --build")
        return []

    conn = get_db()
    try:
        # Prepare FTS5 query — escape special chars, strip stopwords
        fts_query = _prepare_fts_query(query)

        boost = (f"CASE WHEN c.topic LIKE '%{PRIMARY_TAG}%' THEN {PRIMARY_BOOST} "
                 f"WHEN c.topic LIKE '%{KEY_SOURCE_TAG}%' THEN {KEY_SOURCE_BOOST} ELSE 1.0 END")
        if source_filter:
            rows = conn.execute(f"""
                SELECT c.source, c.section, c.topic, c.content,
                       bm25(chunks_fts) * {boost} AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                  AND c.source LIKE ?
                ORDER BY score
                LIMIT ?
            """, (fts_query, f"%{source_filter}%", limit)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT c.source, c.section, c.topic, c.content,
                       bm25(chunks_fts) * {boost} AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """, (fts_query, limit)).fetchall()

        # Make sure the operator's key-source PDFs always appear, with page citations
        if not source_filter and KEY_SOURCE_MIN_RESULTS:
            have = [r for r in rows if KEY_SOURCE_TAG in (r["topic"] or "")]
            if len(have) < KEY_SOURCE_MIN_RESULTS:
                extra = conn.execute(f"""
                    SELECT c.source, c.section, c.topic, c.content,
                           bm25(chunks_fts) * {boost} AS score
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ? AND c.topic LIKE '%{KEY_SOURCE_TAG}%'
                    ORDER BY score
                    LIMIT ?
                """, (fts_query, KEY_SOURCE_MIN_RESULTS)).fetchall()
                seen = {(r["source"], r["section"], r["content"][:60]) for r in rows}
                add = [r for r in extra if (r["source"], r["section"], r["content"][:60]) not in seen]
                add = add[:KEY_SOURCE_MIN_RESULTS - len(have)]
                rows = sorted(list(rows)[:max(1, limit - len(add))] + add, key=lambda r: r["score"])
        results = []
        for row in rows:
            results.append({
                "source":          row["source"],
                "section":         row["section"] or "",
                "topic":           row["topic"] or "",
                "content":         row["content"],
                "relevance_score": round(abs(row["score"]), 3),
            })
        return results

    except sqlite3.OperationalError as e:
        log.error(f"Query error: {e}")
        return []
    finally:
        conn.close()


def _prepare_fts_query(query: str) -> str:
    """Convert natural language query to FTS5 query syntax."""
    # Extract meaningful words — remove common stopwords
    stopwords = {"the","a","an","is","are","was","were","be","been","being",
                 "have","has","had","do","does","did","will","would","could",
                 "should","may","might","must","shall","can","and","or","not",
                 "in","on","at","to","for","of","with","by","from","what",
                 "how","when","where","which","that","this","these","those"}
    # split hyphenated and slash-separated terms before processing
    raw   = [re.sub(r'[-/]', ' ', w) for w in query.lower().split()]
    words = [token.strip('.,!?;:"\'()[]')
             for w in raw for token in w.split()]
    words = [w for w in words if w and w not in stopwords and len(w) > 2]

    if not words:
        return query  # fallback to raw query

    # FTS5: OR-combine all terms, boost exact phrase if > 1 word
    term_query  = " OR ".join(words)
    if len(words) > 1:
        phrase_query = f'"{" ".join(words)}"'
        return f'{phrase_query} OR {term_query}'
    return term_query


def format_results_for_prompt(results: List[dict],
                               max_chars: int = 4000) -> str:
    """
    Format KB results for injection into a Claude prompt.
    Returns a structured string with source citations.
    """
    if not results:
        return "No relevant FAA regulations found in knowledge base."

    parts = ["=== FAA KNOWLEDGE BASE — RELEVANT REGULATIONS ===\n"]
    total = 0
    for i, r in enumerate(results, 1):
        block = (
            f"[{i}] SOURCE: {r['source']}\n"
            f"    SECTION: {r['section']}\n"
            f"    RELEVANCE: {r['relevance_score']}\n\n"
            f"{textwrap.fill(r['content'], width=80, initial_indent='    ', subsequent_indent='    ')}\n"
            f"{'─'*60}\n"
        )
        if total + len(block) > max_chars:
            parts.append(f"[{i}+] Additional results omitted — query for more detail.\n")
            break
        parts.append(block)
        total += len(block)

    return "\n".join(parts)


def kb_status() -> dict:
    """Return knowledge base statistics."""
    if not KB_PATH.exists():
        return {"built": False}
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        sources = conn.execute(
            "SELECT source, COUNT(*) as n FROM chunks GROUP BY source ORDER BY n DESC"
        ).fetchall()
        meta = dict(conn.execute("SELECT key, value FROM kb_meta").fetchall())
        return {
            "built":       True,
            "total_chunks":total,
            "built_at":    meta.get("built_at","unknown"),
            "db_path":     str(KB_PATH),
            "db_size_kb":  KB_PATH.stat().st_size // 1024,
            "sources":     [{"source": r["source"], "chunks": r["n"]}
                            for r in sources],
        }
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
#  CLAUDE TOOL DEFINITION (for use in agents)
# ══════════════════════════════════════════════════════════════════════════════

KB_TOOL = {
    "name": "query_faa_regulations",
    "description": (
        "Search the FAA knowledge base for relevant Part 107 regulations, "
        "aeronautical chart guidance, airspace rules, weather minimums, "
        "NOTAM phraseology, Remote ID requirements, and waiver procedures. "
        "Use this tool BEFORE making any regulatory determination. "
        "Returns the exact regulatory text with source citations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural language query describing what regulation or "
                    "guidance you need. Examples: 'night flying strobe light "
                    "requirement', 'Class D airspace authorization LAANC', "
                    "'wind speed operating limits', 'TFR avoidance'"
                )
            },
            "limit": {
                "type": "integer",
                "description": "Number of results to return (default 5, max 10)",
                "default": 5
            },
            "source_filter": {
                "type": "string",
                "description": "Optional: filter by source name substring, e.g. 'Part 107', 'Chart'",
                "default": ""
            }
        },
        "required": ["query"]
    }
}


def execute_kb_tool(args: dict) -> str:
    """Execute the KB tool call from a Claude agent loop."""
    query         = args.get("query", "")
    limit         = min(int(args.get("limit", 5)), 10)
    source_filter = args.get("source_filter") or None

    results = query_kb(query, limit=limit, source_filter=source_filter)
    return format_results_for_prompt(results)


# ══════════════════════════════════════════════════════════════════════════════
#  PREFLIGHT AGENT INTEGRATION PATCH
# ══════════════════════════════════════════════════════════════════════════════

PREFLIGHT_KB_SYSTEM_ADDENDUM = """
KNOWLEDGE BASE ACCESS
---------------------
You have access to the query_faa_regulations tool which searches a local
database of FAA Part 107 regulations, aeronautical chart guidance, airspace
rules, and weather minimums.

MANDATORY: Before making any regulatory determination (GO/NO-GO verdict,
airspace classification, weather minimum check, lighting requirement), you
MUST call query_faa_regulations with a specific query to retrieve the
relevant regulatory text. Do not rely on memory alone — always ground your
decisions in the retrieved regulatory text and cite the source.

Examples:
- Checking altitude: query "maximum altitude 400 ft AGL uncontrolled airspace"
- Night flight: query "civil twilight anti-collision light 3 statute miles"
- TFR check: query "temporary flight restriction UAS operations"
- Wind limits: query "wind speed operating limitations Mini 4 Pro Air 3"
"""


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="FAA Knowledge Base for Safety1271")
    parser.add_argument("--build",  action="store_true", help="Build/update knowledge base")
    parser.add_argument("--ingest-local", action="store_true", help="Ingest PDFs from the Knowleged folder as key sources")
    parser.add_argument("--force",  action="store_true", help="Rebuild from scratch")
    parser.add_argument("--query",  type=str, help="Test query against the knowledge base")
    parser.add_argument("--status", action="store_true", help="Show KB statistics")
    parser.add_argument("--limit",  type=int, default=5, help="Results per query")
    args = parser.parse_args()

    if args.status:
        s = kb_status()
        if not s.get("built"):
            print("\n  Knowledge base not built. Run: python faa_knowledge_base.py --build")
            return
        print(f"\n  FAA Knowledge Base Status")
        print(f"  {'─'*40}")
        print(f"  Path:    {s['db_path']}")
        print(f"  Built:   {s['built_at']}")
        print(f"  Chunks:  {s['total_chunks']:,}")
        print(f"  Size:    {s['db_size_kb']} KB")
        print(f"\n  Sources:")
        for src in s["sources"]:
            print(f"    {src['chunks']:4d} chunks  {src['source'][:60]}")
        return

    if args.ingest_local:
        conn = get_db()
        init_db(conn)
        done = ingest_knowledge_folder(conn)
        conn.close()
        print(f"\n  Ingested key sources: {', '.join(done) if done else 'none found in ' + str(KNOWLEDGE_DIR)}\n")
        return

    if args.build:
        print(f"\n  Building FAA knowledge base...")
        print(f"  This downloads ~8 MB of FAA documents on first run.")
        print(f"  Subsequent runs use the local cache.\n")
        n = build_kb(force=args.force)
        print(f"\n  Done — {n} chunks indexed.\n")
        return

    if args.query:
        if not KB_PATH.exists():
            print("\n  Build the KB first: python faa_knowledge_base.py --build")
            return
        results = query_kb(args.query, limit=args.limit)
        print(f"\n  Query: '{args.query}'")
        print(f"  Results: {len(results)}")
        print()
        print(format_results_for_prompt(results))
        return

    parser.print_help()


if __name__ == "__main__":
    main()

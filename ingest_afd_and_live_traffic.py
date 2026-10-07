"""
ingest_afd_and_live_traffic.py
================================
TWO CAPABILITIES IN ONE FILE:

PART 1 — Airport/Facility Directory (A/FD) Ingestion
  Parses the uploaded FAA Chart Supplement / A/FD PDFs and adds them
  to the knowledge base. Covers:
    • Davison AAF (KDAA)  — Army airfield, Class D, Fort Belvoir VA
    • Front Royal (KFRR)  — Class G surface, Warren County VA
    • Leesburg Exec (KJYO)— Class D, Loudoun County VA
    • FAA Airman Knowledge Testing Supplement (sectional chart legends,
      chart supplement legends, METAR/TAF formats, airspace depictions)

  Run:
    python ingest_afd_and_live_traffic.py --ingest-pdfs

PART 2 — Live Traffic Deconfliction Agent (OpenSky Network)
  Queries the OpenSky Network API for transponder-equipped aircraft
  within a configurable radius of the drone's position. Calculates
  closest approach and issues SMS/email deconfliction alerts when a
  manned aircraft enters the safety buffer zone.

  Why OpenSky (not FlightRadar24/FlightAware):
    FlightRadar24: no public API — scraping violates ToS.
    FlightAware AeroAPI: paid ($0.01/query) — viable option (see below).
    OpenSky Network: FREE, open, live ADS-B data, no API key required.
    ADS-B Exchange: free for non-commercial, good US coverage.

  Run standalone:
    python ingest_afd_and_live_traffic.py --traffic --lat 38.7773 --lon -77.1868

  Import as module:
    from ingest_afd_and_live_traffic import LiveTrafficMonitor
    monitor = LiveTrafficMonitor(home_lat=38.7773, home_lon=-77.1868)
    monitor.start()
"""

import os, re, sys, math, time, json, sqlite3, hashlib, threading, logging, argparse
from pathlib  import Path
from datetime import datetime
from typing   import List, Optional

import requests
import pdfplumber
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("AFD_Traffic")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")

KB_PATH     = Path(os.getenv("FAA_KB_PATH", "./faa_knowledge.db"))
ALERT_PHONE = os.getenv("ALERT_PHONE", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")


# ══════════════════════════════════════════════════════════════════════════════
#  PART 1 — A/FD AND SUPPLEMENT INGESTION
# ══════════════════════════════════════════════════════════════════════════════

# ── Inline A/FD structured data (from uploaded PDFs) ─────────────────────────
# Parsed and structured from the FAA Chart Supplement pages in the uploads.

AFD_AIRPORTS = [
    {
        "icao":      "KDAA",
        "iata":      "DAA",
        "name":      "Davison Army Airfield",
        "location":  "Fort Belvoir, Virginia (3 NM NW of fort)",
        "lat":        38.7150,   # N38°42.90'
        "lon":       -77.1808,   # W77°10.85'
        "elevation":  74,        # ft MSL
        "type":      "Military (ARNG)",
        "region":    "Northern Virginia / DC SFRA",
        "content": """
DAVISON AAF (KDAA) — FORT BELVOIR, VIRGINIA
FAA IDENTIFIER: DAA   ICAO: KDAA
COORDINATES: N38°42.90' W77°10.85'   ELEVATION: 74 ft MSL
TYPE: Army National Guard (ARNG) Military Airfield
A/FD CYCLE: NE, 9 JUL 2026 to 3 SEP 2026

AIRSPACE:
  CLASS D: Active 1100–0230Z Monday–Friday, excluding holidays.
  OTHER TIMES: CLASS G (reverts to uncontrolled).
  NOTE: Class D radius typically 4-5 NM around airport.
  When tower is CLOSED, Class D is NOT in effect → Class G applies.
  This means on weekends and holidays, no LAANC/ATC authorization required
  for operations below 700 ft AGL (check specific grid).

RUNWAYS:
  RWY 14-32: 5,421 ft × 75 ft, Asphalt, PCN 52 F/A/W/T, HIRL lighting.
  RWY 14: PAPI (P4L) Glide slope 3.0°, TCH 68 ft. Threshold displaced 491 ft.
  RWY 32: MALSF approach lights. PAPI (P4R) 3.0°, TCH 35 ft. Threshold displaced 892 ft.

COMMUNICATIONS:
  CTAF:     124.275 MHz (Common Traffic Advisory Frequency)
  ATIS:     128.175 MHz (active 1100–0230Z Mon–Fri, excl holidays)
  Tower:    124.275 / 229.4 / 241.0 MHz (1100–0230Z Mon–Fri)
  Ground:   121.9 / 351.8 MHz
  Clearance: 351.8 MHz
  Potomac Approach/Departure: 118.95, 124.7, 257.2, 338.2 MHz
  Base Ops: 139.4 MHz (VIP arrival contact 15 min prior to landing)
  Metro PMSV: 139.4 MHz

SERVICE:
  Fuel: A++ Military only. Available 1200–0300Z Mon–Fri.
  Lighting: Available 0230–1100Z Mon–Fri.
  Military-only fuel — civilians cannot refuel here.
  Prior Permission Required (PPR): 24-hour notice for all non-DAA based aircraft.
  Contact Base Ops: C571-515-4226/4228/4224

UAS / DRONE NOTES:
  • Located within DC SFRA (30 NM from DCA). LAANC required.
  • Class D active Mon–Fri 1100–0230Z — during these hours, ATC authorization
    mandatory for any altitude.
  • Outside Class D hours (weekends, holidays, nights): Class G may apply
    but still within DC SFRA. Verify LAANC ceiling on UAS Facility Map.
  • Military airfield — unexpected military helicopter traffic at any time.
  • CAUTION: Expected wind shear/crosswind shift in touchdown zone Rwy 32
    during SW-NW winds. Wildlife hazard.
  • ATC Frequency for ATC Listener agent: 124.275 (tower/CTAF)

RADIO AIDS:
  ILS/DME: 108.9 MHz, I-DAA, Channel 26, Runway 32, Class IA.
  ARMEL VORW/DME: 113.5 AML, N38°56.08' W77°28.00', 142°/18.8 NM to field.
  NOTAM FILE: DCA
""",
    },
    {
        "icao":      "KFRR",
        "iata":      "FRR",
        "name":      "Front Royal-Warren County Airport",
        "location":  "Front Royal, Virginia (3 NM West of town)",
        "lat":        38.9175,   # N38°55.05'
        "lon":       -78.2533,   # W78°15.20'
        "elevation":  704,
        "type":      "Public — No Control Tower",
        "region":    "Shenandoah Valley, Virginia",
        "content": """
FRONT ROYAL-WARREN CO (KFRR) — FRONT ROYAL, VIRGINIA
FAA IDENTIFIER: FRR   ICAO: KFRR
COORDINATES: N38°55.05' W78°15.20'   ELEVATION: 704 ft MSL
TYPE: Public General Aviation Airport — NO CONTROL TOWER
A/FD CYCLE: NE, 9 JUL 2026 to 3 SEP 2026

AIRSPACE:
  NO CLASS D — No control tower at this airport.
  Airspace: Check sectional chart — likely Class E at 700 ft AGL due to
  instrument approach procedures (dashed magenta boundary on chart).
  Below 700 ft AGL: Class G (no authorization required).
  LAANC: Check UAS Facility Map — may have authorization available.
  NOTAM File: DCA

RUNWAYS:
  RWY 10-28: 3,008 ft × 75 ft, Asphalt, S-12.5 load rating, MIRL lighting.
  RWY 10: APAP approach path indicator. Glide angle 3.0°, TCH 16 ft. Road at end.
  RWY 28: APAP glide path. 3.0°, TCH 16 ft. Pole at end.
  Runway slope: 0.4% uphill to the East.

COMMUNICATIONS:
  CTAF/UNICOM:  123.0 MHz
  Potomac App/Dep: 120.45 MHz
  Clearance Delivery phone: 866-709-4993 (Potomac Approach)
  AWOS-3: 121.85 MHz / (540) 635-5377
  Airport Manager: (540) 635-3570

SERVICE:
  Fuel: 100LL (avgas)
  Lighting: Activate MIRL Rwy 10-28 via CTAF (keying mic).
  Attended: May–Sep 1300–2200Z, Oct–Apr 1400–2100Z.

SPECIAL ACTIVITIES AT THIS AIRPORT:
  GLIDER operations on and in vicinity of airport.
  PARACHUTE operations on and in vicinity of airport.
  Gyrocopter, ultralight, and glider traffic uses RIGHT traffic pattern
  for both Runway 10 and Runway 28.
  Deer and geese on and in vicinity — wildlife hazard.
  Noise abatement procedures in effect.

UAS / DRONE NOTES:
  • No Class D = less restrictive for drones (verify LAANC ceiling).
  • ACTIVE glider and parachute operations — high risk of untracked traffic.
  • Gliders and ultralights have NO transponders → NOT visible on ADS-B/OpenSky.
  • CAUTION: Parachutists may be at any altitude below 15,000 ft in vicinity.
  • At 704 ft elevation, density altitude significantly higher than sea level.
  • ATC Listener: Monitor CTAF 123.0 for traffic advisories.
  • LINDEN VORTACW 114.3 MHz (N38°51.26' W78°12.33') 4.4 NM from field.

ADJACENT HELIPAD:
  H1: 30 ft × 30 ft concrete. (Previous page entry — Brooke VORTAC area)

RADIO AIDS:
  LINDEN (L) VORTACW: 114.3 LDN Chan 90, N38°51.26' W78°12.33', 335°/4.4 NM to field.
  BROOKE VORTAC: 114.5 BRV Chan 92, N38°20.18' W77°21.17'.
  NDB: 237 EZF, N38°15.98' W77°27.03'. Unmonitored when airport unattended.
  NOTAM FILE: DCA
""",
    },
    {
        "icao":      "KJYO",
        "iata":      "JYO",
        "name":      "Leesburg Executive Airport",
        "location":  "Leesburg, Virginia (3 NM South of town)",
        "lat":        39.0780,   # N39°04.68'
        "lon":       -77.5575,   # W77°33.45'
        "elevation":  390,
        "type":      "Public — Control Tower (part-time)",
        "region":    "Northern Virginia / Loudoun County",
        "content": """
LEESBURG EXECUTIVE (KJYO) — LEESBURG, VIRGINIA
FAA IDENTIFIER: JYO   ICAO: KJYO
COORDINATES: N39°04.68' W77°33.45'   ELEVATION: 390 ft MSL
TYPE: Public — Control Tower (PART-TIME)
A/FD CYCLE: NE, 9 JUL 2026 to 3 SEP 2026

AIRSPACE:
  CLASS D: Active when Leesburg Tower is operating: 1300–2300Z daily.
  (Approximately 9:00 AM to 7:00 PM Eastern — varies with DST.)
  OTHER TIMES: CLASS G (reverts to uncontrolled — no ATC auth required).
  Traffic Pattern Altitude (TPA): 1,200 ft MSL (810 ft AGL).
  NOTAM File: JYO
  Note: This airport is approximately 30 NM from DCA — on edge of DC SFRA.
  Verify LAANC requirements — SFRA procedures may apply.

RUNWAYS:
  RWY 17-35: 5,500 ft × 100 ft, Asphalt-Grooved, S-30 / D-70 load ratings.
  PCN 63 F/A/W/U. HIRL lighting.
  RWY 17: ODALS (3-light non-standard). REIL. PAPI (P4L) 3.0°, TCH 45 ft. Tree.
  RWY 35: REIL. PAPI (P4L) 3.0°, TCH 37 ft. Pole.
  CALM WIND: Use Runway 17.

COMMUNICATIONS:
  CTAF:         127.5 MHz (when tower closed)
  UNICOM:       122.975 MHz
  Tower:        127.5 MHz (1300–2300Z daily)
  Ground:       120.5 MHz
  Clearance Del: 120.5 / 118.55 MHz
  Potomac App/Dep: 125.05 MHz
  AWOS-3:       125.225 MHz / (703) 777-3781
  Airport Mgr:  703-737-7125
  CD phone (tower closed): 866-709-4993 (Potomac Approach)
  U.S. Customs: 703-661-2800 (24-hour notice required)

SERVICE:
  Fuel: 100LL, Jet A, Oxygen (OX 4).
  Attended: 1100–0200Z daily (approximately 7 AM to 10 PM Eastern).
  Class D hours (tower): 1300–2300Z daily.
  When tower CLOSED: activate approach lights ODALS Rwy 17, REIL Rwy 17&35,
  PAPI Rwy 17&35, HIRL Rwy 17-35 via CTAF frequency keying.

SPECIAL NOTES:
  Birds and deer on and in vicinity — wildlife hazard.
  Helicopter activity on and in vicinity.
  Possible thermal plumes from power plant 1.3 NM SSE — may affect drones.
  U.S. Customs available (for international arrivals) with 24-hour notice.
  Western Ramp: fence on southwest side. Lead-in lines on west ramp not available.

UAS / DRONE NOTES:
  • Class D active 1300–2300Z — LAANC/authorization required during tower hours.
  • When tower closes (before 1300Z or after 2300Z): Class G → fly freely below 700 ft.
  • THERMAL PLUMES from power plant 1.3 NM SSE: may cause drone instability.
  • High GA traffic airport — active IFR and VFR traffic at all hours.
  • ATC Listener frequencies: Tower 127.5 / Potomac App 125.05.
  • LCAA (Loudoun County Aeromodelers) flying field nearby — model aircraft activity.
  • ATC Listener agent should monitor 127.5 (tower) and 125.05 (Potomac).

RADIO AIDS:
  ILS/DME: 111.75 I-JYO, Channel 54(Y), Runway 17, Class IE.
  ARMEL VORW/DME: 113.5 AML Chan 82, N38°56.08' W77°28.00', 342°/9.6 NM to field.
  NOTAM FILE: IAD (not DCA — important for NOTAM searches)
""",
    },
]

# ── Supplement content (FAA-CT-8080-2H test supplement) ─────────────────────

SUPPLEMENT_KNOWLEDGE = [
    ("REF","FAA Test Supplement","Chart Supplement Legend — Airport Data","chart supplement,A/FD,legend","""
FAA CHART SUPPLEMENT — HOW TO READ AIRPORT ENTRIES (Legend 2-3)

AIRPORT ENTRY FORMAT:
NAME (Identifier)(ICAO) [Services] [Distance/Direction] UTC offset [Coordinates]
Elevation [Beacon type] TPA [Traffic Pattern Altitude] NOTAM FILE [Office]

EXAMPLE DECODED:
LEESBURG EXEC (JYO)(KJYO) 3 S UTC-5(-4DT) N39°04.68' W77°33.45'
= Leesburg Executive Airport, FAA ID JYO, ICAO KJYO, 3 miles South,
  UTC minus 5 hours (EDT: minus 4), coordinates as shown.

390 B TPA-1200(810) LRA NOTAM FILE JYO
= Elevation 390 ft MSL, Beacon (rotating), TPA 1,200 ft MSL (810 ft AGL),
  Low/Restricted Airspace, NOTAMs filed with JYO office.

AIRPORT BEACON TYPES:
White/Green alternating = lighted land airport.
Green/Yellow alternating = lighted water airport.
White/Yellow alternating = lighted heliport.
White/White/Green = military airport (double white flash).
No beacon = airport may still be operational.

RUNWAY INFORMATION FORMAT:
RWY 17-35: H5500X100 (ASPH-GRVD) S-30, D-70 PCN63 F/A/W/U HIRL
= Runway 17-35, Hard surface 5,500 ft × 100 ft, Asphalt-Grooved.
  Single-wheel load: 30,000 lbs. Dual-wheel: 70,000 lbs.
  PCN (Pavement Classification Number): 63.
  F=Flexible pavement, A=Medium-strength subgrade, W=High-tire pressure, U=Unlimited eval.
  HIRL = High Intensity Runway Lights.

H = Hard surface (paved)
S = Soft surface (turf/gravel)
Number = length in feet × width in feet
(ASPH) = Asphalt, (CONC) = Concrete, (TURF) = Grass
"""),

    ("REF","FAA Test Supplement","Sectional Chart Legend — Airspace Boundaries","sectional chart,legend,airspace,symbols","""
FAA SECTIONAL CHART LEGEND — AIRSPACE DEPICTIONS (Legend 1)

CONTROLLED AIRSPACE BOUNDARIES:
Blue solid line — Class B (ceiling/floor noted inside: e.g., 100/30 = 10,000 MSL ceiling, 3,000 MSL floor)
Magenta solid line — Class C (two rings: SFC to 4,000 ft AGL)
Blue dashed line — Class D (surface to ~2,500 ft AGL, near tower airports)
Magenta vignette (fuzzy shaded) — Class E floor begins at 700 ft AGL
Blue vignette (fuzzy shaded) — Class E floor begins at 1,200 ft AGL
Magenta dashed — Class E surface extension (floor at surface, instrument approach areas)

SPECIAL USE AIRSPACE:
Blue hatch with P-### — Prohibited Area (no flight)
Blue hatch with R-### — Restricted Area (need permission)
Blue hatch with W-### — Warning Area (caution)
Magenta hatch — MOA (Military Operations Area)
Magenta hatch with A-### — Alert Area
NSA label — National Security Area (voluntary avoidance)

AIRPORTS ON SECTIONAL:
Blue circle — Airport with control tower (towered)
Magenta circle — Airport without control tower (non-towered)
R — REIL (Runway End Identifier Lights)
VASI — Visual Approach Slope Indicator
PAPI — Precision Approach Path Indicator

HOW TO IDENTIFY AIRSPACE AT A SPECIFIC LOCATION:
1. Find your location on sectional.
2. Look for any boundary lines AROUND or THROUGH your location.
3. If inside any boundary, determine the floor/ceiling.
4. Class G = no boundary symbols below you.
5. Dashed magenta around airport = Class E starts at surface.
"""),

    ("REF","FAA Test Supplement","METAR/TAF Reference — Official FAA Format","METAR,TAF,weather,format,decode","""
OFFICIAL FAA METAR FORMAT (from FAA Test Supplement)

METAR KDCA 151754Z 27012KT 10SM FEW025 SCT080 BKN250 23/10 A2998 RMK AO2 SLP152

Decoded element by element:
METAR      = Routine surface weather observation
KDCA       = Reagan Washington National Airport (K prefix = CONUS)
151754Z    = Day 15 of month, 1754 UTC (Zulu time)
27012KT    = Wind FROM 270° (West) at 12 knots
             FORMAT: DDD/SSGxx = direction/speed/G(ust)
             VRB = variable direction (wind speed < 6 kts)
10SM       = Visibility 10 statute miles (not nautical)
             P6SM = greater than 6 SM
             M1/4SM = less than 1/4 SM
FEW025     = Few clouds at 2,500 ft AGL (FEW = 1-2 oktas)
SCT080     = Scattered clouds at 8,000 ft AGL (SCT = 3-4 oktas)
BKN250     = Broken layer at 25,000 ft AGL — this is the ceiling (BKN = 5-7 oktas)
23/10      = Temperature 23°C / Dew point 10°C
A2998      = Altimeter setting 29.98 inHg
RMK AO2    = Automated station with precipitation discriminator
SLP152     = Sea Level Pressure 1015.2 mb

WEATHER PHENOMENA:
BR = Mist (1/2 to 6 SM vis)   FG = Fog (<1/4 SM)   HZ = Haze
RA = Rain   SN = Snow   DZ = Drizzle   TS = Thunderstorm   GR = Hail
VC = In vicinity (5-10 SM from station)   UP = Unknown precipitation
Intensity: - = Light (no sign) = Moderate   + = Heavy
"""),

    ("REF","FAA Test Supplement","Reading Airport Frequencies from Chart Supplement","communications,frequencies,CTAF,ATIS,UNICOM","""
COMMUNICATIONS SECTION OF CHART SUPPLEMENT

HOW TO FIND FREQUENCIES FOR ANY AIRPORT:

CTAF (Common Traffic Advisory Frequency):
Used for position reports and traffic coordination at non-towered airports.
Also used at towered airports when tower is CLOSED.
Pilots say: "Leesburg traffic, Cessna 12345, downwind Runway 17, touch-and-go, Leesburg"
Drone operators: Monitor CTAF for traffic awareness.

UNICOM:
Ground-based operator providing airport services (fuel, tie-down etc.)
Typically same as CTAF at non-towered airports.
Frequency usually 122.8 or 123.0 at non-towered airports.

ATIS (Automatic Terminal Information Service):
Pre-recorded weather + runway + NOTAM info.
Updated every hour or when conditions change significantly.
Current info identified by letter code (Alpha, Bravo, Charley...).
Listen before calling ATC: "have information Delta."

® symbol before frequency = RADAR REQUIRED for approach/departure.

FREQUENCY BANDS:
118.0-136.975 MHz = VHF Aviation (ATC, CTAF, ATIS, UNICOM).
121.5 MHz = Emergency/Guard frequency (always monitored by ATC).
123.45 MHz = Air-to-air (pilot chat, not for ATC use).
AWOS/ASOS = Automated weather broadcast on specified VHF frequency.

NORTHERN VIRGINIA AIRPORT FREQUENCIES (from uploaded A/FD):
Davison AAF  CTAF/Tower: 124.275 | ATIS: 128.175 | Potomac App: 118.95/124.7
Leesburg Exec CTAF: 127.5 | Tower: 127.5 | Potomac App: 125.05 | AWOS: 125.225
Front Royal   CTAF: 123.0 | Potomac App: 120.45 | AWOS: 121.85
"""),

    ("REF","FAA Test Supplement","Northern Virginia Airport Deconfliction Guide","deconfliction,Northern Virginia,airports,Class D,hours","""
NORTHERN VIRGINIA AIRPORTS — UAS DECONFLICTION REFERENCE

DAVISON AAF (KDAA) — Fort Belvoir, VA
  Position: N38°42.90' W77°10.85' (38.7150°N, 77.1808°W)
  Elevation: 74 ft MSL
  Class D hours: 1100–0230Z Monday–Friday (excl. holidays)
  = 6:00 AM to 10:30 PM Eastern (Summer/EDT)
  = 7:00 AM to 11:30 PM Eastern (Winter/EST)
  Weekend/Holiday: CLASS G — no ATC auth required (check LAANC ceiling)
  Key risk: Military helicopter traffic (H-60, etc.) at low altitude, often unannounced.
  ATC Listener freq: 124.275 MHz (monitor for helicopter traffic advisories)
  SFRA status: Inside DC SFRA. Verify LAANC.

LEESBURG EXECUTIVE (KJYO) — Leesburg, VA
  Position: N39°04.68' W77°33.45' (39.0780°N, 77.5575°W)
  Elevation: 390 ft MSL
  Class D hours: 1300–2300Z daily (approx 9 AM–7 PM EDT)
  Outside Class D: CLASS G — below 700 ft AGL unrestricted
  Key risk: Active IFR traffic, helicopter operations, thermal plumes from power plant 1.3 NM SSE
  ATC Listener: 127.5 (CTAF/Tower) and 125.05 (Potomac Approach)
  Note: NOTAM file is IAD (not DCA) — search both for complete NOTAM picture.

FRONT ROYAL-WARREN CO (KFRR) — Front Royal, VA
  Position: N38°55.05' W78°15.20' (38.9175°N, 78.2533°W)
  Elevation: 704 ft MSL
  No control tower — NO Class D. Airport is non-towered.
  Key risk: GLIDERS (no transponder), PARACHUTISTS (no radar return), ultralights
  ATC Listener: 123.0 CTAF — monitor for traffic position reports
  AWOS: 121.85 MHz

DISTANCE BETWEEN AIRPORTS (approximate):
  KDAA to KJYO: ~30 NM
  KDAA to Burke Community Church (38.7773N 77.1868W): ~2 NM
  KJYO to Burke CC: ~24 NM
  KFRR to Burke CC: ~50 NM

BURKE COMMUNITY CHURCH AIRSPACE CONTEXT:
  Location: 38.7773°N, 77.1868°W (8304 Old Keene Mill Rd, Springfield VA)
  Distance from DCA (Reagan National): ~9 NM → Inside DC FRZ
  Distance from KDAA (Davison): ~2 NM → Inside Davison's Class D radius when active
  Typical LAANC ceiling: 0-100 ft at this location (highly restricted)
  ATC Listener priority: 124.275 (Davison Tower), 118.95 (Potomac App)
"""),
]


def get_db():
    conn = sqlite3.connect(str(KB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def insert_chunk(conn, domain, source, section, topic, content):
    h = hashlib.md5(content.encode()).hexdigest()
    try:
        conn.execute(
            "INSERT INTO chunks(domain,source,section,topic,content,content_hash,char_count) "
            "VALUES(?,?,?,?,?,?,?)",
            (domain, source, section, topic, content.strip(), h, len(content)))
        return 1
    except sqlite3.IntegrityError:
        return 0


def ingest_pdf_text(conn, pdf_path: Path, domain: str, source: str, topic: str):
    """Extract text from PDF and chunk into KB."""
    if not pdf_path.exists():
        log.warning(f"PDF not found: {pdf_path}")
        return 0
    total = 0
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            buffer = []
            for page in pdf.pages:
                t = (page.extract_text() or "").strip()
                if t:
                    buffer.append(t)
                if len(buffer) >= 5:
                    combined = "\n".join(buffer)
                    # Split by section headers
                    sections = re.split(r'\n(?=[A-Z][A-Z\s]{8,}(?:\n|$))', combined)
                    for sec in sections:
                        sec = sec.strip()
                        if len(sec) > 100:
                            h = hashlib.md5(sec.encode()).hexdigest()
                            try:
                                conn.execute(
                                    "INSERT INTO chunks(domain,source,section,topic,content,content_hash,char_count) "
                                    "VALUES(?,?,?,?,?,?,?)",
                                    (domain, source, sec[:60], topic, sec, h, len(sec)))
                                total += 1
                            except sqlite3.IntegrityError:
                                pass
                    buffer = []
    except Exception as e:
        log.error(f"PDF ingestion error {pdf_path.name}: {e}")
    conn.commit()
    return total


def ingest_all_pdfs():
    """Main ingestion function — PDFs + structured airport data + supplement knowledge."""
    conn = get_db()

    # Ensure tables exist
    try:
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
    except Exception:
        log.error("Run build_part107_kb.py --build first to initialize the database.")
        conn.close()
        return

    total = 0
    log.info("\nIngesting Airport/Facility Directory data...")

    # 1. Structured airport entries
    for apt in AFD_AIRPORTS:
        n = insert_chunk(conn, "II", f"A/FD — {apt['name']}",
                         f"{apt['icao']} — {apt['name']}", "airport,A/FD,frequencies,airspace,Class D",
                         apt["content"])
        total += n
        log.info(f"  {apt['icao']} {apt['name']}: {n} chunk(s)")

    # 2. Supplement knowledge entries
    log.info("\nIngesting FAA Test Supplement content...")
    for domain, source, section, topic, content in SUPPLEMENT_KNOWLEDGE:
        n = insert_chunk(conn, domain, source, section, topic, content)
        total += n
        log.info(f"  {section[:50]}: {n} chunk(s)")

    # 3. Raw PDF text from supplement
    supplement_path = Path("/mnt/user-data/uploads/sport_rec_private_akts.pdf")
    if supplement_path.exists():
        log.info("\nIngesting FAA Airman Knowledge Testing Supplement PDF (113 pages)...")
        n = ingest_pdf_text(conn, supplement_path, "REF",
                            "FAA Airman Knowledge Testing Supplement (FAA-CT-8080-2H)",
                            "charts,figures,sectional,METAR,test supplement,legends")
        total += n
        log.info(f"  Supplement PDF: {n} chunks")

    # 4. Raw PDF from airport pages
    for pdf_name, apt_name in [("Davison.pdf","Davison AAF"), ("Front_Royal.pdf","Front Royal"),
                                ("Leesburg.pdf","Leesburg Exec")]:
        path = Path(f"/mnt/user-data/uploads/{pdf_name}")
        if path.exists():
            n = ingest_pdf_text(conn, path, "II",
                                f"A/FD — {apt_name}", "airport,Virginia,airspace,frequencies")
            total += n
            log.info(f"  {pdf_name}: {n} chunks")

    # Rebuild FTS index
    log.info("\nRebuilding FTS index...")
    try:
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    except Exception:
        pass
    conn.commit()
    conn.close()

    log.info(f"\n  Total new chunks added: {total}")
    return total


# ══════════════════════════════════════════════════════════════════════════════
#  PART 2 — LIVE TRAFFIC DECONFLICTION (OpenSky Network)
# ══════════════════════════════════════════════════════════════════════════════
"""
LIVE TRAFFIC DATA SOURCES COMPARISON:

FlightRadar24
  - No official public API. Scraping violates Terms of Service.
  - Commercial API exists but requires enterprise contract ($$$).
  - NOT RECOMMENDED — ToS violation risk.

FlightAware AeroAPI (v4)
  - Official paid API: https://www.flightaware.com/commercial/aeroapi/
  - Cost: ~$0.01 per query (reasonable for low-frequency use).
  - Very complete data: position, flight plan, origin/destination, delay info.
  - API key required. Set FLIGHTAWARE_API_KEY in .env.
  - Endpoint: GET /flights/search/positions?query=-latlong 38 -77 39 -78

ADS-B Exchange (https://www.adsbexchange.com/)
  - Free for non-commercial use with attribution.
  - API: https://adsbexchange.com/api/aircraft/json/lat/lon/dist/
  - Excellent ADS-B coverage, unfiltered (shows military too).
  - Preferred for security/safety applications.
  - Set ADSBX_API_KEY in .env (free tier available).

OpenSky Network (https://opensky-network.org/)
  - FREE — no API key required for anonymous access.
  - 100 requests/hour anonymous; 4,000/hour with free account.
  - Returns all Mode-S/ADS-B transponder traffic.
  - Best choice for development and light-use applications.
  - Coverage: Good in Northern Virginia (many feeder stations).
  - Limitation: ~5-15 second data latency.

WHAT ADS-B SHOWS / DOESN'T SHOW:
Shows: Commercial airliners, most GA aircraft with Mode-S/ADS-B transponders,
       helicopters with transponders, some military (unclassified).
Doesn't show: Gliders, ultralights, some helicopters without transponders,
              military operating in EMCON (emissions control), small GA
              without transponders below 10,000 ft MSL.
NOTE: Drones (sUAS) are NOT on ADS-B — Remote ID uses a different protocol.
"""

OPENSKY_URL   = "https://opensky-network.org/api/states/all"
ADSBX_URL     = "https://adsbexchange.com/api/aircraft/json"
FLIGHTAWARE_URL = "https://aeroapi.flightaware.com/aeroapi/flights/search/positions"


def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def bearing_to(lat1, lon1, lat2, lon2):
    """Bearing from point 1 to point 2 in degrees."""
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(math.radians(lat2))
    y = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) -
         math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def fetch_opensky(drone_lat, drone_lon, radius_nm=5.0):
    """
    Query OpenSky Network for aircraft within radius_nm of drone.
    Returns list of aircraft dicts.
    """
    # Bounding box: 1 NM ≈ 0.0167° lat
    d = radius_nm * 0.0167 * 1.5   # generous box
    params = {
        "lamin": drone_lat - d, "lamax": drone_lat + d,
        "lomin": drone_lon - d, "lomax": drone_lon + d,
    }
    # Use registered account if credentials available
    auth = None
    user = os.getenv("OPENSKY_USER")
    pwd  = os.getenv("OPENSKY_PASS")
    if user and pwd:
        auth = (user, pwd)

    try:
        resp = requests.get(OPENSKY_URL, params=params, auth=auth, timeout=10)
        if not resp.ok:
            log.warning(f"OpenSky API error: {resp.status_code}")
            return []
        data = resp.json()
        states = data.get("states", []) or []
        aircraft = []
        for s in states:
            if s[5] is None or s[6] is None:  # no position
                continue
            ac_lon, ac_lat = s[5], s[6]
            dist_nm  = haversine_nm(drone_lat, drone_lon, ac_lat, ac_lon)
            if dist_nm > radius_nm:
                continue
            alt_baro_ft = (s[7] or 0) * 3.281 if s[7] else None  # meters to feet
            alt_geo_ft  = (s[13] or 0) * 3.281 if s[13] else None
            alt_ft = alt_geo_ft or alt_baro_ft or 0
            aircraft.append({
                "icao24":    s[0],
                "callsign":  (s[1] or "").strip() or "UNKNOWN",
                "lat":       ac_lat,
                "lon":       ac_lon,
                "alt_ft":    round(alt_ft),
                "vel_kts":   round((s[9] or 0) * 1.944),  # m/s to knots
                "heading":   s[10] or 0,
                "vert_rate": round((s[11] or 0) * 197),   # m/s to ft/min
                "on_ground": s[8] or False,
                "dist_nm":   round(dist_nm, 2),
                "bearing_from_drone": round(bearing_to(drone_lat, drone_lon, ac_lat, ac_lon)),
                "source":    "OpenSky Network",
            })
        return sorted(aircraft, key=lambda a: a["dist_nm"])
    except Exception as e:
        log.error(f"OpenSky fetch error: {e}")
        return []


def fetch_adsbx(drone_lat, drone_lon, radius_nm=5.0):
    """
    Query ADS-B Exchange API (better coverage, unfiltered military data).
    Requires ADSBX_API_KEY in environment.
    """
    api_key = os.getenv("ADSBX_API_KEY")
    if not api_key:
        return []  # silently skip if no key

    # radius in nautical miles → convert to their expected value
    try:
        url = f"{ADSBX_URL}/lat/{drone_lat}/lon/{drone_lon}/dist/{int(radius_nm*1.852)}/"
        resp = requests.get(url, headers={"api-auth": api_key}, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        aircraft = []
        for ac in data.get("ac", []):
            ac_lat = float(ac.get("lat", 0) or 0)
            ac_lon = float(ac.get("lon", 0) or 0)
            if not ac_lat or not ac_lon:
                continue
            dist_nm = haversine_nm(drone_lat, drone_lon, ac_lat, ac_lon)
            aircraft.append({
                "icao24":    ac.get("hex",""),
                "callsign":  ac.get("flight","UNKNOWN").strip(),
                "lat":       ac_lat,
                "lon":       ac_lon,
                "alt_ft":    int(ac.get("alt_baro", 0) or 0),
                "vel_kts":   int(ac.get("gs", 0) or 0),
                "heading":   float(ac.get("track", 0) or 0),
                "vert_rate": int(ac.get("baro_rate", 0) or 0),
                "on_ground": ac.get("alt_baro") == "ground",
                "dist_nm":   round(dist_nm, 2),
                "bearing_from_drone": round(bearing_to(drone_lat, drone_lon, ac_lat, ac_lon)),
                "type":      ac.get("t",""),
                "source":    "ADS-B Exchange",
            })
        return sorted(aircraft, key=lambda a: a["dist_nm"])
    except Exception as e:
        log.error(f"ADS-B Exchange fetch error: {e}")
        return []


def assess_threat(aircraft: dict, drone_lat: float, drone_lon: float,
                   drone_alt_ft: float) -> dict:
    """
    Evaluate whether an aircraft poses a deconfliction concern.
    Returns dict with threat_level (NONE/LOW/MEDIUM/HIGH/CRITICAL)
    and recommended_action.
    """
    dist_nm   = aircraft["dist_nm"]
    alt_ft    = aircraft["alt_ft"]
    vert_rate = aircraft.get("vert_rate", 0)   # ft/min (negative = descending)
    on_ground = aircraft.get("on_ground", False)
    vel_kts   = aircraft.get("vel_kts", 0)

    if on_ground:
        return {"threat_level": "NONE", "action": "On ground — not airborne."}

    # Altitude separation
    alt_sep_ft = abs(alt_ft - drone_alt_ft)

    # Descending toward drone altitude?
    descending_toward = (vert_rate < -100 and alt_ft > drone_alt_ft and
                         alt_sep_ft < 1500)

    # Classify threat
    if dist_nm < 0.25 and alt_sep_ft < 500:
        level  = "CRITICAL"
        action = f"IMMEDIATE: Aircraft within 0.25 NM and 500 ft — LAND NOW or RTH."
    elif dist_nm < 0.5 and alt_sep_ft < 500:
        level  = "HIGH"
        action = f"Aircraft {aircraft['callsign']} within 0.5 NM at {alt_ft} ft. Descend and land immediately."
    elif dist_nm < 1.0 and (alt_sep_ft < 800 or descending_toward):
        level  = "MEDIUM"
        action = f"Aircraft {aircraft['callsign']} {dist_nm} NM at {alt_ft} ft {'descending' if vert_rate < 0 else ''}. Hold position; monitor."
    elif dist_nm < 2.0 and alt_sep_ft < 1000:
        level  = "LOW"
        action = f"Traffic advisory: {aircraft['callsign']} {dist_nm} NM, {alt_ft} ft. Maintain awareness."
    else:
        level  = "NONE"
        action = "No immediate conflict."

    return {"threat_level": level, "action": action,
            "alt_separation_ft": alt_sep_ft,
            "descending_toward": descending_toward}


def format_traffic_report(aircraft_list: list, drone_lat: float,
                           drone_lon: float, drone_alt_ft: float) -> str:
    """Format a readable traffic situation report for SMS/email/log."""
    if not aircraft_list:
        return "No ADS-B traffic detected in search area."

    lines = [f"TRAFFIC REPORT — {datetime.now().strftime('%H:%M:%S')} UTC",
             f"Drone position: {drone_lat:.4f}N {abs(drone_lon):.4f}W @ {drone_alt_ft:.0f} ft AGL",
             f"Aircraft found: {len(aircraft_list)}",
             ""]
    for ac in aircraft_list[:8]:   # show max 8
        threat = assess_threat(ac, drone_lat, drone_lon, drone_alt_ft)
        lvl    = threat["threat_level"]
        flag   = {"CRITICAL":"🆘","HIGH":"⛔","MEDIUM":"⚠️","LOW":"ℹ️","NONE":"✅"}.get(lvl,"")
        lines.append(
            f"{flag} {ac['callsign']:<10} {ac['dist_nm']:4.1f} NM  {ac['alt_ft']:5d} ft  "
            f"{ac['vel_kts']:3d} kts  {ac['bearing_from_drone']:3.0f}°  [{lvl}]"
        )
    return "\n".join(lines)


class LiveTrafficMonitor:
    """
    Background thread monitoring live ADS-B traffic around the drone
    and issuing deconfliction alerts via SMS/email when needed.

    Integrates with Safety1271 patrol agent — call .start() after
    the drone lifts off, .stop() on landing.
    """

    def __init__(self, home_lat: float, home_lon: float,
                 drone_alt_ft: float = 200.0,
                 radius_nm: float = 3.0,
                 poll_interval_s: int = 20,
                 alert_threshold: str = "MEDIUM"):
        self.lat            = home_lat
        self.lon            = home_lon
        self.drone_alt_ft   = drone_alt_ft
        self.radius_nm      = radius_nm
        self.interval       = poll_interval_s
        self.alert_threshold= alert_threshold
        self._running       = False
        self._thread        = None
        self._last_alerts   = {}  # icao24 → last alert time (dedup)
        self.history        = []  # recent reports

    def update_position(self, lat: float, lon: float, alt_ft: float):
        """Call from patrol loop to keep drone position current."""
        self.lat, self.lon, self.drone_alt_ft = lat, lon, alt_ft

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._poll_loop,
                                          name="TrafficMonitor", daemon=True)
        self._thread.start()
        log.info(f"Live traffic monitor started — {self.radius_nm} NM radius, "
                 f"polling every {self.interval}s (OpenSky + ADS-B Exchange)")

    def stop(self):
        self._running = False
        log.info("Live traffic monitor stopped.")

    def _poll_loop(self):
        while self._running:
            self._check_traffic()
            time.sleep(self.interval)

    def _check_traffic(self):
        # Query both sources, merge, deduplicate by icao24
        opensky = fetch_opensky(self.lat, self.lon, self.radius_nm)
        adsbx   = fetch_adsbx(self.lat, self.lon, self.radius_nm)

        seen   = {}
        for ac in opensky + adsbx:
            k = ac["icao24"]
            if k not in seen or ac["dist_nm"] < seen[k]["dist_nm"]:
                seen[k] = ac
        aircraft = sorted(seen.values(), key=lambda a: a["dist_nm"])

        # Assess and alert on threats
        threshold_rank = {"NONE":0,"LOW":1,"MEDIUM":2,"HIGH":3,"CRITICAL":4}
        alert_rank     = threshold_rank.get(self.alert_threshold, 2)
        now            = time.time()

        for ac in aircraft:
            threat = assess_threat(ac, self.lat, self.lon, self.drone_alt_ft)
            lvl    = threat["threat_level"]
            rank   = threshold_rank.get(lvl, 0)

            if rank >= alert_rank:
                # Dedup: don't re-alert same aircraft within 60 seconds
                last = self._last_alerts.get(ac["icao24"], 0)
                if now - last > 60:
                    self._send_alert(ac, threat)
                    self._last_alerts[ac["icao24"]] = now

        # Log traffic summary
        if aircraft:
            report = format_traffic_report(aircraft, self.lat, self.lon, self.drone_alt_ft)
            self.history.append({
                "time":    datetime.now().isoformat(),
                "count":   len(aircraft),
                "report":  report,
            })
            if len(self.history) > 20:
                self.history.pop(0)
            log.info(f"Traffic: {len(aircraft)} aircraft in {self.radius_nm} NM")

    def _send_alert(self, ac: dict, threat: dict):
        lvl   = threat["threat_level"]
        emoji = {"CRITICAL":"🆘","HIGH":"⛔","MEDIUM":"⚠️"}.get(lvl,"⚠️")
        ts    = datetime.now().strftime("%H:%M:%S")

        sms   = (
            f"{emoji} TRAFFIC ALERT [{lvl}] — {ts}\n"
            f"Aircraft: {ac['callsign']} ({ac['icao24']})\n"
            f"Distance: {ac['dist_nm']} NM  Bearing: {ac['bearing_from_drone']}°\n"
            f"Altitude: {ac['alt_ft']} ft  Speed: {ac['vel_kts']} kts\n"
            f"Vert rate: {ac.get('vert_rate',0):+d} ft/min\n\n"
            f"ACTION: {threat['action']}"
        )
        log.critical(f"TRAFFIC ALERT [{lvl}] {ac['callsign']} {ac['dist_nm']} NM {ac['alt_ft']} ft")

        if ALERT_PHONE:
            try:
                from twilio_client import create_twilio_client, get_twilio_from_phone
                create_twilio_client().messages.create(
                  body=sms, from_=get_twilio_from_phone(), to=ALERT_PHONE)
            except Exception as e:
                log.error(f"SMS failed: {e}")

        if ALERT_EMAIL:
            try:
                from sendgrid import SendGridAPIClient
                from sendgrid.helpers.mail import Mail, Content
                html = f"""
                <div style='font-family:monospace;max-width:600px'>
                  <div style='background:{"#8b0000" if lvl=="CRITICAL" else "#cc2200" if lvl=="HIGH" else "#ba7517"};
                       padding:12px 20px;color:#fff;font-size:18px;font-weight:700'>
                    {emoji} DECONFLICTION ALERT — {lvl}
                  </div>
                  <div style='padding:16px;background:#f9f9f9'>
                    <pre style='font-size:13px'>{sms}</pre>
                    <p style='font-size:11px;color:#888;margin-top:12px'>
                      Source: {ac.get('source','ADS-B')} · 
                      Safety1271 Live Traffic Monitor
                    </p>
                  </div>
                </div>"""
                sg = SendGridAPIClient(os.environ["SENDGRID_API_KEY"])
                mail = Mail(from_email="traffic@safety1271.ai",
                            to_emails=ALERT_EMAIL,
                            subject=f"[{lvl}] Traffic: {ac['callsign']} {ac['dist_nm']} NM")
                mail.add_content(Content("text/html", html))
                sg.send(mail)
            except Exception as e:
                log.error(f"Email failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="A/FD Ingestion + Live Traffic Monitor")
    parser.add_argument("--ingest-pdfs", action="store_true",
                        help="Ingest uploaded A/FD PDFs and supplement into knowledge base")
    parser.add_argument("--traffic",     action="store_true",
                        help="Run live traffic check (single query)")
    parser.add_argument("--monitor",     action="store_true",
                        help="Run continuous traffic monitor (Ctrl+C to stop)")
    parser.add_argument("--lat",  type=float, default=38.7773,
                        help="Drone latitude (default: Burke Community Church)")
    parser.add_argument("--lon",  type=float, default=-77.1868,
                        help="Drone longitude")
    parser.add_argument("--alt",  type=float, default=200.0,
                        help="Drone altitude in feet AGL")
    parser.add_argument("--radius", type=float, default=3.0,
                        help="Search radius in nautical miles")
    parser.add_argument("--threshold", type=str, default="MEDIUM",
                        choices=["LOW","MEDIUM","HIGH","CRITICAL"],
                        help="Alert threshold level")
    args = parser.parse_args()

    if args.ingest_pdfs:
        n = ingest_all_pdfs()
        print(f"\n  Ingested {n} new chunks into knowledge base.\n")

    if args.traffic or args.monitor:
        print(f"\n  Querying live traffic near {args.lat:.4f}N {abs(args.lon):.4f}W "
              f"within {args.radius} NM...\n")

        opensky  = fetch_opensky(args.lat, args.lon, args.radius)
        adsbx    = fetch_adsbx(args.lat, args.lon, args.radius)
        combined = {ac["icao24"]: ac for ac in opensky + adsbx}
        aircraft = sorted(combined.values(), key=lambda a: a["dist_nm"])

        print(format_traffic_report(aircraft, args.lat, args.lon, args.alt))

        if args.monitor:
            print(f"\n  Starting continuous monitor (polling every 20s)...")
            monitor = LiveTrafficMonitor(
                home_lat       = args.lat,
                home_lon       = args.lon,
                drone_alt_ft   = args.alt,
                radius_nm      = args.radius,
                alert_threshold= args.threshold
            )
            monitor.start()
            try:
                while True:
                    time.sleep(30)
            except KeyboardInterrupt:
                monitor.stop()

    if not any([args.ingest_pdfs, args.traffic, args.monitor]):
        parser.print_help()


if __name__ == "__main__":
    main()

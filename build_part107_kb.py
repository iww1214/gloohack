"""
build_part107_kb.py
====================
Builds a comprehensive FAA Part 107 knowledge base covering every topic
that appears on the Remote Pilot Certificate Knowledge Test (ACS-10B).

All content is embedded inline — no network access required.
Structured by the 12 official ACS knowledge domains.

Run:
    python build_part107_kb.py               # build the KB
    python build_part107_kb.py --status      # show what's indexed
    python build_part107_kb.py --query "density altitude"
    python build_part107_kb.py --quiz        # random sample questions
"""

import os, sqlite3, hashlib, argparse, random, textwrap
from pathlib  import Path
from datetime import datetime

KB_PATH   = Path(os.getenv("FAA_KB_PATH", "./faa_knowledge.db"))
CHUNK_SZ  = 900
OVERLAP   = 150


# ══════════════════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(str(KB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chunks (
        id           INTEGER PRIMARY KEY,
        domain       TEXT,
        source       TEXT NOT NULL,
        section      TEXT,
        topic        TEXT,
        content      TEXT NOT NULL,
        content_hash TEXT UNIQUE,
        char_count   INTEGER
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        domain, source, section, topic, content,
        content='chunks', content_rowid='id',
        tokenize='porter ascii'
    );
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid,domain,source,section,topic,content)
        VALUES(new.id,new.domain,new.source,new.section,new.topic,new.content);
    END;
    CREATE TABLE IF NOT EXISTS sample_questions (
        id       INTEGER PRIMARY KEY,
        domain   TEXT,
        question TEXT,
        options  TEXT,
        answer   TEXT,
        explanation TEXT
    );
    CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.commit()

def insert_chunk(conn, domain, source, section, topic, content):
    h = hashlib.md5(content.encode()).hexdigest()
    try:
        conn.execute(
            "INSERT INTO chunks(domain,source,section,topic,content,content_hash,char_count) "
            "VALUES(?,?,?,?,?,?,?)",
            (domain, source, section, topic, content.strip(), h, len(content)))
        return 1
    except sqlite3.IntegrityError:
        return 0  # duplicate

def insert_question(conn, domain, question, options, answer, explanation):
    conn.execute(
        "INSERT INTO sample_questions(domain,question,options,answer,explanation) VALUES(?,?,?,?,?)",
        (domain, question, json_safe(options), answer, explanation))

def json_safe(opts):
    import json
    return json.dumps(opts)


# ══════════════════════════════════════════════════════════════════════════════
#  KNOWLEDGE CORPUS  —  ALL 12 ACS DOMAINS
# ══════════════════════════════════════════════════════════════════════════════

KNOWLEDGE = [

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN I — APPLICABLE REGULATIONS
# ─────────────────────────────────────────────────────────────────────────────

("I","14 CFR Part 107","§107.1 — Applicability","regulations,scope","""
14 CFR PART 107 — SMALL UNMANNED AIRCRAFT SYSTEMS

§107.1 APPLICABILITY
Part 107 applies to the registration, airman certification, and operation
of civil small unmanned aircraft systems within the United States, including
its territories and possessions.

§107.3 DEFINITIONS
Small unmanned aircraft: unmanned aircraft weighing less than 55 lbs (25 kg)
at takeoff, including everything on board.
sUAS (small UAS): the aircraft plus its communication links and any other
components required to control the aircraft.
Control station: the interface used by the remote pilot to control the sUAS.
Visual observer: a crew member who assists the remote pilot by maintaining
visual contact with the aircraft.
"""),

("I","14 CFR Part 107","§107.12 — Requirement for remote pilot certificate","regulations,certification","""
§107.12 REMOTE PILOT CERTIFICATE REQUIREMENT
No person may act as remote pilot in command of a small UAS unless that
person holds a remote pilot certificate with a small UAS rating issued by
the FAA.

§107.13 REGISTRATION
Any sUAS used in operations under Part 107 must be registered with the FAA.
Must be registered if aircraft weighs more than 0.55 lbs (250g) at takeoff.
Registration number must be displayed on the aircraft.
Register at: faadronezone.faa.gov — Fee: $5 per aircraft.

§107.15 CONDITION FOR SAFE OPERATION
Remote pilot must:
  (a) Ensure aircraft is in condition for safe operation
  (b) Discontinue flight when operation would pose undue hazard

§107.17 MEDICAL CONDITION
No person may manipulate the flight controls of a small UAS if they know
or have reason to know they have a physical or mental condition that would
interfere with the safe operation of the aircraft.

§107.19 REMOTE PILOT IN COMMAND
The remote pilot in command:
  (a) Is directly responsible for and is the final authority over the
      operation of the sUAS
  (b) Must ensure operations are conducted in compliance with Part 107
  (c) Has the final authority to abort the flight

§107.21 IN-FLIGHT EMERGENCY
In an in-flight emergency, the remote pilot may deviate from any rule to
the extent necessary to meet that emergency. Must report deviation to FAA
upon request.
"""),

("I","14 CFR Part 107","§107.23 — Hazardous operations","regulations,safety","""
§107.23 HAZARDOUS OPERATIONS
No person may operate a small UAS in a careless or reckless manner so as
to endanger the life or property of another.
No person may allow an object to be dropped from the aircraft in a manner
that creates undue hazard to persons or property on the surface.

§107.25 MOVING VEHICLES
No person may operate a small UAS from a moving aircraft.
A person may operate from a moving land or water vehicle if:
  — Operation is over a sparsely populated area OR
  — Over open water with no uninvolved persons on board

§107.27 ALCOHOL AND DRUGS
No person may act as pilot in command of a sUAS:
  — Within 8 hours of consuming alcohol (bottle to throttle rule)
  — With blood alcohol content of 0.04% or greater
  — Under the influence of alcohol
  — While using a drug that adversely affects safety

§107.29 DAYLIGHT OPERATIONS AND ANTI-COLLISION LIGHTING
(a) No person may operate a sUAS at night unless the aircraft has
    lighted anti-collision lighting visible for at least 3 statute miles.
(b) Civil twilight operations: also require the 3 SM anti-collision light.
CIVIL TWILIGHT: 30 minutes before official sunrise to 30 minutes after
official sunset.

§107.31 VISUAL LINE OF SIGHT AIRCRAFT OPERATION
The remote pilot, visual observer (if used), and person manipulating
flight controls must be able to see the aircraft at all times using
unaided vision (glasses/contacts OK — binoculars NOT permitted).
Must be able to see: aircraft altitude, attitude, location, and determine
the aircraft is not endangering persons or property.
"""),

("I","14 CFR Part 107","§107.35-§107.39 — Operations over people and vehicles","regulations,OOP","""
§107.35 MULTIPLE AIRCRAFT
A person may not operate or act as remote pilot in command of more than
one unmanned aircraft at the same time.

§107.37 RIGHT OF WAY
(a) Each small unmanned aircraft must yield the right of way to all other
    aircraft, including manned aircraft and other UAS.
(b) A remote pilot must NOT cause a collision hazard to other aircraft.

§107.39 OPERATIONS OVER PEOPLE
(a) No person may operate a small UAS over a human being unless:
  — The person is directly participating in the operation OR
  — The person is located under a covered structure protecting from
    a falling sUAS OR
  — Aircraft qualifies under Operations Over People rules (Categories 1-4)

OPERATIONS OVER PEOPLE — CATEGORIES (2021 Final Rule)
Category 1: Under 0.55 lbs (250g), no exposed rotating parts — fly over anyone
Category 2: FAA Declaration of Compliance (DOC) filed; injury severity limits met
Category 3: DOC filed; operation limited to controlled/restricted areas
Category 4: FAA Airworthiness Certificate issued; flight manual compliance

DJI MINI 4 PRO (249g): Category 1 eligible for recreational use.
DJI AIR 3 (720g): No OOP DOC filed — cannot fly over uninvolved people.

§107.45 OPERATIONS NEAR AIRCRAFT, RESTRICTED/PROHIBITED AREAS
No person may operate a sUAS in restricted or prohibited airspace
without ATC authorization, or where flight is not in the interest
of national defense or public safety or welfare.
"""),

("I","14 CFR Part 107","§107.51 — Operating limitations","regulations,altitude,visibility,speed","""
§107.51 OPERATING LIMITATIONS FOR SMALL UNMANNED AIRCRAFT

(a) ALTITUDE: Maximum 400 ft above ground level (AGL).
    Exception: May fly above 400 ft AGL if within 400 ft of a structure,
    then may fly up to 400 ft above the structure's highest point.

(b) AIRSPEED: Maximum groundspeed of 100 mph (87 knots).

(c) VISIBILITY: Minimum 3 statute miles from the remote pilot's
    control station.

(d) CLOUD CLEARANCE: Cannot fly in clouds. Must remain:
    — At least 500 ft below clouds
    — At least 2,000 ft horizontally from clouds

(e) No operation is allowed over any person not directly participating
    in the operation unless Categories 1-4 apply.

(f) Daylight or civil twilight only (unless anti-collision light used).

QUICK REFERENCE MINIMUMS TABLE:
  Altitude:   400 ft AGL maximum (or 400 ft above structure within 400 ft)
  Speed:      100 mph / 87 knots maximum
  Visibility: 3 statute miles minimum
  Clouds:     500 ft below / 2,000 ft horizontal
  Time:       Civil twilight with strobe, or daylight
"""),

("I","14 CFR Part 107","§107.57-§107.69 — Waivers and certificates","regulations,waiver","""
§107.57 OFFENSES INVOLVING ALCOHOL OR DRUGS
A conviction for violation of any Federal or State statute relating to
growing, processing, manufacturing, transporting, distributing, or
selling of narcotic drugs results in denial of application.

§107.59 REFUSAL TO SUBMIT TO TEST
Refusing to submit to an alcohol test or to furnish test results is
grounds for denial, suspension, or revocation of certificate.

§107.61 ELIGIBILITY: REMOTE PILOT IN COMMAND
To be eligible for a remote pilot certificate with sUAS rating:
  — Be at least 16 years of age
  — Be able to read, speak, write, and understand English
  — Be in a physical and mental condition to safely operate
  — Pass the initial aeronautical knowledge test (UA test) at an
    FAA-approved knowledge testing center

§107.65 AERONAUTICAL KNOWLEDGE RECENCY
Remote pilot must have passed the initial knowledge test within the
preceding 24 calendar months, OR have completed the online recurrent
training course within the preceding 24 calendar months.

§107.200-§107.205 WAIVERS
FAA may waive the following provisions:
  §107.25 — Moving vehicle operations
  §107.29 — Daylight operations  
  §107.31 — Visual line of sight
  §107.33 — Visual observer
  §107.35 — Multiple aircraft
  §107.37(a) — Right of way
  §107.39 — Over people (replaced by OOP rule)
  §107.41 — Airspace authorization (replaced by LAANC)
  §107.51 — Operating limitations

Application: faadronezone.faa.gov — must demonstrate equivalent safety.
BVLOS waiver processing time: typically 3-6 months.
"""),

("I","14 CFR Part 48 / Part 89","Registration and Remote ID","regulations,registration,Remote ID","""
PART 48 — REGISTRATION AND MARKING

Required if aircraft weighs MORE than 0.55 lbs (250g) at takeoff.
(Note: 0.55 lbs = 250 grams exactly)
Fee: $5 per aircraft.
Registration at: faadronezone.faa.gov
Certificate: Valid for 3 years.
Number marking: Must be displayed on exterior, readily accessible
for inspection — legible, in a permanent manner.

RECREATIONAL FLYERS (Section 336/49 USC 44809)
Recreational flyers register differently — one $5 registration covers
all aircraft. Number must still be displayed on each aircraft.
Must fly within guidelines of FAA-recognized Community-Based Organization.
TRUST (Recreational UAS Safety Test) required.

PART 89 — REMOTE ID (Effective March 16, 2024)

Standard Remote ID: broadcasts directly from aircraft.
Broadcasts: UAS ID (serial number), lat/lon/altitude of drone, velocity,
lat/lon/altitude of ground control station, time mark.
Range: broadcasts on WiFi and Bluetooth simultaneously.
DJI Air 3: Standard Remote ID — FAA DOC December 2023.
DJI Mini 4 Pro: Standard Remote ID — FAA DOC December 2023.
Enable in DJI Fly: Aircraft Settings → Safety → Remote ID → ON.
Must broadcast from engine start through shutdown.
Flying without Remote ID: Civil penalty up to $27,500.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN II — AIRSPACE
# ─────────────────────────────────────────────────────────────────────────────

("II","Airspace","Class A Airspace","airspace,Class A,IFR,18000 feet","""
CLASS A AIRSPACE
Definition: From 18,000 ft MSL to FL600 (60,000 ft MSL).
Coverage: Entire contiguous United States, Alaska, Hawaii, overlying waters
within 12 NM.
Requirements: IFR flight plan required. ATC clearance required.
UAS Operations: Virtually never authorized for drone operations.
Chart depiction: Not depicted on VFR sectional charts (above chart scope).
Communication: ATC contact required at all times.
Visibility: IFR rules apply — VFR minimums not applicable.
"""),

("II","Airspace","Class B Airspace","airspace,Class B,terminal,authorization,LAANC","""
CLASS B AIRSPACE
Definition: Surrounds nation's busiest airports (Primary airports).
Shape: Upside-down wedding cake — multiple layers with floor/ceiling.
Altitude depiction on chart: Upper/lower in hundreds of feet MSL.
  Example: "100/SFC" means from surface to 10,000 ft MSL.
Chart symbol: Solid blue lines.
Communications: Must establish two-way radio communication AND receive
explicit ATC clearance before entering.
Equipment: Mode C transponder required within 30 NM (Mode C veil).
Speed limit: 200 KIAS below Class B airspace; 250 KIAS at/below 10,000 ft.
Examples: LAX, JFK, ATL, DCA, ORD, DFW.

UAS / DRONE REQUIREMENTS IN CLASS B:
— Authorization REQUIRED to fly at any altitude, even 1 ft AGL.
— LAANC authorization: Most Class B has UAS Facility Map with altitude ceilings.
— LAANC ceiling of 0 ft = cannot use LAANC; must file manual DroneZone waiver.
— Common ceiling near primary airport: 0 ft (no drone ops).
— Common ceiling in outer rings: 50-200 ft.
"""),

("II","Airspace","Class C Airspace","airspace,Class C,approach control,LAANC","""
CLASS C AIRSPACE
Definition: Surrounds busy airports with operational control tower and
radar approach control.
Shape: Two concentric circles — inner (5 NM radius, SFC to 4,000 ft AGL)
and outer (10 NM radius, 1,200 ft to 4,000 ft AGL).
Chart symbol: Solid magenta lines.
Altitude depiction: Upper/lower in hundreds of feet MSL.
Communications: Must establish two-way radio communication with approach
control before entering. ATC clearance not explicitly required but
communication establishment is mandatory.
Equipment: Mode C transponder required.
Examples: PHX, PBI, SAT, SAN, MSP.

UAS REQUIREMENTS IN CLASS C:
— Authorization REQUIRED at all altitudes.
— LAANC available at most Class C airports.
— Typical ceiling in inner ring: 0-100 ft.
— Typical ceiling in outer ring: 100-400 ft.
— Must comply with any altitude ceiling assigned.
"""),

("II","Airspace","Class D Airspace","airspace,Class D,towered,LAANC","""
CLASS D AIRSPACE
Definition: Surrounds airports with an operational control tower.
Shape: Circular, typically 4-5 NM radius.
Altitude: Generally from surface to 2,500 ft AGL (shown in hundreds).
Chart symbol: Dashed blue lines.
Communications: Must establish two-way communication with tower before
entering. (Unlike Class C — just establishing comm is the requirement,
not ATC clearance per se.)
Hours: Only in effect when tower is operational. When tower closed:
Class D reverts to Class E or Class G depending on approach procedures.
Examples: HEF, HPN, PWM, FRD.

UAS REQUIREMENTS IN CLASS D:
— Authorization REQUIRED at any altitude.
— LAANC widely available at Class D airports.
— Must check if tower operational (hours vary).
— When tower closed: Class D may become Class G — check airspace.

IMPORTANT EXAM CONCEPT: LAANC authorization granted is the ceiling.
If LAANC grants 100 ft, you cannot fly above 100 ft even though Part 107
normally allows 400 ft.
"""),

("II","Airspace","Class E Airspace","airspace,Class E,controlled,IFR transition","""
CLASS E AIRSPACE (Controlled, but not Class A/B/C/D)
Definition: All other controlled airspace not classified A through D.
Starts at: 1,200 ft AGL over most of the country.
Starts at 700 ft AGL near airports with instrument approaches (depicted
as magenta shading on sectional charts).
Starts at surface at some airports (Class E surface area — dashed magenta).

CHART SYMBOLS FOR CLASS E:
— Magenta vignette (fuzzy boundary): floor at 700 ft AGL
— Blue vignette: floor at 1,200 ft AGL (outside these, Class G below)
— Dashed magenta: Class E surface area (SFC to ceiling above)
— Dashed blue: Airport without a tower but with IAP

WHEN TO READ CHART CAREFULLY:
— If dashed magenta exists: Class E starts at surface → LAANC required
— If no dashed boundary: Class G exists from surface to 700 or 1,200 ft

UAS REQUIREMENTS IN CLASS E:
— Surface area (dashed magenta): authorization required; LAANC may apply.
— Above 700 ft or 1,200 ft: authorization required.
— Below 700 ft or 1,200 ft (Class G): NO authorization required.
"""),

("II","Airspace","Class G Airspace","airspace,Class G,uncontrolled,no authorization","""
CLASS G AIRSPACE (Uncontrolled)
Definition: All airspace below the base of Class E that is not Class A/B/C/D.
Typical coverage: Surface to 700 ft AGL (near airports) or 1,200 ft AGL
(open country areas).
Chart symbol: NO specific symbol — it is the absence of any controlled
airspace boundaries.

UAS REQUIREMENTS IN CLASS G:
— NO authorization required for drone operations.
— May fly up to 400 ft AGL freely (weather/visibility permitting).
— Most rural areas have abundant Class G airspace below 1,200 ft AGL.
— Urban areas near airports may have very little Class G.

HOW TO IDENTIFY CLASS G ON A SECTIONAL:
1. Look for absence of any airspace boundary symbols.
2. Below the magenta vignette line = Class G (up to 700 ft AGL there).
3. Below the blue vignette (farther from airports) = Class G up to 1,200 ft.
4. If you are below ALL depicted airspace boundaries = Class G.

EXAM KEY POINT: The default assumption in the absence of a chart marking
is Class G from the surface. Look carefully for surface extensions
(dashed magenta) which eliminate the Class G surface layer.
"""),

("II","Airspace","Special Use Airspace","airspace,prohibited,restricted,MOA,ADIZ,warning","""
SPECIAL USE AIRSPACE

PROHIBITED AREAS (P-XXX)
— Flight of aircraft is prohibited.
— Established for national security, national welfare, or other reasons.
— No exceptions — even emergency deviation requires care.
— Examples: P-56 (Washington DC monuments), P-49 (nuclear facilities).
— Chart: Blue hatching with "P-XXX".

RESTRICTED AREAS (R-XXX)
— Contains hazardous activities (artillery, missiles, aerial gunnery).
— Flight NOT prohibited but requires permission or must verify inactive.
— When active: permission from controlling agency required.
— When inactive: may be used without restriction.
— Chart: Blue hatching with "R-XXX" and altitudes.
— Check NOTAMs or call controlling agency for active times.

WARNING AREAS (W-XXX)
— Similar hazards to restricted areas but over international waters.
— Non-regulatory — FAA cannot prohibit flight but warns of hazards.
— Chart: Blue hatching with "W-XXX".

MILITARY OPERATIONS AREAS (MOA)
— High-speed military training, acrobatics, formation flight.
— Civilian aircraft may transit when inactive; use caution when active.
— Check NOTAMs or call Flight Service for activity status.
— Chart: Magenta hatching, labeled with MOA name.

ALERT AREAS (A-XXX)
— High volume of pilot training or unusual aerial activity.
— Non-regulatory — no permission required; exercise caution.
— Chart: Magenta hatching with "A-XXX".

NATIONAL SECURITY AREAS (NSA)
— Increased security required for national security reasons.
— Voluntary avoidance requested (not prohibited by regulation).
— Chart: Magenta hatching with NSA label.

CONTROLLED FIRING AREAS
— Activities suspended when non-participating aircraft approach.
— NOT depicted on charts because they self-police.
"""),

("II","Airspace","Washington DC SFRA and FRZ","airspace,SFRA,FRZ,DC,Washington,prohibited","""
WASHINGTON DC SPECIAL FLIGHT RULES AREA (SFRA)

SFRA BOUNDARY: 30 NM radius centered on DCA (Ronald Reagan National Airport).
ALL aircraft including UAS must comply with SFRA procedures.
Known as the "Beltway of the Sky."

FLIGHT RESTRICTED ZONE (FRZ)
FRZ BOUNDARY: 15 NM radius centered on DCA.
Inside FRZ: No drone operations without specific FAA authorization
(virtually impossible to obtain for recreational/commercial UAS).
The FRZ covers: Washington DC, Arlington VA, parts of Maryland.

For UAS in the SFRA (15-30 NM ring):
— Authorization required (LAANC where available).
— Most locations have LAANC ceilings of 0 ft.
— 0 ft ceiling = cannot operate — manual DroneZone waiver needed.
— Processing time for waiver: months.

GEOGRAPHIC COVERAGE:
— All of Washington DC.
— All of Arlington County, VA.
— All of Alexandria City, VA.
— Most of Fairfax County, VA (Springfield, McLean, Tyson's Corner etc.)
— Parts of Montgomery County, MD and Prince George's County, MD.

HOW TO CHECK: FAA B4UFLY app, DJI Fly airspace map, Aloft app.
Always check specific location — LAANC ceiling varies by grid square.
A location 16 NM from DCA may have 0 ft ceiling; one 25 NM away may have 400 ft.
"""),

("II","Airspace","TFRs and NOTAMs","airspace,TFR,NOTAM,temporary,restriction","""
TEMPORARY FLIGHT RESTRICTIONS (TFRs) — 14 CFR §91.137-§91.145

TFRs override ALL other authorizations including LAANC.
Flying into a TFR = federal violation regardless of LAANC authorization.

TYPES OF TFRs:
(1) Disaster/Hazard TFR (§91.137): Wildfire, major disaster — protect
    emergency aircraft. Even "small" drones prohibited — they interfere
    with air tankers and helicopters at 500-2,000 ft.

(2) VIP TFR (§91.141): President, VP, foreign heads of state movement.
    Inner ring: 10-30 NM radius, no unauthorized flight.
    DC area Presidential TFR appears with little warning.
    Covers the entire flight path, not just destination.

(3) Space Operations TFR (§91.143): SpaceX, NASA launches.
    Extends along trajectory.

(4) Stadium TFR (§91.145): Sports events with 30,000+ attendance.
    3 NM radius, from 1 hour before kickoff to 1 hour after final whistle.
    Altitude: SFC to 3,000 ft AGL.
    Applies to: NFL, MLB, NCAA Division I football, NASCAR Cup races,
    major league soccer, Indianapolis 500.

READING A TFR NOTAM:
  !FDC NOTAM example:
  !DCA 0/1234 DCA/ZDC AIRSPACE TEMPORARY FLIGHT RESTRICTION ...
  FDC = Flight Data Center (nationwide distribution)
  ZDC = Washington ARTCC (area affected)

HOW TO CHECK TFRs:
— FAA TFR website: tfr.faa.gov
— FAA B4UFLY app
— 1800wxbrief.com
— DJI Fly airspace map (updates every 5 minutes)
— NotamSearch.faa.gov
"""),

("II","Airspace","LAANC and UAS Facility Maps","airspace,LAANC,authorization,UAS Facility Map","""
LAANC — LOW ALTITUDE AUTHORIZATION AND NOTIFICATION CAPABILITY

What it is: FAA automated system for near-instant airspace authorization
in controlled airspace (Class B, C, D, E surface areas).

How it works:
1. FAA creates UAS Facility Maps — grid squares around airports with
   pre-approved altitude ceilings (0, 50, 100, 200, 300, 400 ft AGL).
2. Pilots request authorization via LAANC-enabled app.
3. System checks requested location and altitude vs. UAS Facility Map.
4. If within ceiling: approval is instantaneous.
5. If above ceiling: must file manual DroneZone waiver.

LAANC-ENABLED APPS: DJI Fly, Aloft (formerly Kittyhawk), AirMap, ForeFlight.

ALTITUDE CEILING MEANINGS:
0 ft = Cannot fly here via LAANC; need manual waiver (rare to get).
50 ft = May fly up to 50 ft AGL (not 400 ft — the ceiling IS the limit).
100 ft = May fly up to 100 ft AGL.
400 ft = May fly full Part 107 altitude.

KEY EXAM POINT: If LAANC grants you 100 ft in a location where Part 107
normally allows 400 ft, you may ONLY fly to 100 ft. LAANC authorization
takes precedence.

Authorization parameters:
— Location (GPS coordinates + radius)
— Altitude (ceiling in ft AGL)
— Time window (start and end time)
— May not operate outside the authorized parameters.

BEYOND LAANC (DroneZone Waiver):
— Use: faadronezone.faa.gov
— For: Ceilings above LAANC, TFR areas, restricted areas.
— Processing: Days to weeks (not instant).
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN III — AVIATION WEATHER
# ─────────────────────────────────────────────────────────────────────────────

("III","Weather","METAR — Complete Decoder","weather,METAR,observation,surface,current","""
METAR — AVIATION ROUTINE WEATHER REPORT

Complete format:
METAR KDCA 011754Z 27012KT 10SM BKN025 OVC040 23/12 A2996 RMK AO2

Decoded:
METAR     = routine observation (SPECI = special observation)
KDCA      = station identifier (K = CONUS; KDCA = Reagan National, DC)
011754Z   = day 01, time 17:54 UTC (Zulu)
27012KT   = wind from 270° (west) at 12 knots
            (VRB = variable direction; /G## = gust speed)
            Example: 27012G22KT = gusts to 22 knots
10SM      = visibility 10 statute miles (not nautical miles)
            M1/4SM = less than 1/4 SM
BKN025    = broken clouds at 2,500 ft AGL
OVC040    = overcast at 4,000 ft AGL
23/12     = temperature 23°C / dew point 12°C
A2996     = altimeter setting 29.96 inHg
RMK AO2   = remarks: automated station with precipitation discriminator

CLOUD COVERAGE CODES:
SKC/CLR = sky clear (0 oktas)
FEW     = 1-2 oktas (1/8 to 2/8 coverage)
SCT     = scattered, 3-4 oktas (3/8 to 4/8)
BKN     = broken, 5-7 oktas (5/8 to 7/8) — IFR ceiling
OVC     = overcast, 8 oktas (total coverage) — IFR ceiling

CEILING DEFINITION: Lowest broken (BKN) or overcast (OVC) layer.
VFR MINIMUMS: ceiling ≥ 3,000 ft + visibility ≥ 5 SM.
IFR CONDITIONS: ceiling < 1,000 ft OR visibility < 3 SM.

WEATHER PHENOMENA CODES:
RA = rain | DZ = drizzle | SN = snow | FG = fog | BR = mist
TS = thunderstorm | SQ = squall | GR = hail | IC = ice crystals
VC = vicinity (within 10 SM of station)
— = light | (no prefix) = moderate | + = heavy
Example: -TSRA = light thunderstorm with rain
         +RASN = heavy rain and snow
"""),

("III","Weather","TAF — Terminal Aerodrome Forecast","weather,TAF,forecast,terminal","""
TAF — TERMINAL AERODROME FORECAST

Issued by: NWS 4 times daily (0000, 0600, 1200, 1800 UTC).
Valid for: 24 or 30 hours. Covers 5 SM radius around airport.

Example TAF:
TAF
KDCA 011730Z 0118/0224 27012KT P6SM BKN025
     TEMPO 0118/0120 5SM -TSRA BKN010
     FM022200 31008KT P6SM SKC

Decoded:
KDCA     = station
011730Z  = issued day 01 at 17:30 UTC
0118/0224= valid from day 01 at 1800Z to day 02 at 2400Z
27012KT  = wind 270° at 12 kts
P6SM     = visibility greater than 6 SM
BKN025   = broken at 2,500 ft AGL (base forecast)

TEMPO 0118/0120 = temporary conditions 1800-2000Z day 01:
  5SM -TSRA BKN010 = vis 5 SM, light thunderstorm/rain, broken 1,000 ft

FM022200 = from day 02 at 2200Z:
  31008KT P6SM SKC = wind 310°/8kts, vis >6SM, sky clear

CHANGE INDICATORS:
FM (from): permanent change in conditions
TEMPO: temporary conditions lasting < 1 hour, occurring < half the time
PROB30/PROB40: 30% or 40% probability of conditions
BECMG: gradual change expected over specified period
"""),

("III","Weather","SIGMETs and AIRMETs","weather,SIGMET,AIRMET,advisory,hazard","""
SIGMET — SIGNIFICANT METEOROLOGICAL INFORMATION
Issued by: Aviation Weather Center (AWC).
Covers: Significant weather hazardous to ALL aircraft.
Valid: 4 hours (6 hours for hurricanes/tropical storms).

SIGMET covers:
— Severe or extreme turbulence (not associated with thunderstorms)
— Severe or extreme icing (not associated with thunderstorms)
— Dust storms, sandstorms lowering visibility below 3 SM
— Volcanic ash
— Embedded thunderstorms
— Squall lines

CONVECTIVE SIGMET (WST):
— Thunderstorm-related hazards.
— Issued hourly + special issues.
— Covers: Severe thunderstorms (hail ≥3/4", wind ≥50 kts, tornadoes).
— Embedded thunderstorms.
— Lines of thunderstorms.
— Areas of thunderstorms ≥40% coverage.

AIRMET — AIRMEN'S METEOROLOGICAL INFORMATION
Issued by: Aviation Weather Center.
Covers: Hazardous weather of operational interest to light aircraft and IFR.
Valid: 6 hours.
Issued: Every 6 hours (more frequently as needed).

THREE TYPES OF AIRMET:
AIRMET SIERRA (S): IFR conditions and/or mountain obscuration.
  — Ceilings < 1,000 ft and/or visibility < 3 SM over 50% of area.
  — Mountain tops obscured by clouds/precipitation.

AIRMET TANGO (T): Moderate turbulence, strong surface winds, low-level
  wind shear.
  — Moderate turbulence below 18,000 ft.
  — Sustained surface winds ≥ 30 knots.

AIRMET ZULU (Z): Moderate icing and freezing level heights.
  — Moderate icing below 18,000 ft.
  — Freezing level information.

UAS SIGNIFICANCE: Any SIGMET or AIRMET Tango in your area = strong
consideration for delaying or cancelling flight.
"""),

("III","Weather","Density Altitude","weather,density altitude,performance,temperature,altitude","""
DENSITY ALTITUDE

Definition: Pressure altitude corrected for non-standard temperature.
Represents: The altitude at which the aircraft "thinks" it is flying
in terms of aerodynamic performance.

FORMULA:
Density Altitude = Pressure Altitude + (120 × (OAT - ISA Temp))

Where:
  Pressure Altitude = Field elevation + (29.92 - altimeter setting) × 1,000
  ISA Temperature = 15°C - (2°C × altitude in thousands of feet)
  OAT = Outside Air Temperature in °C

SIMPLIFIED RULE OF THUMB:
For every 1,000 ft increase in altitude: performance decreases.
For every 10°C above standard: performance decreases as if altitude
increased by ~1,700 ft.

EXAMPLE CALCULATION:
Airport elevation: 5,000 ft
Altimeter: 29.42 inHg
Temperature: 30°C
Pressure Alt = 5,000 + (29.92 - 29.42) × 1,000 = 5,500 ft
ISA temp at 5,500 ft = 15 - (2 × 5.5) = 4°C
Density Alt = 5,500 + (120 × (30 - 4)) = 5,500 + 3,120 = 8,620 ft

WHY IT MATTERS FOR DRONES:
— Higher density altitude = thinner air = less lift per rotor rotation
— Battery drain increases (motors work harder)
— Max altitude and speed may decrease
— Particularly relevant: high elevation airports in summer

MEMORY AID: "High, Hot, Humid" = high density altitude = poor performance.
"""),

("III","Weather","Wind and Turbulence","weather,wind,turbulence,wind shear,microburst","""
WIND AND TURBULENCE

WIND TERMINOLOGY:
Headwind: wind blowing toward the aircraft — increases lift/performance.
Tailwind: wind blowing behind aircraft — decreases performance.
Crosswind: wind blowing across flight path.
Sustained wind: steady wind speed.
Gust: rapid increases in wind speed above sustained, then rapid decrease.

TURBULENCE INTENSITY (PIREP SCALE):
Light: slight erratic changes in attitude and/or altitude.
Moderate: similar but greater intensity; aircraft control still maintained.
Severe: large abrupt changes; aircraft may be temporarily out of control.
Extreme: violently tossed, practically impossible to control.

WIND SHEAR:
Definition: A change in wind speed and/or direction over a short distance.
Can occur: vertically or horizontally.
Microburst: A severe form of wind shear caused by strong downdraft from
thunderstorm. Creates:
  — Strong downdraft in center.
  — Outflow in all directions at surface.
  — Encounter sequence: headwind (lift increases) → downdraft → tailwind
    (lift decreases rapidly) — most dangerous phase.
  — Can generate winds of 45+ knots.
  — Lifetime: < 15 minutes typically.
  — Diameter: typically 1-2 miles.

MECHANICAL TURBULENCE: Created by wind flowing over buildings, terrain,
trees. Increases near ground in windy conditions.
Significant for low-altitude drone operations near structures.

THERMAL TURBULENCE: Rising columns of warm air (thermals).
Strongest: 10 AM - 3 PM on sunny days over dark/paved surfaces.
Best flying time for smooth conditions: early morning.
"""),

("III","Weather","Fog and Clouds","weather,fog,visibility,IFR,clouds","""
FOG TYPES AND FORMATION

RADIATION FOG (most common in valleys):
— Forms on clear, calm nights with high humidity.
— Ground radiates heat, surface cools, water vapor condenses.
— Thickest before sunrise; burns off after sunrise.
— Found in valleys, low-lying areas.

ADVECTION FOG:
— Warm moist air moves (advects) over cool surface.
— Common along coastlines (fog rolls in from ocean).
— Not limited to nighttime — can persist all day.
— Can be very thick and extensive.

UPSLOPE FOG:
— Moist air forced up terrain slope, cools, condenses.
— Common on windward sides of mountains.

PRECIPITATION-INDUCED FOG:
— Warm rain falls through cool air → evaporation → saturation.
— Often associated with frontal passages.

STEAM FOG (Arctic smoke):
— Cold air moves over warmer water → evaporation → fog.

DEW POINT SPREAD:
When temperature - dew point = 4°F (2°C) or less, fog or low clouds
are likely forming or will form.
Example: Temp 55°F, Dew Point 52°F → spread = 3°F → fog likely.

VISIBILITY EFFECTS:
Mist (BR): 5/8 to 6 SM visibility.
Fog (FG): < 5/8 SM visibility.
Part 107 requires ≥3 SM visibility — fog can quickly violate this.
"""),

("III","Weather","Thunderstorms and Icing","weather,thunderstorm,icing,hazard","""
THUNDERSTORMS

THREE REQUIREMENTS (all must exist):
1. Sufficient moisture in atmosphere.
2. Source of lift (frontal, orographic, convective heating).
3. Unstable atmosphere (temperature decreases rapidly with altitude).

THREE STAGES:
(1) Cumulus: building phase, updrafts dominate, precipitation forming.
(2) Mature: most hazardous — updrafts AND downdrafts coexist, lightning,
    heavy precipitation, hail, wind shear, turbulence.
(3) Dissipating: downdrafts dominate, precipitation decreases, anvil head.

HAZARDS TO UAS:
— Lightning: strikes most likely near cell edges.
— Hail: can extend 20 miles from visible storm.
— Turbulence: severe/extreme in and near cells.
— Wind shear/microburst: below cell base.
— Downdrafts: can overwhelm climb performance.

RULE: NEVER fly near a thunderstorm. No exceptions.
If you can hear thunder, you are within lightning strike range (10 miles).
FAA: avoid thunderstorm cells by 20 miles.

ICING
Forms when supercooled water droplets (liquid below 0°C) hit aircraft surface.
Most common: 0°C to -20°C with visible moisture (clouds, precipitation).
Types:
  Clear ice: most serious — heavy, hard, irregular shape.
  Rime ice: rough, milky, opaque — less structurally damaging.
  Mixed ice: combination.

UAS CONSIDERATIONS:
— Most civilian drones NOT certified for flight into known icing.
— Ice adds weight, disrupts aerodynamics, can jam rotating parts.
— If temperature near or below 0°C with clouds/precipitation: do not fly.
"""),

("III","Weather","Weather Products and Sources","weather,PIREP,AWOS,ASOS,briefing","""
WEATHER INFORMATION SOURCES

PIREP — PILOT REPORT
Filed by pilots in flight; most current "truth" about actual conditions.
Format: UA/OV/TM/FL/TP/SK/WX/TA/WV/TB/IC/RM
  UA = routine PIREP (UUA = urgent)
  OV = location (over)
  TM = time (UTC)
  FL = altitude (FL180 = 18,000 ft)
  TP = aircraft type
  SK = sky conditions
  WX = weather and visibility
  TA = air temperature
  WV = wind (degrees/knots)
  TB = turbulence (NEG/SMTH/LGT/MDT/SEV/EXTM)
  IC = icing (NEG/TRACE/LGT/MDT/SEV/TRACE to SEV)
  RM = remarks

AWOS — AUTOMATED WEATHER OBSERVING SYSTEM
— Automated stations at smaller airports.
— Broadcasts on airport frequency (also available by phone).
— Types: AWOS-1 (winds, altimeter) through AWOS-3+ (full observation).

ASOS — AUTOMATED SURFACE OBSERVING SYSTEM
— More sophisticated automation; generates METARs.
— Located at major airports; feeds official observations.

ATIS — AUTOMATIC TERMINAL INFORMATION SERVICE
— Pre-recorded broadcast updated hourly (or when conditions change).
— Contains: time, ceiling/visibility, temp/dewpoint, wind, altimeter,
  active runways, approach in use, NOTAMs.
— Each update assigned a letter (A=Alpha, B=Bravo...).
— Pilot must say "have information [letter]" on initial ATC contact.
— Frequencies: 118-137 MHz VHF; accessible via the ATC listener agent.

FAA WEATHER RESOURCES:
— aviationweather.gov: official FAA weather products.
— 1800wxbrief.com: full weather briefing service (required for Part 107 ops).
— api.weather.gov: NOAA free API for current conditions.
— windy.com / Weather Underground: consumer weather (supplement only).
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN IV — LOADING AND PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

("IV","Loading and Performance","Weight and Balance Principles","loading,CG,weight,balance","""
WEIGHT AND BALANCE

CENTER OF GRAVITY (CG):
— The point about which the aircraft would balance if suspended.
— Must remain within prescribed limits (CG envelope) for controllable flight.
— Forward CG: tends to nose-heavy, more stable but less maneuverable.
— Aft CG: less stable, potentially uncontrollable.

BASIC WEIGHT AND BALANCE FORMULA:
Weight × Arm = Moment
Total Moment / Total Weight = CG Location

For UAS, the manufacturer sets the CG envelope in the flight manual.
Adding payload (camera, accessories) shifts CG.
Example: Adding a gimbal/camera below the CG lowers the CG → more pendulum
stability but can affect dynamic flight characteristics.

MAXIMUM GROSS WEIGHT:
— Never exceed the maximum takeoff weight in the UAS manual.
— Exceeding MGTOW: reduced control authority, longer takeoff distance,
  reduced battery life, possible structural stress.
— Part 107: sUAS must weigh less than 55 lbs (25 kg) AT TAKEOFF.
  This includes everything: aircraft + payload + batteries.

BATTERY WEIGHT CHANGES:
— Batteries are the heaviest component of most small UAS.
— A discharged battery weighs essentially the same as a full one.
— Multiple batteries (if modular): changing in flight shifts CG.

PAYLOAD PLACEMENT:
— Most consumer drones (DJI Mini 4 Pro, Air 3): fixed payload (camera).
— Custom/enterprise drones: payload placement affects CG significantly.
— Follow manufacturer guidance for payload CG limits.
"""),

("IV","Loading and Performance","Performance Factors","loading,performance,density altitude,battery","""
PERFORMANCE FACTORS FOR SMALL UAS

DENSITY ALTITUDE EFFECT:
— High DA = thinner air = propellers generate less thrust per revolution.
— Motors run harder = higher current draw = faster battery drain.
— Reduced hover efficiency = shorter flight time.
— Higher minimum rotor RPM needed = less speed margin before stall.
— Rule: At high-altitude/high-temperature airfields, expect 10-20% less
  flight time than at sea level standard day.

TEMPERATURE EFFECTS ON BATTERY:
— LiPo/LiHV batteries used in DJI drones perform best at 15-25°C.
— Cold weather (<10°C): capacity reduced, sag under load increases.
  DJI recommends warming battery before flight in cold weather.
  Below 0°C: significant capacity reduction (up to 40% in extreme cold).
— Hot weather (>40°C): risk of thermal runaway if also under heavy load.
  Avoid charging or operating in direct sun at very high temperatures.

WIND EFFECTS ON PERFORMANCE:
— Headwind: increases effective lift, reduces ground speed.
— Tailwind: decreases effective lift. Maximum groundspeed: 100 mph (§107.51).
— Crosswind: requires constant correction, increases power consumption.
— Strong winds reduce flight time due to increased power needed.
— DJI Mini 4 Pro max wind resistance: Level 5 (38 kph / ~24 mph).
— DJI Air 3 max wind resistance: Level 6 (46 kph / ~29 mph).

OBSTACLE AVOIDANCE:
— APAS 5.0 (DJI Mini 4 Pro, Air 3): active obstacle avoidance.
— DISABLED at night (cameras can't see obstacles).
— May be less effective in low light or rain.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN V — EMERGENCY PROCEDURES
# ─────────────────────────────────────────────────────────────────────────────

("V","Emergency Procedures","Lost Link and Flyaway","emergency,lost link,RTH,flyaway","""
EMERGENCY PROCEDURES

LOST LINK (Radio Communication Lost):
Definition: Loss of control data link between control station and aircraft.
What DJI aircraft do (configurable):
  1. Hovers for ~3 seconds (waits for link to restore).
  2. Returns to Home (RTH) if link not restored.
  3. RTH altitude must be set ABOVE all obstacles in return path.
  4. Lands at home point.

REMOTE PILOT ACTIONS ON LOST LINK:
1. Do not panic — the drone is executing pre-programmed failsafe.
2. Stop moving to allow re-acquisition of GPS/signal.
3. Ensure antenna of controller points toward aircraft.
4. Move to higher ground or away from interference sources.
5. If drone is returning to home, prepare for landing.
6. Keep spectators clear of landing zone.

FLYAWAY:
Definition: Drone flies in unintended direction or fails to respond to inputs.
Causes: Compass interference, GPS spoofing, software error, EMI.
Actions:
1. Attempt to regain control — try switching flight modes.
2. Activate RTH.
3. Activate motors-off/kill switch if aircraft is about to cause harm.
4. If aircraft goes out of VLOS: cease operation — you have lost compliance.

REPORTING ACCIDENTS (§107.9):
Must report to FAA within 10 days if operation results in:
— Serious injury to any person, OR
— Loss of consciousness of any person, OR
— Property damage (not counting UAS itself) totaling more than $500.
Report to: FAA UAS Accident Reporting — FAA Safety Hotline.
"""),

("V","Emergency Procedures","Motor Failure and Emergency Landing","emergency,motor failure,landing","""
MOTOR FAILURE

Multi-rotor UAS (quadcopter, hexacopter, octocopter):
— Quadcopter losing one motor: likely crash — cannot maintain stable flight.
— Hexacopter losing one motor: may maintain reduced control.
— Octocopter losing one motor: can usually fly safely.

Signs of motor failure:
— Sudden yaw or bank in one direction.
— Rapid altitude loss.
— Unusual vibration or noise.
— Sudden battery drain increase.

ACTIONS:
1. Immediately assess altitude and position.
2. Attempt controlled descent to safest available area.
3. Clear of people: execute controlled crash into open area.
4. Over people: execute best possible avoidance maneuver first.
5. Never attempt to catch a malfunctioning drone — serious injury risk.

EMERGENCY LANDING SITE SELECTION PRIORITY:
1. Open field with no people.
2. Unpaved area without people.
3. Paved area cleared of people.
4. Water (if no alternative and aircraft will not harm others).

PROPELLER FAILURE:
— Similar to motor failure in effect.
— Can occur from obstruction, defect, or loose fitting.
— Pre-flight check: confirm all props are undamaged and properly secured.
— Post-crash: NEVER approach spinning props. Wait for full stop.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN VI — CREW RESOURCE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

("VI","CRM","Hazardous Attitudes and Antidotes","CRM,attitude,decision making,hazardous","""
FIVE HAZARDOUS ATTITUDES AND THEIR ANTIDOTES
(Critical Part 107 exam topic — all 5 must be memorized)

1. ANTI-AUTHORITY: "Don't tell me what to do."
   Antidote: "Follow the rules. They are usually right."
   Example: Ignoring TFR because "it's just a temporary restriction."

2. IMPULSIVITY: "Do something — quickly!"
   Antidote: "Not so fast. Think first."
   Example: Rushing to launch before pre-flight check because the client
   is waiting.

3. INVULNERABILITY: "It won't happen to me."
   Antidote: "It could happen to me."
   Example: Flying near a thunderstorm because "I've done it before."

4. MACHO: "I can do it."
   Antidote: "Taking chances is foolish."
   Example: Flying in gusty winds beyond aircraft limits to "prove" skill.

5. RESIGNATION: "What's the use?"
   Antidote: "I'm not helpless. I can make a difference."
   Example: Continuing a deteriorating flight situation and doing nothing
   because "it probably won't end well anyway."

RECOGNITION AND RESPONSE:
Step 1: Recognize a potentially hazardous attitude forming.
Step 2: Label the thought (e.g., "That's anti-authority thinking").
Step 3: Apply the correct antidote.
Step 4: Make a safe decision based on facts.
"""),

("VI","CRM","Risk Management — PAVE and IMSAFE","CRM,risk,PAVE,IMSAFE,preflight","""
RISK MANAGEMENT CHECKLISTS

PAVE CHECKLIST — Pre-flight risk assessment:
P — PILOT: Am I current? Trained? Rested? Healthy?
A — AIRCRAFT: Is the sUAS airworthy? Pre-flight complete? Battery charged?
V — EnVironment: Weather acceptable? Airspace clear? Site survey done?
E — External pressures: Am I being rushed? Feeling pressure to complete?

IMSAFE — Personal checklist for pilot fitness:
I — Illness: Am I sick? Any symptoms that could affect performance?
M — Medication: Am I taking prescription or OTC drugs that could impair?
S — Stress: Am I under psychological pressure that could distract?
A — Alcohol: Have I consumed alcohol within 8 hours? BAC below 0.04%?
F — Fatigue: Am I rested? Fatigue is a major cause of accidents.
E — Emotion: Am I emotionally upset? Can I concentrate fully?

3P MODEL — Perceive, Process, Perform:
Perceive: Gather all information about current situation.
Process: Analyze the risk using tools like PAVE.
Perform: Make a decision and act; monitor for changes.

DECIDE MODEL — 6-step aeronautical decision making:
D — Detect: Identify a change has occurred.
E — Estimate: Assess the need to counter the change.
C — Choose: Select a course of action.
I — Identify: Determine actions needed to implement the choice.
D — Do: Implement the chosen action.
E — Evaluate: Monitor the outcome; cycle back to Detect if needed.

TASK MANAGEMENT / WORKLOAD:
— Prioritize: Safety > Navigation > Communication > Other.
— Avoid task saturation — one task at a time under stress.
— Distribute tasks between pilot and visual observer when possible.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN VII — RADIO COMMUNICATIONS
# ─────────────────────────────────────────────────────────────────────────────

("VII","Radio Communications","Phonetic Alphabet and ATC Phraseology","radio,ATC,phonetic,communication","""
PHONETIC ALPHABET (ICAO) — Must know all 26

A - Alpha      N - November
B - Bravo      O - Oscar
C - Charlie    P - Papa
D - Delta      Q - Quebec
E - Echo       R - Romeo
F - Foxtrot    S - Sierra
G - Golf       T - Tango
H - Hotel      U - Uniform
I - India      V - Victor
J - Juliet     W - Whiskey
K - Kilo       X - X-ray
L - Lima       Y - Yankee
M - Mike       Z - Zulu

NUMBERS: Pronounced individually.
0 = Zero  1 = One  2 = Two  3 = Three  4 = Four  5 = Five
6 = Six   7 = Seven  8 = Eight  9 = Niner (not "nine" — avoids confusion with German "nein")
Altitude: "One thousand five hundred" (not "fifteen hundred" for ATC).

COMMON ATC PHRASES:
"Cleared": you have permission to proceed.
"Hold short": do not cross the runway/taxiway.
"Say again": repeat your last transmission.
"Roger": I have received your last transmission (NOT "okay" or "will comply").
"Wilco": I have received your message and will comply.
"Unable": cannot comply with clearance/instruction.
"Standby": wait — I will get back to you.
"Traffic": I am alerting you to other aircraft.
"Ident": activate transponder identification.
"Squawk XXXX": set transponder to assigned code.
"Mayday Mayday Mayday": distress — immediate danger to life.
"Pan Pan Pan": urgency — serious but not immediate danger.
"""),

("VII","Radio Communications","ATC Light Gun Signals","radio,light gun,tower,no radio","""
ATC LIGHT GUN SIGNALS
Used when radio communication is not possible.
Remote pilots should recognize signals directed at their aircraft or area.

FOR AIRCRAFT ON THE GROUND:
Steady GREEN:    Cleared for takeoff.
Flashing GREEN:  Cleared to taxi.
Steady RED:      Stop.
Flashing RED:    Taxi clear of runway in use.
Flashing WHITE:  Return to starting point on airport.
Alternating R/G: General warning — exercise extreme caution.

FOR AIRCRAFT IN FLIGHT:
Steady GREEN:    Cleared to land.
Flashing GREEN:  Return for landing (to be followed by steady green).
Steady RED:      Give way to other aircraft; continue circling.
Flashing RED:    Airport unsafe — do not land.
Flashing WHITE:  Not applicable for aircraft in flight.
Alternating R/G: General warning — exercise extreme caution.

MEMORY AID:
Green = Go (like a traffic light).
Red = Stop/Danger.
Flashing = Conditional/Taxi.
Steady = Final clearance.

UAS APPLICATION:
If operating near an airport and tower attempts light gun contact:
— Stop and acknowledge (rock wings if manned; hover/rock UAS).
— Comply with signal.
— Contact ATC by phone after landing.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN VIII — UAS PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

("VIII","UAS Performance","Computing Density Altitude","performance,density altitude,formula,calculation","""
COMPUTING DENSITY ALTITUDE — STEP BY STEP

STEP 1: Find Pressure Altitude.
Pressure Altitude = Field Elevation + (29.92 - Actual Altimeter) × 1,000

Example: Field elevation 1,000 ft, altimeter 29.42 inHg
PA = 1,000 + (29.92 - 29.42) × 1,000 = 1,000 + 500 = 1,500 ft

STEP 2: Find ISA Standard Temperature at Pressure Altitude.
ISA Temp (°C) = 15 - (2 × PA in thousands of feet)
At 1,500 ft: ISA = 15 - (2 × 1.5) = 15 - 3 = 12°C

STEP 3: Calculate Density Altitude.
DA = PA + 120 × (Actual OAT - ISA Temp)
DA = 1,500 + 120 × (OAT - 12)

Example with OAT = 32°C:
DA = 1,500 + 120 × (32 - 12) = 1,500 + 2,400 = 3,900 ft

INTERPRETATION: The aircraft at this airport on this day performs as if
it were flying at 3,900 ft on a standard day — despite being at 1,000 ft.

QUICK RULE OF THUMB: For each 1,000 ft of density altitude above sea level:
— Flight time decreases approximately 5-10%.
— Maximum climb rate decreases.
— Motors run at higher percentage of maximum power.

PERFORMANCE CHART USAGE:
Many aircraft performance charts use:
  — Pressure altitude (horizontal axis)
  — Temperature (multiple curves)
To find: Max altitude, hover efficiency, flight time.
Enter with your pressure altitude and OAT → read performance value.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN IX — PHYSIOLOGICAL EFFECTS
# ─────────────────────────────────────────────────────────────────────────────

("IX","Physiology","Alcohol, Drugs, and Fatigue","physiology,alcohol,drugs,fatigue,fitness","""
PHYSIOLOGICAL FACTORS AFFECTING UAS OPERATIONS

ALCOHOL (§107.27)
Legal limits:
— No alcohol within 8 hours before acting as remote pilot ("bottle to throttle").
— Blood alcohol content (BAC) must be below 0.04%.
  (Note: DUI limit for driving is 0.08% — FAA limit is half of that.)
— No flying while impaired regardless of time elapsed.
Alcohol effects: Impairs judgment before affecting physical coordination.
"I feel fine" is NOT a valid safety check — impairment can be subtle.
Hangover: Dehydration, impaired judgment can persist well beyond 8 hours.

DRUGS AND MEDICATIONS
Over-the-counter (OTC) drugs affecting flight safety:
— Antihistamines (Benadryl, etc.): sedation, slowed reaction time.
— Decongestants (pseudoephedrine): cardiovascular effects.
— Sleep aids: drowsiness can persist to next day.
— Pain relievers (opioids): strong impairment.
Rule: If medication has ANY label warning about driving, do not fly.
If in doubt, do not fly. Consult an Aviation Medical Examiner (AME).

FATIGUE
One of the most dangerous and underestimated hazards.
Effects: Similar to intoxication — slower reaction, poor decisions, tunnel vision.
Cumulative fatigue: multiple nights of inadequate sleep = increasing impairment.
Countermeasures: Adequate sleep (7-8 hours), scheduled breaks, avoid
  scheduling complex operations when fatigued.

HYPOXIA (low oxygen at altitude)
Generally not significant for UAS operators (at ground level).
High-altitude operations (mountains): operators at 8,000+ ft elevation may
experience mild hypoxia effects — use supplemental oxygen if concerned.
Symptoms: Headache, dizziness, fatigue, impaired judgment.

STRESS AND ANXIETY
Narrowing of attention ("tunnel vision") under high stress.
Pre-flight stress management: use checklists, take time, breathe.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN X — AERONAUTICAL DECISION MAKING
# ─────────────────────────────────────────────────────────────────────────────

("X","ADM","Aeronautical Decision Making Models","ADM,DECIDE,3P,risk,judgment","""
AERONAUTICAL DECISION MAKING (ADM)

SINGLE-PILOT RESOURCE MANAGEMENT (SRM):
The art of managing all resources available (information, equipment,
technology, people) to ensure a successful outcome.
Key resources: Autopilot/automation, weather data, charts, visual observer.

AUTOMATION COMPLACENCY:
Risk of over-relying on automated systems (APAS, GPS hold, RTH).
Automation can fail. Remote pilot must be prepared to take manual control
and has ultimate responsibility for the operation.

SITUATIONAL AWARENESS:
Knowing what is happening around you: aircraft position, attitude, altitude,
other aircraft, people on the ground, changing weather, battery status.
Loss of SA is a precursor to accidents.
Maintaining SA with automation: actively monitor, don't just trust the screen.

RISK MANAGEMENT MATRIX:
         | Unlikely | Possible | Probable |
Low harm | Accpt    | Accpt    | Review   |
Mod harm | Accpt    | Review   | Avoid    |
High harm| Review   | Avoid    | Avoid    |
Accpt = Acceptable; Review = Monitor carefully; Avoid = Do not proceed.

FOUR FUNDAMENTAL RISKS (CARE):
C — Consequences: What could go wrong and how bad?
A — Alternatives: Is there another way to accomplish the mission safely?
R — Reality: Am I being honest about the situation?
E — External pressures: Is something pushing me to take unnecessary risk?

GO/NO-GO DECISION FACTORS:
— Weather at or deteriorating toward minimums → No-Go.
— Equipment with known faults → No-Go.
— Pilot not current or fit → No-Go.
— Operational pressure to rush → Warning sign; take extra care.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN XI — AIRPORT OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

("XI","Airport Operations","Runway Markings and Signs","airport,runway,markings,signs,incursion","""
AIRPORT RUNWAY MARKINGS

RUNWAY DESIGNATOR:
Numbers indicate magnetic heading divided by 10, rounded to nearest 10°.
Runway 27 = magnetic heading of approximately 270° (west).
Parallel runways: L (left), R (right), C (center). Example: 27L, 27R.

RUNWAY MARKINGS (VFR):
White centerline: Dashed white line down center.
Threshold marking: Eight white stripes perpendicular to centerline (start of landing area).
Displaced threshold: White arrows on pavement; threshold stripe at displaced point.
Aiming point: Two large white rectangles 1,000 ft from threshold.
Touchdown zone: Groups of white rectangles showing distance from threshold.

TAXIWAY MARKINGS:
Yellow centerline: Continuous yellow line.
Edge markings: Continuous yellow lines.
Holding position (runway): Yellow solid/dashed lines (hold short markings).
   Double yellow solid lines toward runway = stop here before entering.
   Four yellow lines (two solid, two dashed) = runway hold position.

AIRPORT SIGNS:
Red background = Mandatory instruction (STOP/DO NOT ENTER).
  — Runway designator: "27-9" (you are holding short of/entering runway).
  — NO ENTRY sign (circle with slash).
Yellow background = Location or direction.
  — Taxiway location: black letter on yellow.
  — Direction/destination: yellow on black.

RUNWAY INCURSION:
Any unauthorized or incorrect presence of aircraft/vehicle on protected runway.
Prevention: Stop and confirm clearance before crossing any hold short line.
"When in doubt, don't." Always confirm with ATC before runway entry.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# DOMAIN XII — MAINTENANCE AND PREFLIGHT
# ─────────────────────────────────────────────────────────────────────────────

("XII","Maintenance","Preflight Inspection and Maintenance","maintenance,inspection,airworthiness,preflight","""
PREFLIGHT INSPECTION PROCEDURE

PRE-FLIGHT CHECKLIST (general UAS — adapt to aircraft type):
1. Site survey: Identify hazards, people, obstacles, emergency landing areas.
2. Weather check: METAR, TAF, winds, visibility — confirm minimums met.
3. Airspace check: LAANC, NOTAMs, TFRs — confirm authorization obtained.
4. Equipment check:
   □ Drone: No visible damage to frame, arms, motors, props.
   □ Propellers: No cracks, chips, bends. Properly and securely attached.
   □ Battery: Charged to appropriate level. No swelling, damage, or heat.
   □ Remote controller: Charged. Antennas properly positioned.
   □ GPS: Acquiring sufficient satellites (≥6 for stable flight; ≥8 preferred).
   □ Compass calibration: Completed if in new location or after long storage.
   □ Remote ID: Broadcasting confirmed.
   □ Camera/gimbal: Functioning. Memory card installed.
   □ Return-to-Home altitude: Set above all obstacles in RTH path.
5. Software/firmware: Check for updates. Do NOT update firmware immediately
   before a critical operation — update with time to test.

BATTERY MANAGEMENT:
— Never fly below 20% battery (manufacturer warning levels apply).
— LiPo storage voltage: 3.7-3.85V per cell (not full charge for long storage).
— Never store fully charged or fully discharged LiPo long-term.
— Swell/puffed batteries: REMOVE FROM SERVICE IMMEDIATELY. Fire risk.
— Cold weather: pre-warm batteries (hover briefly at low altitude).

MAINTENANCE LOG:
No specific Part 107 logging requirement for maintenance records.
Best practice: Log maintenance, crashes, battery cycles for safety tracking.
Any crash: Full inspection before return to service.
"""),

# ─────────────────────────────────────────────────────────────────────────────
# BONUS: FORMULAS AND TABLES
# ─────────────────────────────────────────────────────────────────────────────

("REF","Reference","Part 107 Quick Reference Card","reference,formula,table,quick reference","""
PART 107 QUICK REFERENCE — ALL KEY NUMBERS

REGISTRATION THRESHOLDS:
> 0.55 lbs (250g) = must register with FAA at faadronezone.faa.gov
< 0.55 lbs = registration NOT required for recreational (still good practice)
Part 107 commercial: register regardless of weight

OPERATING LIMITS (§107.51):
Altitude: 400 ft AGL (or 400 ft above structure within 400 ft of it)
Speed: 100 mph / 87 knots maximum
Visibility: 3 statute miles minimum
Clouds: 500 ft below / 2,000 ft horizontal
Time: Civil twilight to civil twilight (±30 min of sunrise/sunset)

ALCOHOL LIMITS (§107.27):
Time: 8 hours minimum from last drink
BAC: < 0.04%

REPORTING ACCIDENTS (§107.9):
Within: 10 days of occurrence
Trigger: Serious injury OR property damage > $500 (excluding drone itself)

CERTIFICATE RECENCY (§107.65):
Knowledge test: every 24 calendar months (or online recurrent training)

TOWER LIGHT GUN SIGNALS — QUICK TABLE:
Ground | Steady Green = Takeoff | Flashing Green = Taxi | Steady Red = Stop
       | Flashing Red = Clear runway | Flashing White = Return to start
Flight | Steady Green = Land | Flashing Green = Return & land | Steady Red = Give way
       | Flashing Red = Unsafe do not land | Alt R/G = Extreme caution (all)

UNIT CONVERSIONS:
1 knot = 1.151 mph = 1.852 kph
1 statute mile = 5,280 ft = 1.609 km
1 nautical mile = 6,076 ft = 1.852 km
°C to °F: multiply by 9/5, add 32
°F to °C: subtract 32, multiply by 5/9

ALTITUDE MSL TO AGL:
AGL = MSL altitude - Field elevation MSL
Example: Flying at 1,500 ft MSL over field at 800 ft MSL = 700 ft AGL

CLOUD COVERAGE OKTAS:
SKC/CLR=0  FEW=1-2  SCT=3-4  BKN=5-7  OVC=8  (out of 8 total)
Ceiling = lowest BKN or OVC layer
"""),

("REF","Reference","FAA Sample Questions — Part 107","sample questions,practice test,exam","""
FAA PART 107 SAMPLE QUESTIONS AND ANSWERS

Q1: Under what condition may a remote pilot operate a small UAS from a
moving vehicle?
A: Only over a sparsely populated area. (§107.25)

Q2: What is the maximum groundspeed for a small UAS under Part 107?
A: 100 mph (87 knots). (§107.51)

Q3: When operating a small UAS, the remote pilot must yield the right of
way to which aircraft?
A: All other aircraft, including manned aircraft and other UAS. (§107.37)

Q4: A remote pilot may operate over a moving vehicle when the operation
is conducted over which type of area?
A: A sparsely populated area. (§107.25)

Q5: Which weather phenomenon is always associated with a thunderstorm?
A: Lightning.

Q6: What minimum visibility is required for Part 107 UAS operations?
A: 3 statute miles from the control station. (§107.51(c))

Q7: What cloud clearance is required under Part 107?
A: 500 feet below and 2,000 feet horizontal from clouds. (§107.51(d))

Q8: What is the maximum altitude for UAS operations under Part 107?
A: 400 feet AGL, or 400 feet above the tallest structure within 400 ft.

Q9: When must a remote pilot report an accident to the FAA?
A: Within 10 days if there is serious injury or property damage > $500.

Q10: What is the minimum age to obtain a remote pilot certificate?
A: 16 years of age. (§107.61)

Q11: The haze layer associated with a temperature inversion can:
A: Cause the horizon to become obscured.

Q12: What causes a microburst?
A: A strong downdraft associated with a thunderstorm cell.

Q13: AIRMET SIERRA is issued for:
A: IFR conditions and mountain obscuration.

Q14: What is the hazardous attitude antidote for "invulnerability"?
A: "It could happen to me."

Q15: Class G airspace exists:
A: From the surface to the base of overlying controlled airspace.
"""),

("REF","Reference","Common Exam Traps and Trick Questions","exam,traps,common mistakes,study tips","""
COMMON PART 107 EXAM TRAPS — FREQUENTLY MISSED QUESTIONS

TRAP 1: Statute miles vs. nautical miles.
The 3-mile visibility minimum is STATUTE miles (not nautical miles).
Chart distances and airspace are in NAUTICAL miles.
Airport runway lengths are in FEET.
Always check which unit the question uses.

TRAP 2: MSL vs. AGL altitude.
FAA altitude limit (400 ft) is AGL (above ground level).
Charts show altitudes in MSL (above mean sea level).
METAR cloud heights are AGL. TAF cloud heights are AGL.
Pressure altitude is MSL. Density altitude is MSL.

TRAP 3: Civil twilight ≠ sunrise/sunset.
Civil twilight = 30 minutes BEFORE sunrise and 30 minutes AFTER sunset.
You can fly during civil twilight with the anti-collision light.
Not sunrise to sunset — civil twilight extends this window.

TRAP 4: Clock faces for wind direction.
"Wind from 270°" = wind FROM the west (blowing TOWARD the east).
Aircraft heading INTO the wind to land/takeoff.

TRAP 5: Class D hours.
Class D exists only when control tower is OPERATIONAL.
When tower is closed, verify whether Class E or G applies.
Check ATIS or call ATC — do not assume hours.

TRAP 6: BKN vs. OVC as "ceiling."
A "ceiling" is the lowest BKN or OVC layer.
FEW and SCT are NOT ceilings (too sparse).
METAR BKN025 = ceiling at 2,500 ft AGL.

TRAP 7: TFR + LAANC.
LAANC authorization does NOT allow flight in a TFR.
TFRs override LAANC. Always check TFRs separately.

TRAP 8: 0.04% BAC vs. 0.08% BAC.
FAA aviation limit is 0.04% (half the DUI limit for driving).
8-hour bottle-to-throttle rule applies independently of BAC.

TRAP 9: Property damage threshold.
Must report if damage to property OTHER THAN THE DRONE exceeds $500.
Damage to the drone itself does not trigger the reporting requirement.

TRAP 10: Maximum weight for Part 107.
55 lbs is the TOTAL takeoff weight (aircraft + payload + batteries).
Weight at the moment of takeoff — not just the airframe.
"""),

]


# ══════════════════════════════════════════════════════════════════════════════
#  SAMPLE QUESTIONS DATABASE
# ══════════════════════════════════════════════════════════════════════════════

SAMPLE_QUESTIONS = [
    ("II","What airspace class requires a two-way radio communication AND an explicit ATC clearance before entry?",
     ["A) Class D","B) Class C","C) Class B","D) Class E"],
     "C) Class B",
     "Class B requires both establishing two-way communication AND receiving an explicit ATC clearance before entering. Class C only requires communication establishment. Class D requires communication."),

    ("III","A METAR reads 'BKN018.' What does this indicate?",
     ["A) Broken clouds at 18,000 ft","B) Broken clouds at 1,800 ft AGL","C) Scattered clouds at 1,800 ft","D) Broken clouds at 180 ft"],
     "B) Broken clouds at 1,800 ft AGL",
     "METAR cloud heights are always in hundreds of feet AGL. BKN018 = broken layer at 1,800 ft AGL. This would be the ceiling since BKN is 5-7 oktas coverage."),

    ("I","Under Part 107, what is the maximum altitude for sUAS operations?",
     ["A) 400 ft MSL","B) 400 ft AGL","C) 500 ft AGL","D) 1,000 ft AGL"],
     "B) 400 ft AGL",
     "§107.51 limits operations to 400 ft above ground level (AGL). Exception: within 400 ft of a structure, may fly up to 400 ft above the structure's highest point."),

    ("VI","What is the antidote for the hazardous attitude 'Anti-Authority'?",
     ["A) 'Not so fast, think first.'","B) 'It could happen to me.'","C) 'Follow the rules, they are usually right.'","D) 'Taking chances is foolish.'"],
     "C) 'Follow the rules, they are usually right.'",
     "Anti-Authority is 'Don't tell me what to do.' Antidote: 'Follow the rules. They are usually right.' Impulsivity antidote is 'Not so fast.' Invulnerability antidote is 'It could happen to me.' Macho antidote is 'Taking chances is foolish.'"),

    ("III","AIRMET TANGO is issued for:",
     ["A) IFR conditions and mountain obscuration","B) Moderate turbulence and strong surface winds","C) Moderate icing","D) Volcanic ash"],
     "B) Moderate turbulence and strong surface winds",
     "AIRMET Sierra = IFR/mountain obscuration. AIRMET Tango = Turbulence/strong winds/wind shear. AIRMET Zulu = icing. SIGMETs cover volcanic ash."),

    ("I","How soon after an accident must a remote pilot report to the FAA?",
     ["A) Immediately","B) Within 24 hours","C) Within 10 days","D) Within 30 days"],
     "C) Within 10 days",
     "§107.9 requires reporting to the FAA within 10 days if the accident resulted in serious injury to any person or property damage exceeding $500 (not counting the drone itself)."),

    ("III","What is density altitude?",
     ["A) The altitude shown on the altimeter","B) Pressure altitude corrected for non-standard temperature","C) The altitude above mean sea level","D) The altitude above ground level"],
     "B) Pressure altitude corrected for non-standard temperature",
     "Density altitude represents actual aircraft performance altitude. High DA means thinner air, reduced aircraft performance. Formula: DA = PA + 120 × (OAT - ISA temp at that PA)."),

    ("II","An aircraft in which airspace has the right of way over a drone?",
     ["A) Only manned aircraft in Class B","B) All aircraft, including other drones","C) Only commercial aircraft","D) No aircraft — UAS have equal right of way"],
     "B) All aircraft, including other drones",
     "§107.37 states that each small unmanned aircraft must yield the right of way to ALL other aircraft, including manned aircraft AND other UAS."),
]


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def build_kb():
    import json as _json
    conn = get_db()
    init_db(conn)
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM sample_questions")
    conn.execute("DELETE FROM kb_meta")
    conn.commit()

    total = 0
    domain_counts = {}

    print(f"\n  Building Part 107 Knowledge Base")
    print(f"  {'─'*45}")

    for domain, source, section, topic, content in KNOWLEDGE:
        n = insert_chunk(conn, domain, source, section, topic, content)
        total += n
        domain_counts[domain] = domain_counts.get(domain, 0) + n

    conn.commit()

    for domain, question, options, answer, explanation in SAMPLE_QUESTIONS:
        insert_question(conn, domain, question, options, answer, explanation)
    conn.commit()

    # Rebuild FTS
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    conn.commit()

    conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('built_at', ?)",
                 (datetime.now().isoformat(),))
    conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('total_chunks', ?)",
                 (str(total),))
    conn.execute("INSERT OR REPLACE INTO kb_meta VALUES ('version', '2.0-part107-full')",)
    conn.commit()
    conn.close()

    print(f"\n  Domain breakdown:")
    domain_names = {
        "I":"Applicable Regulations","II":"Airspace","III":"Aviation Weather",
        "IV":"Loading & Performance","V":"Emergency Procedures","VI":"CRM",
        "VII":"Radio Communications","VIII":"UAS Performance","IX":"Physiology",
        "X":"Aeronautical Decision Making","XI":"Airport Operations",
        "XII":"Maintenance","REF":"Reference / Sample Questions"
    }
    for dom in sorted(domain_counts):
        print(f"    Domain {dom}: {domain_counts[dom]:3d} chunks  {domain_names.get(dom,'')}")
    print(f"\n  TOTAL: {total} chunks")
    print(f"  Sample questions: {len(SAMPLE_QUESTIONS)}")
    print(f"  DB: {KB_PATH}  ({KB_PATH.stat().st_size//1024} KB)\n")
    return total


# ══════════════════════════════════════════════════════════════════════════════
#  QUERY
# ══════════════════════════════════════════════════════════════════════════════

def query_kb(q, limit=5, domain=None):
    import re as _re
    conn = get_db()
    # Clean query for FTS5
    q2 = _re.sub(r'[-/]',' ',q.lower())
    words = [w.strip('.,!?;:"\'()[]') for w in q2.split() if len(w.strip('.,!?;"\'()[]'))>2]
    stop  = {'the','and','for','that','this','with','from','are','was','have','can','not','but','its'}
    words = [w for w in words if w not in stop]
    fts   = ' OR '.join(words)
    if len(words)>1:
        fts = f'"{" ".join(words)}" OR {fts}'
    try:
        if domain:
            rows = conn.execute(
                "SELECT c.domain,c.source,c.section,c.topic,c.content,bm25(chunks_fts) s "
                "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? AND c.domain=? ORDER BY s LIMIT ?",
                (fts, domain, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT c.domain,c.source,c.section,c.topic,c.content,bm25(chunks_fts) s "
                "FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? ORDER BY s LIMIT ?",
                (fts, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        return [{"error": str(e)}]
    finally:
        conn.close()


def random_quiz(n=3):
    conn = get_db()
    rows = conn.execute("SELECT * FROM sample_questions ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    import json as _json
    parser = argparse.ArgumentParser(description="Part 107 Knowledge Base Builder")
    parser.add_argument("--build",  action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--query",  type=str)
    parser.add_argument("--domain", type=str, help="Filter by domain (I, II, III...)")
    parser.add_argument("--quiz",   action="store_true", help="Random sample questions")
    parser.add_argument("--limit",  type=int, default=5)
    args = parser.parse_args()

    if args.build or not KB_PATH.exists():
        build_kb()

    if args.status:
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        qcount= conn.execute("SELECT COUNT(*) FROM sample_questions").fetchone()[0]
        meta  = dict(conn.execute("SELECT key,value FROM kb_meta").fetchall())
        conn.close()
        print(f"\n  Part 107 KB: {total} chunks, {qcount} sample questions")
        print(f"  Built: {meta.get('built_at','unknown')}")
        print(f"  Version: {meta.get('version','unknown')}\n")

    if args.query:
        results = query_kb(args.query, limit=args.limit, domain=args.domain)
        print(f"\n  Query: '{args.query}'  ({len(results)} results)\n")
        for r in results:
            print(f"  [{r.get('domain','')}] {r.get('section','')[:60]}")
            preview = r.get('content','')[:200].replace('\n',' ')
            print(f"  {preview}...\n")

    if args.quiz:
        questions = random_quiz(3)
        for i, q in enumerate(questions, 1):
            opts = _json.loads(q['options']) if isinstance(q['options'],str) else q['options']
            print(f"\n  Q{i}: {q['question']}")
            for opt in opts:
                print(f"    {opt}")
            input("  Press Enter for answer...")
            print(f"  Answer: {q['answer']}")
            print(f"  Explanation: {q['explanation']}")


if __name__ == "__main__":
    main()

# Safety1271 — Another Set of Eyes

**Tagline:** AI-assisted aerial awareness. Human-led safety.

## Project description (250 words)

Safety1271 was inspired by my experience serving on the safety and security team at my church in Northern Virginia. Our challenge is simple: while we monitor one area, what might we miss elsewhere?

Large church campuses cannot be watched everywhere at once. Fixed cameras provide coverage, but parked vehicles, building edges, and viewing angles can leave areas unseen. Safety1271 complements those cameras by combining drone video with AI, giving volunteers a broader aerial perspective and helping them notice situations that deserve attention.

Operators can ask the AI to look for specific things and describe visible activity. In one recorded demonstration, it reports a person wearing a red top walking toward a red car and getting inside, without guessing identity or motive. Other scenarios include cars with open doors, unattended backpacks, and available parking spaces for people arriving after services begin.

The live camera view demonstrates how an operator could inspect the grounds from different viewpoints. Preprogrammed waypoint patrols are a future capability, not currently enabled in the demonstrated solution.

Human judgment remains central. Observations are leads for volunteers to review, not proof of danger or wrongdoing. Search prompts do not launch, steer, or land the aircraft.

Weather and aviation requirements apply. Drones have temperature, wind, and precipitation limits. Automated preflight checks provide weather and airspace information to support decisions about proceeding or holding operations.

Safety1271 augments church safety teams rather than replacing them, helping volunteers see more, assess information, and make informed decisions with people at the center of safety.

## Detailed technology stack

Dependency versions below are declared minimums, not a lockfile or a guarantee of the currently installed version.

### Hardware and network

| Component | Role |
|---|---|
| DJI Mini 4 Pro | Aircraft camera and telemetry source |
| DJI RC-N2 | Screenless controller linking the aircraft to the Android bridge phone |
| Android phone | Runs the DJI Control Server app; connected to the remote by cable |
| Windows laptop | Hosts the Python services, local video relay, and browser dashboard |
| Phone hotspot / local Wi-Fi | Connects the laptop and bridge phone |

### AI and model access

| Technology | Role |
|---|---|
| Gloo AI Studio | Model gateway for the agent workflows |
| OpenAI Python SDK (`openai>=1.30.0`) | OpenAI-compatible calls to Gloo's Responses and Chat Completions APIs |
| OAuth2 client credentials | Gloo authentication with token refresh |
| Claude Sonnet 4.6 (`gloo-anthropic-claude-sonnet-4.6`) | Patrol narration, consistency judging, preflight, and report model alias |
| Gemini 2.5 Flash (`gloo-google-gemini-2.5-flash`) | ATC speech-to-text and experimental routing alias |
| Gemini 2.5 Flash Lite (`gloo-google-gemini-2.5-flash-lite`) | ATC filtering and knowledge-base query alias |
| `requests>=2.31.0` | Token exchange and external HTTP APIs |
| `python-dotenv>=1.0.0` | Loads locally supplied environment configuration |

### Vision and video

| Technology | Role |
|---|---|
| OpenCV (`opencv-python>=4.9.0`) | RTSP capture, image processing, stabilization, and tracking |
| NumPy | Image arrays, color masks, and motion calculations |
| Pillow (`pillow>=10.0.0`) | Image preparation and encoding |
| H.264 / Annex B / AVCC | Aircraft video representation and publisher conversion |
| RTMP | Android-to-laptop video publishing |
| MediaMTX | Local streaming server and RTMP-to-RTSP relay |
| RTSP | Video input to the Python dashboard |
| MJPEG over HTTP | Browser camera preview from the FastAPI relay |
| `focus.py` | Color-region tracking and ground-fixed motion measurements |
| `stream_analyst.py` | Frame preparation, stabilization, and scene analysis helpers |
| `clip_story.py` | Clip narration, checks, retries, withholding, and optional prestaged cache |
| `imageio-ffmpeg>=0.6.0` | FFmpeg access for synthetic replay and audio/video helper paths |

### Dashboard and services

| Technology | Role |
|---|---|
| Python 3.12 | Laptop application runtime used for the demo |
| FastAPI (`fastapi>=0.110.0`) | Command Center, video relay, telemetry, and parking API |
| Uvicorn (`uvicorn>=0.29.0`) | ASGI server |
| HTML, CSS, vanilla JavaScript | Browser dashboard and separate read-only parking view |
| HTTP/JSON | Telemetry and workflow endpoints |
| PowerShell | Local stack startup and aircraft-to-dashboard link checks |
| JSON files | Demo manifest, narration truth, configuration templates, and local state |

### Android bridge

| Technology | Role |
|---|---|
| Kotlin / Java | DJI Control Server application |
| DJI Mobile SDK V5.18.0 | Aircraft video and telemetry access |
| Android SDK | `compileSdk` / `targetSdk` 35; `minSdk` 24 in the included build |
| Gradle wrapper | Android build tooling |
| AndroidX / Material Components | Android application UI and lifecycle |
| Kotlin coroutines | Background work |
| Ktor 1.6.4 / Netty | Embedded telemetry/diagnostic HTTP server |
| Custom Kotlin RTMP publisher | Packages and publishes the aircraft's H.264 stream |

### Weather, location, airspace, and audio

| Technology / service | Role |
|---|---|
| aviationweather.gov METAR API | Nearby-airport weather observations |
| NOAA / National Weather Service APIs | Weather forecasts and alerts |
| FAA airspace/TFR sources | Airspace advisory and preflight information |
| OurAirports CSV data | Airport location and published frequency lookup |
| LiveATC audio | Optional live radio input; use subject to provider terms |
| Gemini via Gloo | Default ATC transcription path |
| OpenAI Whisper | Optional local transcription fallback |
| SciPy, SoundDevice, PyRTLSDR | Optional local audio/SDR paths |
| Astral (`astral>=3.2`) | Supporting civil-twilight calculations |

### Evaluation and supporting modules

| Technology | Role / status |
|---|---|
| Python `unittest` | Offline regression tests |
| `eval_narration.py` / `narration_truth.json` | Three-run narration evaluation against required and forbidden facts |
| Gloo model-assisted judges | Narration consistency and evaluation; not deterministic correctness guarantees |
| Arthur observability SDK | Optional tracing/evaluation integration when configured |
| SQLite / FTS5 | Supporting FAA knowledge-base and flight-log modules |
| ReportLab / pdfplumber | Supporting report generation and FAA document ingestion |
| Twilio / SendGrid | Supporting SMS/email integrations; not evidence that delivery is enabled in the demo |
| APScheduler / Flask | Supporting patrol scheduling and webhook modules |
| DJI WPML/KMZ / mission manager | Experimental future waypoint and replanning integration; not enabled in the current demo |

The active demonstration centers on operator-directed video analysis and human review. Inclusion of a supporting module or dependency does not mean it is active, configured, or flight-validated.

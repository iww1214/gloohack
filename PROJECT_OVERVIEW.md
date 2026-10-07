# Safety1271 — Another Set of Eyes

**Tagline:** AI-assisted aerial awareness. Human-led safety.

## Project description (250 words)

Safety1271 gives church safety volunteers another set of eyes: an aerial perspective paired with AI that describes what is visible, while people remain responsible for decisions.

Built from the experience of serving on a church safety and security team in Northern Virginia, the project addresses a challenge: no volunteer can watch an entire campus at once. Fixed cameras provide coverage, but parked vehicles, building edges, and installed camera angles can leave areas outside their view.

The Command Center lets an operator request observations in plain language, such as finding a person in a red top, a car with open doors, or an unattended backpack. Recorded demonstration footage is labeled as simulated. A live camera view demonstrates the aircraft video connection, while a parking workflow estimates available spaces for the parking crew.

Video analysis uses Gloo AI Studio alongside computer vision techniques that compensate for camera movement and measure motion. Narrations are checked against tracked facts; contradictory accounts are retried or withheld rather than presented as reliable observations. Descriptions avoid identity, age, vehicle make, and assumptions about motive.

Supporting workflows provide weather and airspace information and air traffic control transcripts. Aircraft operating limits still apply. Search prompts never launch, steer, or land the drone. Preprogrammed waypoint patrols are a future integration, not an enabled feature of the current demo.

Safety1271 augments cameras and volunteer patrols instead of replacing them. Its purpose is situational awareness: helping people notice what deserves attention, review the evidence, and respond with human judgment at the center.

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

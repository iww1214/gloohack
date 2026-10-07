# Safety1271 — Another Set of Eyes

AI-assisted aerial awareness for church safety teams. Safety1271 combines drone video, operator-directed observations, and human review. It complements fixed cameras; it does not identify people, establish threats, or replace the remote pilot.

## Submission documents

- [Agent build document](AGENT_BUILD_DOC.md): architecture, prompts, evaluation, guardrails, and current versus planned capabilities.
- [Video pitch](DEMO_PITCH.md): recorded person-in-red-top example followed by the live camera view.
- [Project description and technology stack](PROJECT_OVERVIEW.md): submission summary and detailed component list.

## Current demonstration

The Command Center supports a live drone camera view and recorded scenarios: a person in a red top, a car with open doors, an unattended backpack, and parking-space estimates. Recorded stock footage is simulated and must be labeled accordingly. An AI search directive changes the observation target; it never launches, moves, or lands the aircraft.

Pre-programmed waypoint patrols and autonomous weather/TFR-aware replanning are future integrations, not enabled or demonstrated by the current solution. Experimental supporting flight-control modules are included for inspection; their presence is not evidence of validated autonomous operation. Do not run the full patrol launcher as a no-flight demo.

## Local setup

Requires Python 3.12, a Windows laptop for the supplied PowerShell scripts, MediaMTX, and locally configured Gloo AI Studio credentials. For live aircraft video, use the DJI Mini 4 Pro, RC-N2 remote, and Android DJI Control Server app.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item network.example.json network.json
Copy-Item mediamtx.live.example.yml mediamtx.live.yml
```

Edit the copied files locally. Set the phone/laptop addresses to your actual hotspot addresses, configure Gloo credentials in `.env`, and choose a unique streaming password. Install MediaMTX separately under `mediamtx_bin\mediamtx.exe`. Configure the matching laptop address and streaming credentials in the Android app's `strings.xml`; configure your DJI developer key in its `AndroidManifest.xml`. The committed Android values are placeholders, not working credentials. Build the Android app with its Gradle wrapper and a locally installed Android SDK; do not commit SDK paths or configured credentials.

The Gradle build uses the normal local build directory. If cloud-sync locks affect your checkout, move the checkout outside the synced folder.

Start the laptop stack **before** the phone publishes:

```powershell
.\start_demo.ps1
```

Turn on the remote, then the aircraft on a flat surface with propellers clear, then open the DJI Control Server app. The phone connects to the screenless RC-N2 by cable; DJI Fly is not used on the bridge phone. Do not restart the laptop stack while the phone is publishing.

Open `http://127.0.0.1:8092/`. To inspect the link without issuing flight commands:

```powershell
.\check_link.ps1
```

An indoor room demonstration is a camera/link demonstration, not a flight test. Do not launch the aircraft in the room. Any outdoor operation requires suitable conditions, applicable permissions, and a responsible remote pilot.

## Recorded clips and evaluation

Licensed media, generated narration caches, airport datasets, operational databases, incident reports, runtime configuration, and binaries are deliberately excluded. Supply footage you have permission to use under `demo_clips`, matching the filenames in [demo_manifest.json](demo_manifest.json). Do not assume the recorded scenarios will play in a fresh checkout without those files.

For clip narrations, [narration_truth.json](narration_truth.json) records required facts and forbidden claims:

```powershell
python eval_narration.py --runs 3
```

All three runs must pass before claiming a clip's narration is verified. This requires the clip files and configured model access. No new narration-evaluation result is claimed by this publication.

Targeted offline regression checks:

```powershell
python -m unittest test_twilio_client test_gloo_judges test_video_eval
```

## Publication hygiene

Only an explicit source/documentation allowlist was exported. Private keys, `.env`, actual streaming configuration, local SDK settings, device logs, reports, third-party vendored services, and footage were not uploaded. Android credentials were replaced with placeholders in this public copy; the working local app configuration was not changed. The exported tree was scanned locally with Gitleaks before publication.

Never commit a configured Android API key or stream password. `.gitignore` helps exclude private files, but does not sanitize secrets embedded in tracked source. Review the staged diff and scan every future commit before pushing.

"""
Security Threat Detector for Church/School Compound Patrol
===========================================================

Monitors perimeter sensors, detects anomalous events (unauthorized access,
intrusions, personnel emergencies), and triggers autonomous drone deployment
to investigate and secure the facility.

Use Cases:
- Unauthorized door openings after hours
- Vehicle approach to restricted zones
- Smoke/fire detection → immediate inspection
- Lost person in grounds → aerial search
- Facility damage assessment after weather events
- Medical emergency location (outdoor campus)
- Perimeter intrusion detection

Dependencies:
    pip install requests
"""

import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from gloo_client import GlooAnthropicCompat, complete, MODELS, GLOO_TRADITION

# ── Clients ──────────────────────────────────────────────────────────────────
anthropic_client = GlooAnthropicCompat()


# ── Simulated Sensor Data ────────────────────────────────────────────────────

class SecuritySensor:
    """Simulates perimeter security sensors."""
    
    def __init__(self):
        self.events = []
        self.active_threats = []
    
    def trigger_door_open(self, location: str, time_since_hours: float = 0) -> Dict:
        """Simulate unauthorized door opening (after hours or unexpected)."""
        return {
            "event_id": "DOOR_001",
            "type": "perimeter_access",
            "location": location,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "time_since_hours": time_since_hours,  # hours since facility closed
            "severity": "high" if time_since_hours > 2 else "medium",
            "description": f"Unauthorized door opening at {location}",
        }
    
    def trigger_vehicle_approach(self, zone: str, distance_m: float) -> Dict:
        """Simulate unauthorized vehicle approach to restricted zone."""
        severity = "critical" if distance_m < 50 else "high" if distance_m < 100 else "medium"
        return {
            "event_id": "VEHC_001",
            "type": "perimeter_intrusion",
            "zone": zone,
            "distance_m": distance_m,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": severity,
            "description": f"Vehicle detected {distance_m}m from {zone}",
        }
    
    def trigger_smoke_detection(self, building: str, location: str) -> Dict:
        """Simulate smoke detector alert (facility emergency)."""
        return {
            "event_id": "SMOK_001",
            "type": "fire_emergency",
            "building": building,
            "location": location,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "critical",
            "description": f"Smoke detected in {building} at {location}",
        }
    
    def trigger_lost_person(self, name: str, last_seen: str, age_group: str) -> Dict:
        """Simulate lost person alert (campus emergency)."""
        return {
            "event_id": "LOST_001",
            "type": "personnel_emergency",
            "person": name,
            "age_group": age_group,
            "last_seen": last_seen,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "critical",
            "description": f"Lost person: {name} ({age_group}), last seen at {last_seen}",
        }
    
    def trigger_weather_damage(self, location: str, damage_type: str) -> Dict:
        """Simulate post-weather facility damage assessment request."""
        return {
            "event_id": "WTHR_001",
            "type": "facility_assessment",
            "location": location,
            "damage_type": damage_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": "high",
            "description": f"Post-weather damage assessment needed at {location}: {damage_type}",
        }


# ── Threat Analysis ──────────────────────────────────────────────────────────

THREAT_TOOLS = [
    {
        "name": "analyze_threat_severity",
        "description": (
            "Analyze security event and classify threat level. "
            "Returns severity (critical/high/medium/low), recommended response, "
            "and whether drone deployment is warranted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "Event description"},
                "location": {"type": "string", "description": "Facility location (building/zone)"},
                "time_context": {"type": "string", "description": "Time context (after-hours, weekend, etc.)"},
                "immediate_risk": {"type": "boolean", "description": "Is there immediate physical threat?"}
            },
            "required": ["event", "location", "time_context"]
        }
    },
    {
        "name": "generate_drone_patrol_mission",
        "description": (
            "Generate autonomous drone patrol mission to assess security threat. "
            "Returns target waypoints, inspection duration, camera focus areas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "threat_type": {"type": "string", "description": "Type of threat (intrusion, fire, lost person, etc.)"},
                "target_location": {"type": "string", "description": "GPS coords or facility zone"},
                "altitude_ft": {"type": "number", "description": "Flight altitude for inspection"},
                "inspection_duration_min": {"type": "number", "description": "Expected inspection time (minutes)"}
            },
            "required": ["threat_type", "target_location"]
        }
    }
]


def analyze_threat_severity(
    event: str,
    location: str,
    time_context: str,
    immediate_risk: bool = False,
) -> Dict:
    """
    Use Gloo AI to analyze threat severity and recommend response.
    """
    prompt = f"""You are a security expert for a church compound/Christian school.

SECURITY EVENT:
{event}

FACILITY LOCATION: {location}
TIME CONTEXT: {time_context}
IMMEDIATE PHYSICAL RISK: {immediate_risk}

Analyze this threat and provide:
1. Severity level (CRITICAL/HIGH/MEDIUM/LOW)
2. Threat classification (intrusion, emergency, false alarm, etc.)
3. Recommended response (dispatch security, call police, drone patrol, etc.)
4. Whether drone deployment is appropriate
5. Priority level for response (1-10, 10 = immediate)

Respond with valid JSON only:
{{
    "severity": "...",
    "classification": "...",
    "response": "...",
    "drone_deployment_recommended": true/false,
    "priority": <1-10>,
    "rationale": "..."
}}"""

    try:
        response = complete(
            system_prompt=(
                "You are a security analyst for religious/educational facilities. "
                "Prioritize safety of persons, especially children. Respond with valid JSON only."
            ),
            user_content=prompt,
            model="atc_filter",
            use_tradition=False,
            max_tokens=512,
            cache=None,
        )
        
        result = json.loads(response.strip())
        return result
    except (json.JSONDecodeError, Exception) as e:
        # Fallback heuristic
        return {
            "severity": "HIGH" if immediate_risk else "MEDIUM",
            "classification": "security_event",
            "response": "Investigate immediately" if immediate_risk else "Monitor and assess",
            "drone_deployment_recommended": immediate_risk,
            "priority": 9 if immediate_risk else 5,
            "rationale": f"Automated analysis failed: {str(e)}. Defaulting to precautionary response.",
        }


def generate_drone_patrol_mission(
    threat_type: str,
    target_location: str,
    altitude_ft: float = 150,
    inspection_duration_min: float = 10,
) -> Dict:
    """
    Generate autonomous drone patrol mission using Gloo AI.
    Returns waypoints and inspection parameters.
    """
    prompt = f"""You are a drone mission planner for security/facility operations.

THREAT TYPE: {threat_type}
TARGET LOCATION: {target_location}
ALTITUDE: {altitude_ft} ft
MAX DURATION: {inspection_duration_min} minutes

Design a drone patrol mission that:
1. Covers the threat area efficiently
2. Captures video/thermal imaging of concern areas
3. Maintains safe altitude and distance from people
4. Completes within time/battery constraints
5. Provides situational awareness for security team

Provide:
- Patrol pattern (grid, perimeter, spiral, etc.)
- Number and sequence of waypoints
- Camera settings (zoom, thermal, night vision if needed)
- Return-to-home criteria
- Expected flight time

Respond with valid JSON only:
{{
    "mission_name": "...",
    "pattern": "...",
    "waypoints": [
        {{"lat": <float>, "lon": <float>, "alt_ft": <float>, "duration_sec": <int>}},
        ...
    ],
    "camera_settings": {{"mode": "...", "thermal": true/false, ...}},
    "expected_flight_time_min": <float>,
    "rth_trigger": "...",
    "security_notes": "..."
}}"""

    try:
        response = complete(
            system_prompt="You are a drone mission planner. Respond with valid JSON only.",
            user_content=prompt,
            model="atc_filter",
            use_tradition=False,
            max_tokens=512,
            cache=None,
        )
        
        result = json.loads(response.strip())
        return result
    except (json.JSONDecodeError, Exception) as e:
        # Fallback mission
        return {
            "mission_name": f"Security Patrol - {threat_type}",
            "pattern": "perimeter" if "intrusion" in threat_type else "grid",
            "waypoints": [
                {"lat": 38.765, "lon": -77.181, "alt_ft": altitude_ft, "duration_sec": 30},
                {"lat": 38.766, "lon": -77.182, "alt_ft": altitude_ft, "duration_sec": 30},
            ],
            "camera_settings": {"mode": "video", "thermal": "fire" in threat_type},
            "expected_flight_time_min": min(inspection_duration_min, 12),
            "rth_trigger": "battery < 25% or timeout",
            "security_notes": f"Automated mission generation failed: {str(e)}. Basic patrol pattern used.",
        }


def process_security_event(event: Dict) -> Dict:
    """
    Full security event processing pipeline:
    1. Analyze threat severity
    2. Decide if drone deployment needed
    3. Generate patrol mission if authorized
    4. Return recommendation for preflight gate
    """
    
    threat_analysis = analyze_threat_severity(
        event=event.get("description", "Unknown event"),
        location=event.get("location", event.get("zone", event.get("building", "Unknown"))),
        time_context=event.get("time_since_hours", 0) > 2 and "after-hours" or "during-hours",
        immediate_risk=event.get("severity") == "critical",
    )
    
    result = {
        "event_id": event.get("event_id"),
        "event_type": event.get("type"),
        "threat_analysis": threat_analysis,
        "drone_mission": None,
        "preflight_recommendation": "PROCEED" if not threat_analysis.get("drone_deployment_recommended") else "HOLD_FOR_INVESTIGATION",
    }
    
    # If drone deployment recommended, generate mission
    if threat_analysis.get("drone_deployment_recommended"):
        mission = generate_drone_patrol_mission(
            threat_type=threat_analysis.get("classification"),
            target_location=event.get("location", event.get("zone", "Facility")),
            altitude_ft=150,
            inspection_duration_min=10,
        )
        result["drone_mission"] = mission
        result["preflight_recommendation"] = "HOLD_FOR_INVESTIGATION"  # Pause normal ops, deploy patrol drone
    
    return result


def main():
    """Demonstrate security threat detector."""
    sensor = SecuritySensor()
    
    print("=" * 80)
    print("SECURITY THREAT DETECTOR - Church/School Compound")
    print("=" * 80)
    
    # Test Case 1: Unauthorized Door Opening After Hours
    print("\n[TEST 1] Unauthorized Door Opening After Hours")
    print("-" * 80)
    event1 = sensor.trigger_door_open("East Building - Main Entrance", time_since_hours=3.5)
    result1 = process_security_event(event1)
    print(json.dumps(result1, indent=2))
    
    # Test Case 2: Vehicle Approach to Restricted Zone
    print("\n[TEST 2] Vehicle Approach - Restricted Zone")
    print("-" * 80)
    event2 = sensor.trigger_vehicle_approach("Children's Playground (100m restricted)", distance_m=35)
    result2 = process_security_event(event2)
    print(json.dumps(result2, indent=2))
    
    # Test Case 3: Smoke Detection - Facility Emergency
    print("\n[TEST 3] Smoke Detection - Fire Emergency")
    print("-" * 80)
    event3 = sensor.trigger_smoke_detection("Fellowship Hall", "Kitchen area")
    result3 = process_security_event(event3)
    print(json.dumps(result3, indent=2))
    
    # Test Case 4: Lost Person - Campus Search
    print("\n[TEST 4] Lost Person - Campus Search Request")
    print("-" * 80)
    event4 = sensor.trigger_lost_person("Student (age 12)", "Playground", "child")
    result4 = process_security_event(event4)
    print(json.dumps(result4, indent=2))
    
    # Test Case 5: Post-Weather Damage Assessment
    print("\n[TEST 5] Post-Weather Damage Assessment")
    print("-" * 80)
    event5 = sensor.trigger_weather_damage("Roof - Fellowship Hall", "Possible hail damage, needs inspection")
    result5 = process_security_event(event5)
    print(json.dumps(result5, indent=2))
    
    print("\n" + "=" * 80)
    print("SECURITY THREAT DETECTOR - Demonstration Complete")
    print("=" * 80)


if __name__ == "__main__":
    main()

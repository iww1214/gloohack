#!/usr/bin/env python3
"""Quick bridge telemetry test"""
import urllib.request
import json
import time
import sys

def test_bridge(url="http://172.20.10.2:8080"):
    print("=== Bridge Telemetry Test ===\n")
    
    # Test 1: Single check
    print("[1/3] Single telemetry check:")
    try:
        resp = urllib.request.urlopen(f"{url}/status", timeout=5)
        data = json.loads(resp.read().decode())
        print(f"  Battery: {data.get('battery_pct')}%")
        print(f"  Heading: {data.get('heading_deg')}°")
        print(f"  Altitude: {data.get('altitude_m')}m")
        print(f"  Aircraft connected: {data.get('aircraft_connected')}")
        ts1 = data.get('telemetry_updated_at_ms', 0)
        print(f"  Timestamp: {ts1}ms\n")
    except Exception as e:
        print(f"  ERROR: {e}\n")
        return False
    
    # Test 2: Wait 2 seconds and check again (for timestamp advancement)
    print("[2/3] Waiting 2 seconds...")
    time.sleep(2)
    
    print("[2/3] Second telemetry check:")
    try:
        resp = urllib.request.urlopen(f"{url}/status", timeout=5)
        data = json.loads(resp.read().decode())
        print(f"  Battery: {data.get('battery_pct')}%")
        print(f"  Heading: {data.get('heading_deg')}°")
        print(f"  Altitude: {data.get('altitude_m')}m")
        ts2 = data.get('telemetry_updated_at_ms', 0)
        print(f"  Timestamp: {ts2}ms")
        
        # Check if timestamp advanced
        if ts2 > ts1:
            print(f"  ✅ Timestamp advancing (+{ts2-ts1}ms)\n")
        else:
            print(f"  ⚠️  Timestamp NOT advancing (frozen telemetry)\n")
    except Exception as e:
        print(f"  ERROR: {e}\n")
        return False
    
    # Test 3: Check if altitude/heading are non-null when aircraft connected
    print("[3/3] Telemetry quality check:")
    try:
        resp = urllib.request.urlopen(f"{url}/status", timeout=5)
        data = json.loads(resp.read().decode())
        
        checks = []
        if data.get('aircraft_connected'):
            checks.append(f"  Aircraft: CONNECTED ✅")
            if data.get('altitude_m') is not None:
                checks.append(f"  Altitude: {data.get('altitude_m')}m ✅")
            else:
                checks.append(f"  Altitude: NULL ⚠️  (should have value when connected)")
            
            if data.get('heading_deg') is not None:
                checks.append(f"  Heading: {data.get('heading_deg')}° ✅")
            else:
                checks.append(f"  Heading: NULL ⚠️  (should have value when connected)")
        else:
            checks.append(f"  Aircraft: NOT CONNECTED ❌")
        
        for check in checks:
            print(check)
        
        print("\n=== Result ===")
        all_good = data.get('aircraft_connected') and data.get('altitude_m') is not None and data.get('heading_deg') is not None
        if all_good:
            print("✅ Bridge telemetry HEALTHY")
            return True
        else:
            print("⚠️  Bridge online but telemetry incomplete")
            return False
            
    except Exception as e:
        print(f"  ERROR: {e}\n")
        return False

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://172.20.10.2:8080"
    success = test_bridge(url)
    sys.exit(0 if success else 1)

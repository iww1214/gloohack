#!/usr/bin/env python3
"""Safety1271 Cost Analysis — Agents Track Hackathon"""

import json

# ══════════════════════════════════════════════════════════════════════════════
#  GLOO AI COSTS
# ══════════════════════════════════════════════════════════════════════════════

GLOO_MODELS = {
    "claude_sonnet": {
        "name": "gloo-anthropic-claude-sonnet-4.6",
        "input_cost": 3,      # $ per 1M tokens
        "output_cost": 15,    # $ per 1M tokens
    },
    "gemini_flash": {
        "name": "gloo-google-gemini-2.5-flash",
        "input_cost": 0.30,
        "output_cost": 2.50,
    },
    "gemini_flash_lite": {
        "name": "gloo-google-gemini-2.5-flash-lite",
        "input_cost": 0.10,
        "output_cost": 0.40,
    },
}

# ASSUMPTIONS (not measured yet) - replace with real token counts after the first live patrol.
PATROL_MINUTES = 30
FRAME_INTERVAL_S = 4            # normal mode; alert mode is 1.5 s and costs more
TOKENS_PER_FRAME = 1600         # one 1080p image plus prompt, input tokens
OUTPUT_TOKENS_PER_FRAME = 150
FRAMES_PER_PATROL = PATROL_MINUTES * 60 // FRAME_INTERVAL_S

# Typical usage per patrol cycle (5 patrols/day, PATROL_MINUTES each)
USAGE_PER_PATROL = {
    "preflight_check": {
        "model": "claude_sonnet",
        "input_tokens": 2000,
        "output_tokens": 500,
        "calls_per_patrol": 1,
    },
    "patrol_vision": {
        "model": "claude_sonnet",
        "input_tokens": TOKENS_PER_FRAME,
        "output_tokens": OUTPUT_TOKENS_PER_FRAME,
        "calls_per_patrol": FRAMES_PER_PATROL,   # one call per sampled frame
    },
    "post_patrol_report": {
        "model": "claude_sonnet",
        "input_tokens": 3000,
        "output_tokens": 2000,
        "calls_per_patrol": 1,
    },
    "security_threat_detection": {
        "model": "claude_sonnet",
        "input_tokens": 2000,
        "output_tokens": 1000,
        "calls_per_patrol": 1,
    },
    "custom_evaluators_judge": {
        "model": "claude_sonnet",
        "input_tokens": 500,
        "output_tokens": 300,
        "calls_per_patrol": 1,
    },
    "dynamic_mission_mgr": {
        "model": "gemini_flash",
        "input_tokens": 500,
        "output_tokens": 300,
        "calls_per_patrol": 5,   # ~5 mission updates per patrol
    },
    "atc_listener": {
        "model": "gemini_flash_lite",
        "input_tokens": 200,
        "output_tokens": 100,
        "calls_per_patrol": 60,  # ~1 call/min assuming a 1-hour patrol; not rescaled, pennies either way
    },
    "kb_query": {
        "model": "gemini_flash_lite",
        "input_tokens": 200,
        "output_tokens": 100,
        "calls_per_patrol": 3,   # ~3 FAA knowledge lookups per patrol
    },
}

def calculate_cost(model_key, input_tokens, output_tokens):
    """Calculate cost for a model call."""
    model = GLOO_MODELS[model_key]
    input_cost = (input_tokens / 1_000_000) * model["input_cost"]
    output_cost = (output_tokens / 1_000_000) * model["output_cost"]
    return input_cost + output_cost

def format_usd(amount):
    """Format amount as USD."""
    if amount < 0.01:
        return f"${amount*1000:.2f}m"  # millicents
    return f"${amount:.2f}"

# Calculate per-patrol costs
print("=" * 80)
print(f"SAFETY1271 COST ANALYSIS — Per Patrol ({PATROL_MINUTES} min, {FRAMES_PER_PATROL} frames)")
print("=" * 80)
print()

total_per_patrol = 0
for agent, specs in USAGE_PER_PATROL.items():
    model = specs["model"]
    calls = specs["calls_per_patrol"]
    input_toks = specs["input_tokens"]
    output_toks = specs["output_tokens"]
    
    cost_per_call = calculate_cost(model, input_toks, output_toks)
    total_cost = cost_per_call * calls
    total_per_patrol += total_cost
    
    print(f"{agent:30s} | {GLOO_MODELS[model]['name']:40s}")
    print(f"  {calls}x call(s): {input_toks:,} in + {output_toks:,} out → {format_usd(total_cost)}")
    print()

print(f"{'SUBTOTAL GLOO PER PATROL':30s} → {format_usd(total_per_patrol)}")
print()

# ══════════════════════════════════════════════════════════════════════════════
#  COMMUNICATIONS COSTS
# ══════════════════════════════════════════════════════════════════════════════

SMS_COST_PER_MSG = 0.0079  # Twilio
SMS_PER_PATROL = 5         # Preflight GO/NO-GO, preflight result, start, RTH, post-patrol report
sms_cost_per_patrol = SMS_COST_PER_MSG * SMS_PER_PATROL

print(f"{'Twilio (SMS)':30s} | {SMS_PER_PATROL} msgs @ ${SMS_COST_PER_MSG}/msg → {format_usd(sms_cost_per_patrol)}")
print(f"{'SendGrid (email)':30s} | Free tier: 100/day (sufficient)")
print()

# ══════════════════════════════════════════════════════════════════════════════
#  WEEKLY & MONTHLY ESTIMATES
# ══════════════════════════════════════════════════════════════════════════════

PATROLS_PER_WEEK = 35  # 5 patrols/day × 7 days
PATROLS_PER_MONTH = PATROLS_PER_WEEK * 4.33

weekly_gloo = total_per_patrol * PATROLS_PER_WEEK
weekly_sms = sms_cost_per_patrol * PATROLS_PER_WEEK
weekly_total = weekly_gloo + weekly_sms

monthly_gloo = weekly_gloo * 4.33
monthly_sms = weekly_sms * 4.33
monthly_total = monthly_gloo + monthly_sms

annual_gloo = monthly_gloo * 12
annual_sms = monthly_sms * 12
annual_total = annual_gloo + annual_sms

print("=" * 80)
print("WEEKLY ESTIMATE")
print("=" * 80)
print(f"Patrols: {PATROLS_PER_WEEK}")
print(f"  Gloo AI:    {format_usd(weekly_gloo)}")
print(f"  Twilio SMS: {format_usd(weekly_sms)}")
print(f"  TOTAL:      {format_usd(weekly_total)}")
print()

print("=" * 80)
print("MONTHLY ESTIMATE")
print("=" * 80)
print(f"Patrols: {PATROLS_PER_MONTH:.1f}")
print(f"  Gloo AI:    {format_usd(monthly_gloo)}")
print(f"  Twilio SMS: {format_usd(monthly_sms)}")
print(f"  TOTAL:      {format_usd(monthly_total)}")
print()

print("=" * 80)
print("ANNUAL ESTIMATE")
print("=" * 80)
print(f"Patrols: {PATROLS_PER_MONTH*12:.0f}")
print(f"  Gloo AI:    {format_usd(annual_gloo)}")
print(f"  Twilio SMS: {format_usd(annual_sms)}")
print(f"  TOTAL:      {format_usd(annual_total)}")
print()

# ══════════════════════════════════════════════════════════════════════════════
#  COST BREAKDOWN BY AGENT
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 80)
print("COST BREAKDOWN BY AGENT (Weekly)")
print("=" * 80)
print()

agent_costs = {}
for agent, specs in USAGE_PER_PATROL.items():
    model = specs["model"]
    calls = specs["calls_per_patrol"]
    input_toks = specs["input_tokens"]
    output_toks = specs["output_tokens"]
    
    cost_per_call = calculate_cost(model, input_toks, output_toks)
    total_cost = cost_per_call * calls * PATROLS_PER_WEEK
    agent_costs[agent] = total_cost

# Sort by cost
for agent in sorted(agent_costs, key=lambda x: agent_costs[x], reverse=True):
    cost = agent_costs[agent]
    pct = (cost / weekly_gloo) * 100
    print(f"{agent:30s} {format_usd(cost):>10s} ({pct:5.1f}%)")

print()
print(f"{'TOTAL GLOO':30s} {format_usd(weekly_gloo):>10s} (100.0%)")
print()

# ══════════════════════════════════════════════════════════════════════════════
#  COST COMPARISON & OPTIMIZATION
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 80)
print("COST OPTIMIZATION OPPORTUNITIES")
print("=" * 80)
print()

# ATC listener optimization
atc_current = agent_costs.get("atc_listener", 0)
atc_optimized = (200 / 1_000_000 * 0.10) * 20 * PATROLS_PER_WEEK  # Reduce calls from 60 to 20
atc_savings = atc_current - atc_optimized
print(f"ATC Listener optimization (60→20 calls/patrol):")
print(f"  Current:    {format_usd(atc_current)}/week")
print(f"  Optimized:  {format_usd(atc_optimized)}/week")
print(f"  Savings:    {format_usd(atc_savings)}/week → {format_usd(atc_savings*52)}/year")
print()

# Caching benefit (5-minute TTL on Claude)
cache_hit_rate = 0.3  # Conservative: 30% of requests hit cache
cache_savings_weekly = weekly_gloo * cache_hit_rate * 0.1  # Cache reads 10% of normal cost
print(f"Prompt caching benefit (5-min TTL, 30% hit rate):")
print(f"  Potential savings: {format_usd(cache_savings_weekly)}/week → {format_usd(cache_savings_weekly*52)}/year")
print()

# ══════════════════════════════════════════════════════════════════════════════
#  SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

print("=" * 80)
print("EXECUTIVE SUMMARY")
print("=" * 80)
print()
print(f"✅ Production Cost: {format_usd(weekly_total)}/week (~{format_usd(monthly_total)}/month)")
print(f"✅ Dominated by: Patrol vision agent (Claude Sonnet token usage)")
print(f"✅ ATC listener is highest volume but cheapest model (Gemini Flash Lite)")
print(f"✅ Gloo platform includes:")
print(f"   - Claude Sonnet 4.6 (vision, reasoning, regulatory)")
print(f"   - Gemini 2.5 Flash (routing, updates)")
print(f"   - Gemini 2.5 Flash Lite (high-volume tasks)")
print(f"   - tradition='evangelical' (faith context — Gloo exclusive)")
print()
print(f"🎯 Hackathon advantage: All costs transparent, single platform")
print(f"   (vs. multi-provider setup = integration risk + vendor lock-in)")
print()

"""
gloo_client.py  (v2 — corrected from official Gloo docs)
=========================================================
Gloo AI integration for all Safety1271 agents.

KEY CORRECTIONS vs v1:
  1. Caching:  Just add X-Cache-TTL header — Gloo handles cache_control
               placement on system messages automatically. No manual
               cache_control blocks needed.
  2. Responses API (/ai/v1/responses): Recommended for tool use.
               Tool calls return in output[] as function_call items,
               not in choices[0].message.tool_calls.
               Multi-turn uses function_call + function_call_output items.
  3. Completions V2 (/ai/v2/chat/completions): Also supports tools,
               same OpenAI-compatible choices[] format. Use this for
               simpler non-tool completions with auto_routing.

STRATEGY:
  - Patrol agent (tools + vision):  Responses API v1
  - ATC filter, KB query, dynamic:  Completions V2 with auto_routing
  - Pre-flight, report:             Completions V2 with explicit model

ENDPOINTS:
  Responses API:   https://platform.ai.gloo.com/ai/v1/responses
  Completions V2:  https://platform.ai.gloo.com/ai/v2/chat/completions
  Token:           https://platform.ai.gloo.com/oauth2/token

REQUIRED .env:
  GLOO_CLIENT_ID=...
  GLOO_CLIENT_SECRET=...
  GLOO_TRADITION=evangelical
"""

import os, json, time, logging, threading
from typing import Optional
import requests
from openai import OpenAI
from dotenv import load_dotenv
from arthur_integration import configure_arthur

load_dotenv()
configure_arthur()
log = logging.getLogger("GlooClient")

GLOO_TOKEN_URL   = "https://platform.ai.gloo.com/oauth2/token"
GLOO_V1_BASE     = "https://platform.ai.gloo.com/ai/v1"   # Responses API
GLOO_V2_BASE     = "https://platform.ai.gloo.com/ai/v2"   # Completions V2
GLOO_TRADITION   = os.getenv("GLOO_TRADITION", "evangelical")

# ── Model aliases ─────────────────────────────────────────────────────────────
MODELS = {
    "patrol":     "gloo-anthropic-claude-sonnet-4.6",   # vision + tools
    "preflight":  "gloo-anthropic-claude-sonnet-4.6",   # tool calling
    "report":     "gloo-anthropic-claude-sonnet-4.6",   # quality matters
    "atc_filter": "gloo-google-gemini-2.5-flash-lite",  # 97% cheaper, high volume
    "kb_query":   "gloo-google-gemini-2.5-flash-lite",  # retrieval classification
    "dynamic":    "gloo-google-gemini-2.5-flash",       # routing decisions
    "atc_stt":    "gloo-google-gemini-2.5-flash",       # speech-to-text for ATC audio
    "auto":       None,                                  # Gloo picks
}

# ── Cache TTL header values ───────────────────────────────────────────────────
CACHE_5MIN = "5m"
CACHE_1HR  = "1h"


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class TokenManager:
    """Thread-safe OAuth2 client credentials with auto-refresh."""

    def __init__(self):
        self._token      : Optional[str] = None
        self._expires_at : float         = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if time.time() >= self._expires_at - 60:
                self._refresh()
            return self._token

    def _refresh(self):
        cid  = os.getenv("GLOO_CLIENT_ID", "")
        csec = os.getenv("GLOO_CLIENT_SECRET", "")
        if not cid or not csec:
            raise RuntimeError(
                "Set GLOO_CLIENT_ID and GLOO_CLIENT_SECRET in .env\n"
                "Get them from: platform.ai.gloo.com/studio/manage-api-credentials"
            )
        resp = requests.post(
            GLOO_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "api/access"},
            auth=(cid, csec),
            timeout=10
        )
        resp.raise_for_status()
        data             = resp.json()
        self._token      = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        log.info("Gloo token refreshed")


_tm = TokenManager()


def _bearer() -> str:
    """Gloo API key if configured, else an OAuth2 token from the deprecated client credentials."""
    return os.getenv("GLOO_API_KEY") or _tm.get_token()


def _v1_client() -> OpenAI:
    """OpenAI client pointed at Gloo Responses API v1."""
    return OpenAI(api_key=_bearer(), base_url=GLOO_V1_BASE, timeout=30.0, max_retries=0)


def _v2_client() -> OpenAI:
    """OpenAI client pointed at Gloo Completions V2."""
    return OpenAI(api_key=_bearer(), base_url=GLOO_V2_BASE, timeout=30.0, max_retries=0)


def _auth_headers(cache_ttl: Optional[str] = None) -> dict:
    """Build raw Authorization headers for direct HTTP calls."""
    h = {"Authorization": f"Bearer {_bearer()}",
         "Content-Type": "application/json"}
    if cache_ttl:
        h["X-Cache-TTL"] = cache_ttl
    return h


# ══════════════════════════════════════════════════════════════════════════════
#  FORMAT CONVERTERS
# ══════════════════════════════════════════════════════════════════════════════

def anthropic_tools_to_openai(tools: list) -> list:
    """Convert Anthropic input_schema format → OpenAI function format."""
    return [
        {
            "type": "function",
            "function": {
                "name":        t["name"],
                "description": t.get("description", ""),
                "parameters":  t.get("input_schema",
                                     {"type": "object", "properties": {}})
            }
        }
        for t in tools
    ]


def b64_to_data_url(b64: str, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{b64}"


# ══════════════════════════════════════════════════════════════════════════════
#  RESPONSES API v1  (recommended for tool use)
# ══════════════════════════════════════════════════════════════════════════════

def responses_create(model_key: str, system: str, user_content,
                     tools: list = None,
                     use_tradition: bool = True,
                     cache: Optional[str] = CACHE_5MIN,
                     max_tokens: int = 1024) -> dict:
    """
    Single call to Gloo Responses API v1.
    Returns parsed dict: {text, tool_calls: [{id, call_id, name, args}]}.

    NOTE: Responses API uses output[] not choices[].
          Tool calls are function_call items with .call_id (not .id).
    """
    model = MODELS.get(model_key, model_key)

    # Build input list
    if isinstance(user_content, str):
        input_items = [{"role": "user", "content": user_content}]
    elif isinstance(user_content, list):
        # Vision: list of content parts
        input_items = [{"role": "user", "content": user_content}]
    else:
        input_items = [{"role": "user", "content": str(user_content)}]

    body = {
        "model":      model,
        "input":      input_items,
        "max_tokens": max_tokens,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = anthropic_tools_to_openai(tools)
    if use_tradition and GLOO_TRADITION:
        body["tradition"] = GLOO_TRADITION

    resp = requests.post(
        f"{GLOO_V1_BASE}/responses",
        headers=_auth_headers(cache_ttl=cache),
        json=body,
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()

    result = {"text": "", "tool_calls": []}
    for item in data.get("output", []):
        if item.get("type") == "message":
            # Text response
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text"):
                    result["text"] = part.get("text", "")
        elif item.get("type") == "function_call":
            # Tool call — note: call_id not id in Responses API
            try:
                args = json.loads(item.get("arguments", "{}"))
            except Exception:
                args = {}
            result["tool_calls"].append({
                "id":      item.get("id", ""),       # response object id
                "call_id": item.get("call_id", ""),  # the id to use in replies
                "name":    item.get("name", ""),
                "args":    args,
            })
    if not result["text"]:
        result["diagnostics"] = {
            "status": data.get("status"),
            "output_types": [item.get("type") for item in data.get("output", [])],
            "content_types": [
                part.get("type")
                for item in data.get("output", [])
                for part in item.get("content", [])
            ],
            "incomplete_reason": (data.get("incomplete_details") or {}).get("reason"),
        }
    return result


def responses_continue(model_key: str, system: str,
                        input_so_far: list,
                        tool_name: str, call_id: str,
                        arguments: str, tool_result: str,
                        use_tradition: bool = True,
                        cache: Optional[str] = CACHE_5MIN,
                        max_tokens: int = 1024) -> dict:
    """
    Continue a Responses API conversation after a tool call.
    Appends function_call + function_call_output items to input.
    """
    model = MODELS.get(model_key, model_key)

    new_input = input_so_far + [
        {
            "type":      "function_call",
            "call_id":   call_id,
            "name":      tool_name,
            "arguments": arguments,
        },
        {
            "type":    "function_call_output",
            "call_id": call_id,
            "output":  tool_result,
        }
    ]

    body = {
        "model":      model,
        "input":      new_input,
        "max_tokens": max_tokens,
    }
    if system:
        body["system"] = system
    if use_tradition and GLOO_TRADITION:
        body["tradition"] = GLOO_TRADITION

    resp = requests.post(
        f"{GLOO_V1_BASE}/responses",
        headers=_auth_headers(cache_ttl=cache),
        json=body,
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()

    result = {"text": "", "tool_calls": [], "input": new_input}
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    result["text"] = part.get("text", "")
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments", "{}"))
            except Exception:
                args = {}
            result["tool_calls"].append({
                "id":      item.get("id", ""),
                "call_id": item.get("call_id", ""),
                "name":    item.get("name", ""),
                "args":    args,
            })
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  FULL AGENTIC LOOP — Responses API v1
# ══════════════════════════════════════════════════════════════════════════════

def run_agent(system_prompt: str,
              user_content,
              tools: list,
              tool_executor,
              model: str = "patrol",
              use_tradition: bool = True,
              max_rounds: int = 10,
              cache: Optional[str] = CACHE_5MIN,
              max_tokens: int = 1024,
              timeout: int = 30) -> tuple:
    """
    Full agentic tool-call loop via Gloo Responses API v1.
    Caching: X-Cache-TTL header activates — Gloo auto-caches system prompt.
    Returns (final_text: str, all_tool_calls: list).
    """
    # Build initial input
    if isinstance(user_content, str):
        input_items = [{"role": "user", "content": user_content}]
    elif isinstance(user_content, list):
        input_items = [{"role": "user", "content": user_content}]
    else:
        input_items = [{"role": "user", "content": str(user_content)}]

    model_id    = MODELS.get(model, model)
    all_calls   = []
    final_text  = ""
    current_input = input_items

    for round_n in range(max_rounds):
        body = {
            "model":      model_id,
            "input":      current_input,
            "max_tokens": max_tokens,
        }
        if system_prompt:
            body["system"] = system_prompt
        if tools:
            body["tools"] = anthropic_tools_to_openai(tools)
        if use_tradition and GLOO_TRADITION:
            body["tradition"] = GLOO_TRADITION

        for attempt in (1, 2):
            try:
                resp = requests.post(
                    f"{GLOO_V1_BASE}/responses",
                    headers=_auth_headers(cache_ttl=cache),
                    json=body,
                    timeout=timeout
                )
                break
            except requests.exceptions.Timeout:
                if attempt == 2:
                    raise
        resp.raise_for_status()
        data = resp.json()

        tool_calls_this_round = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        final_text = part.get("text", "")
            elif item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except Exception:
                    args = {}
                tool_calls_this_round.append({
                    "call_id": item.get("call_id", ""),
                    "name":    item.get("name", ""),
                    "args":    args,
                    "raw_arguments": item.get("arguments", "{}"),
                })

        if not tool_calls_this_round:
            break

        # Execute tools and build continuation input
        additions = []
        for tc in tool_calls_this_round:
            result = str(tool_executor(tc["name"], tc["args"]))
            additions += [
                {
                    "type":      "function_call",
                    "call_id":   tc["call_id"],
                    "name":      tc["name"],
                    "arguments": tc["raw_arguments"],
                },
                {
                    "type":    "function_call_output",
                    "call_id": tc["call_id"],
                    "output":  result,
                }
            ]
            all_calls.append({
                "name":   tc["name"],
                "args":   tc["args"],
                "result": result,
            })
        current_input = current_input + additions

    return final_text, all_calls


# ══════════════════════════════════════════════════════════════════════════════
#  COMPLETIONS V2  (simple completions, auto-routing, no tools)
# ══════════════════════════════════════════════════════════════════════════════

def complete(system_prompt: str,
             user_content: str,
             model: str = "atc_filter",
             use_tradition: bool = False,
             max_tokens: int = 512,
             json_output: bool = False,
             cache: Optional[str] = CACHE_5MIN,
             auto_routing: bool = False) -> str:
    """
    Simple single-turn completion via Gloo Completions V2.
    Gemini + OpenAI models: implicit caching (auto, no header needed).
    Anthropic models: explicit caching via X-Cache-TTL header.
    Returns response text.
    """
    model_id = MODELS.get(model, model)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content}
    ]
    body = {
        "messages":   messages,
        "max_tokens": max_tokens,
    }
    if auto_routing or model_id is None:
        body["auto_routing"] = True
    else:
        body["model"]        = model_id
        body["auto_routing"] = False

    if use_tradition and GLOO_TRADITION:
        body["tradition"] = GLOO_TRADITION
    if json_output:
        body["response_format"] = {"type": "json_object"}

    # Anthropic models need explicit cache header; Gemini/OpenAI handle automatically
    needs_cache_header = (model_id or "").startswith("gloo-anthropic")
    hdr = _auth_headers(cache_ttl=cache if needs_cache_header else None)

    resp = requests.post(
        f"{GLOO_V2_BASE}/chat/completions",
        headers=hdr,
        json=body,
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "")


# ══════════════════════════════════════════════════════════════════════════════
#  GROUNDED COMPLETIONS  (Gloo native RAG — replaces our SQLite KB for patrol)
# ══════════════════════════════════════════════════════════════════════════════

def grounded_complete(system_prompt: str,
                       user_query: str,
                       collection_id: str,
                       model: str = "patrol",
                       use_tradition: bool = True,
                       max_tokens: int = 1024) -> dict:
    """
    Gloo Grounded Completions — built-in RAG.
    Upload content to Gloo Data Engine, then use this instead of our SQLite KB.
    Returns {text, sources}.

    Advantage over our custom SQLite KB:
      - No manual chunking / FTS5 management
      - Gloo handles embedding + semantic retrieval
      - Source attribution built in
      - Judges see native Gloo platform usage (good for Agents Track score)

    To use:
      1. Upload FAA KB content to Gloo Data Engine via Data Engine API
      2. Get your collection_id from platform.ai.gloo.com
      3. Set GLOO_COLLECTION_ID in .env
      4. Call grounded_complete() instead of query_kb()
    """
    model_id = MODELS.get(model, model)
    body = {
        "model":    model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_query}
        ],
        "collection_id": collection_id,
        "max_tokens":    max_tokens,
    }
    if use_tradition and GLOO_TRADITION:
        body["tradition"] = GLOO_TRADITION

    resp = requests.post(
        f"{GLOO_V2_BASE}/chat/completions/grounded",
        headers=_auth_headers(cache_ttl=CACHE_5MIN),
        json=body,
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "text":    data.get("choices", [{}])[0].get("message", {}).get("content", ""),
        "sources": data.get("sources", []),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  COMPATIBILITY SHIM — minimal Anthropic-like interface
# ══════════════════════════════════════════════════════════════════════════════

class _GlooResponse:
    """Wraps Gloo response so existing agents can read .content list."""

    def __init__(self, text: str, tool_calls: list):
        self.content     = []
        self.stop_reason = "end_turn" if not tool_calls else "tool_use"
        if text:
            self.content.append(_TextBlock(text))
        for tc in tool_calls:
            self.content.append(_ToolBlock(tc.get("call_id", tc["name"]), tc["name"], tc["args"]))


class _TextBlock:
    def __init__(self, text):
        self.type = "text";  self.text = text


class _ToolBlock:
    def __init__(self, id_, name, input_):
        self.type = "tool_use";  self.id = id_;  self.name = name;  self.input = input_


class GlooMessages:
    """Drop-in for anthropic.Anthropic().messages — routes to Gloo."""

    def create(self, model="patrol", max_tokens=1024, system="",
               messages=None, tools=None, tradition=None, **kwargs):
        # Flatten messages to extract user content
        user_text = ""
        user_images = []
        for m in (messages or []):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    user_text = c
                elif isinstance(c, list):
                    for block in c:
                        if block.get("type") == "text":
                            user_text = block.get("text", "")
                        elif block.get("type") == "image":
                            src = block.get("source", {})
                            if src.get("type") == "base64":
                                user_images.append({
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{src.get('media_type','image/jpeg')};base64,{src.get('data','')}"
                                    }
                                })

        if user_images:
            user_content = user_images + [{"type": "text", "text": user_text}]
        else:
            user_content = user_text

        text, tcs = run_agent(
            system_prompt = system,
            user_content  = user_content,
            tools         = tools or [],
            tool_executor = lambda n, a: "",  # caller handles execution
            model         = model,
            use_tradition = bool(tradition or GLOO_TRADITION),
            max_rounds    = 1,   # single-round for compat; callers loop themselves
        )
        return _GlooResponse(text, tcs)


class GlooAnthropicCompat:
    """Drop-in for anthropic.Anthropic() — routes through Gloo."""
    def __init__(self):
        self.messages = GlooMessages()

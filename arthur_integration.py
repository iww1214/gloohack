"""Optional Arthur Platform instrumentation for Safety1271."""

import logging
import os

log = logging.getLogger("ArthurIntegration")
_arthur = None


def configure_arthur():
    """Initialize Arthur when explicitly configured; otherwise remain offline-safe."""
    global _arthur
    if _arthur is not None:
        return _arthur

    api_key = os.getenv("ARTHUR_API_KEY")
    task_id = os.getenv("ARTHUR_TASK_ID")
    if not api_key or not task_id:
        log.info("Arthur instrumentation disabled: ARTHUR_API_KEY/TASK_ID not set")
        return None

    try:
        from arthur_observability_sdk import Arthur

        _arthur = Arthur(
            api_key=api_key,
            base_url=os.getenv("ARTHUR_BASE_URL", "http://localhost:3030"),
            task_id=task_id,
        )
        _arthur.instrument_openai()
        log.info("Arthur instrumentation enabled for task %s", task_id)
        return _arthur
    except ImportError:
        log.warning("Arthur configured but arthur-observability-sdk is not installed")
    except Exception as error:
        log.warning("Arthur instrumentation unavailable: %s", error)
    return None


def shutdown_arthur() -> None:
    if _arthur is not None:
        try:
            _arthur.shutdown()
        except Exception as error:
            log.warning("Arthur shutdown failed: %s", error)

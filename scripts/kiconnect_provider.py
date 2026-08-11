"""
KIConnect OpenAI-compatible provider with concurrency gating.

KIConnect (chat.kiconnect.nrw) enforces a hard limit of 3 concurrent requests account-wide.
This module:
1. Provides a concurrency gate that wraps LiteLLM calls
2. Ensures no more than max_concurrent requests are in-flight at once
3. Lets OpenHands call the LLM normally without awareness of the limit
"""

import threading


class ConcurrencyLimitedOpenAICompatible:
    """
    Wraps OpenAI-compatible LLM calls with a semaphore to enforce max concurrent requests.

    Used for endpoints like KIConnect that have a hard account-wide concurrency limit.
    All calls via this gate share one Semaphore — construct exactly ONE per process.
    """

    def __init__(self, max_concurrent: int):
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        self._sem = threading.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent

    def acquire(self):
        """Block until a slot is available, then acquire it."""
        self._sem.acquire()

    def release(self):
        """Release a slot."""
        self._sem.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# Global gate instance for KIConnect (3 concurrent requests)
_kiconnect_gate = None


def get_kiconnect_gate(max_concurrent: int = 3):
    """
    Get or create the global KIConnect concurrency gate.

    Thread-safe: multiple calls return the same gate instance.
    """
    global _kiconnect_gate
    if _kiconnect_gate is None:
        _kiconnect_gate = ConcurrencyLimitedOpenAICompatible(max_concurrent)
    return _kiconnect_gate

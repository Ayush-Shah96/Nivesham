"""Process-wide admission control for local Ollama inference.

The application is CPU/RAM constrained on developer workstations.  A single
async lock prevents CRISP, YouTube and interactive requests from loading or
running the same local model concurrently in separate HTTP requests.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

# One slot per backend process.  Keep this module imported by every local
# Ollama caller so the lock is shared across features.
_INFERENCE_SLOT = asyncio.Semaphore(1)


@asynccontextmanager
async def local_inference_slot() -> AsyncIterator[None]:
    """Wait until the local model is free, then reserve it for one request."""
    async with _INFERENCE_SLOT:
        yield

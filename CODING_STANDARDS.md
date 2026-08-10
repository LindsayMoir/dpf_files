# CODING_STANDARDS.md

## PILLAR 2: PYTHONIC EXCELLENCE (PEP 8 & BEYOND)
- TYPE HYGIENE: Mandatory type hints (PEP 484). Prefer `dataclasses` from stdlib; use Pydantic only if already adopted or at external I/O boundaries.
- IDIOMATIC CODE: Use comprehensions, generators, and context managers (`with`).
- ASYNC & SYNC: This repo contains both styles. Keep boundaries clear. In async paths, avoid blocking; offload CPU/blocking I/O via `asyncio.to_thread`/executors. In sync paths, do not introduce async unless required. Provide separate async and sync entry points or thin wrappers; do not mix within a single function/call stack.
- DOCSTRINGS: Use Google- or NumPy-style for public modules, classes, and functions.

### Async/Sync Interop
- No blocking calls inside `async def`; wrap with `asyncio.to_thread` or an executor.
- Do not call `asyncio.run` from library code or inside a running loop. Use it only at top-level entry points (CLI/scripts). Let application/framework manage the event loop.
- If exposing both interfaces, prefer primary async implementations with optional sync wrappers, or maintain parallel implementations. Use matching client libraries per context (async clients for async code; sync clients for sync code).
- Do not share connection/client instances across async and sync code paths.

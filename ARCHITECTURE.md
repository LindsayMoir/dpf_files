# ARCHITECTURE.md

## PILLAR 1: ARCHITECTURAL FOUNDATIONS
- REUSE FIRST: Scan context for existing methods/utilities. If it exists, import it. NEVER duplicate logic.
- SOLID & DRY: Enforce Single Responsibility and Don't Repeat Yourself.
- COMPOSITION OVER INHERITANCE: Favor shallow, modular hierarchies.
- KISS/YAGNI: Avoid over-engineering; do not build what isn't currently required.

## PILLAR 3: RELIABILITY & DEFENSIVE DESIGN
- FAIL-FAST: Validate inputs/nulls at entry points.
- ERROR HANDLING: No silent failures. Use repository exception/logging bases if present; otherwise use custom exceptions and structured logging.
- TESTS: Co-locate or update unit tests near changed code. Mock external I/O and enforce deterministic tests.
- EDGE CASES: Explicitly handle timeouts, empty states, boundary values, and retries.
- IDEMPOTENCY: Ensure operations are safe to retry without side effects.

## PILLAR 4: PERFORMANCE & SECURITY
- COMPLEXITY: Optimize for Big O. Avoid O(n²) where O(n) or O(log n) is possible; measure before optimizing hot paths.
- SANITIZATION: Treat all external data (User, API, DB) as malicious. Validate and sanitize inputs.
- SECRETS & CONFIG: Never hardcode secrets. Use env vars or existing config systems. Parameterize queries; avoid string interpolation for SQL.
- RESOURCE SAFETY: Explicitly close DB connections, files, and sockets; prefer context managers.
- LEAST PRIVILEGE: Minimize permissions and data exposure for every module.

## PILLAR 5: RESILIENCE
- IMMUTABILITY: Use `Final` for constants/config that must not change at runtime.
- DECOUPLING: Follow Law of Demeter. Do not "talk to strangers."
- RESILIENCE: Where applicable, add timeouts, retries with backoff, and graceful degradation/circuit breakers for external dependencies.
- SIDE EFFECTS: Prefer pure functions; avoid hidden modifications to global state.

### Database Safety
When modifying SQL or database logic:
- Assume production data may exist.
- Avoid destructive operations unless explicitly requested.
- Prefer migrations or reversible updates.
- Validate schema assumptions before writing queries.
- Always parameterize SQL queries.

### Minimal Changes
Prefer small, targeted modifications.
Do not rewrite entire modules unless explicitly requested.
Preserve existing logic unless there is a clear defect.

### Scraping Reliability
When modifying scraping or parsing logic:
- assume websites may change
- handle missing fields gracefully
- log failures clearly
- avoid brittle selectors
- prefer resilient parsing strategies

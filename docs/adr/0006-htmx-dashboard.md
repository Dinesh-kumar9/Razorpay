# ADR 0006 - HTMX Dashboard (Server-Rendered)

Date: 2024-08-18
Status: Accepted

## Decision
HTMX with Jinja2 templates served by FastAPI. No React, no npm, no client-side state.

## Consequences
- No separate frontend build step.
- Full system is a single Python process: data to decision to audit to UI.
- Filtering uses HTMX hx-get with server-side query parameters.
- Bar chart is pure CSS; no JS charting library needed.

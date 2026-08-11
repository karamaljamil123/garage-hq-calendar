# Garage HQ — a calendar you actually want to open

A personal planner styled as a cozy night garage: red neon, sunset windows, a football game on the TV, and a sleeping Cavalier King Charles Spaniel named Cassio on the couch. Underneath the scene is a real calendar app.

**Live demo:** https://karamaljamil123.github.io/garage-hq-calendar/

## Features

- **Month view** (Monday start) and a **week view** with an hourly time grid and a live red "now" line
- Events with start/end times, four color-coded categories, and click-to-edit
- **Flexible recurrence** — one-time, weekly on any set of days (M–S picker), or monthly, with an optional end date
- **Reminders** — browser notifications with a chosen lead time (5 min to 1 day), plus in-app toasts and an optional horn sound
- A notepad for quick reminders (click a note to edit it)
- Past days are crossed off like a real wall calendar
- Everything saved in the browser via localStorage

## Tech

One self-contained HTML file — no frameworks, no build step, no backend, no dependencies. The garage scene is hand-drawn CSS and SVG with compositor-friendly animations (and `prefers-reduced-motion` support). Open `index.html` in any browser, or use the live link above.

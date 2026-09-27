# What changed

Copy this whole folder's contents into your real `ams.lavison` project,
overwriting the matching files/paths. Keep your existing
`login.html`, `dashboard.html`, `students.html`, `courses.html`,
`sessions.html`, `settings.html`, `attendance.html`,
`student_details.html`, and `attendance.db` — those are not touched
or included here.

## Files in this bundle

- `app.py` — bug fixes (see below)
- `database.py` — unchanged, included for completeness
- `requirements.txt` — unchanged
- `nfc/` — unchanged
- `templates/reports.html` — fixed
- `templates/partials/nav.html` — fixed
- `templates/partials/footer.html` — **new file**
- `static/css/style.css` — redesigned
- `static/js/app.js` — fixed

## Bugs fixed in `app.py`

- `students()` and `courses()`: the "delete" action was blocked by the
  "name/code required" validation, which ran first and rejected the
  delete request because a plain delete button doesn't send those
  fields. Reordered so delete runs first (matches how `sessions()`
  already worked).
- Both routes: a couple of early `return redirect(...)` paths weren't
  closing the DB connection first.
- `reports()`: the "Check-in" column was pulling the session's
  scheduled start time instead of the student's actual check-in
  timestamp. Now selects `a.check_in_time`.

## The nav/footer structural fix — action required on your side

`nav.html` used to close `</div></div></body></html>` at its own end.
Because every page template includes `nav.html` and then adds its own
`<main>` content *after* that include, the page body was rendering
**outside** the closed `<html>` tag on every single page — not just
reports. That's what the old CSS hack
(`body > main.page-shell { position: absolute !important; ... }`) was
duct-taping over.

Fix: `nav.html` now stops right after the flash-messages block and
leaves `<div class="content-area">` / `<div class="layout">` open.
`templates/partials/footer.html` (new) closes them.

**You need to add this line right after the closing `</main>` tag in
every other page template** (`dashboard.html`, `students.html`,
`courses.html`, `sessions.html`, `settings.html`, `attendance.html`,
`student_details.html`):

```jinja
{% include 'partials/footer.html' %}
```

`login.html` does NOT need this — it uses the standalone `auth-page`
layout (no sidebar), not `nav.html`.

## CSS

Single consolidated theme (the old file had three stacked/duplicate
`:root` blocks overriding each other — removed). All existing class
names are preserved, so none of your other templates need markup
changes for styling to apply. Uses Google Fonts (Newsreader + Inter)
via `@import`, so the machine serving this needs outbound internet
access for fonts to load — if not, add local font fallbacks.

## Folder layout this all assumes

```
ams.lavison/
├── app.py
├── database.py
├── requirements.txt
├── nfc/
│   ├── __init__.py
│   ├── reader.py
│   └── simulator.py
├── database/                  (created automatically, holds attendance.db)
├── static/
│   ├── css/style.css
│   └── js/app.js
└── templates/
    ├── login.html
    ├── dashboard.html
    ├── students.html
    ├── courses.html
    ├── sessions.html
    ├── settings.html
    ├── attendance.html
    ├── student_details.html
    ├── reports.html
    └── partials/
        ├── nav.html
        └── footer.html
```

Static files (`style.css`, `app.js`) must sit under `static/`, and
`nav.html`/`footer.html` must sit under `templates/partials/` — Flask
won't find them loose in the project root.

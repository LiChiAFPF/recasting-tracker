# Recasting Regulations Tracker — Auto-Update Setup

This repo rebuilds the **Recasting Regulations** deregulation tracker from Airtable
on a schedule and publishes it as a static site. The data is baked into the page at
build time, so the published site is fast and has no live dependency on Airtable —
and the Airtable token never touches the browser.

```
build_tracker.py            the build script (fetch → validate → transform → render)
assets/                     fonts + AFP-F logo (committed; the script embeds them)
.github/workflows/rebuild.yml   the nightly rebuild + deploy job
```

---

## What you need once: a read-only Airtable token

1. Go to **https://airtable.com/create/tokens** (Airtable → your account → *Builder hub → Personal access tokens → Create token*).
2. **Name:** `recasting-tracker-readonly` (anything you like).
3. **Scopes** — add exactly these two (both read-only):
   - `data.records:read`
   - `schema.bases:read`
4. **Access:** add *only* the base that holds the tracker table
   (base ID `appPUrk3pj2BEN3sZ`). Do **not** grant all-workspace access.
5. Click **Create token** and copy the value (starts with `pat...`). Airtable shows
   it **once** — if you lose it, just make a new one.

> The token is read-only. It literally cannot change your table, so there is no risk
> to the data even if it were exposed. We keep it secret anyway, as a matter of hygiene.

---

## Option A — GitHub Pages (recommended, all-in-one, free)

1. **Create a repo** (private is fine) and upload the contents of this folder so the
   layout matches exactly (keep `assets/` and `.github/workflows/` where they are).
2. **Add the token as a secret:** repo **Settings → Secrets and variables → Actions
   → New repository secret.**
   - Name: `AIRTABLE_TOKEN`
   - Value: the `pat...` token you just made.
3. **Turn on Pages:** repo **Settings → Pages → Build and deployment → Source =
   "GitHub Actions."**
4. **Run it once by hand:** repo **Actions → "Rebuild Recasting Regulations tracker"
   → Run workflow.** After ~1 minute it will build and publish. The Pages URL appears
   in the workflow's `deploy` step (and under Settings → Pages).

That's it. From then on it rebuilds every night on the schedule in `rebuild.yml`.
Change the `cron:` line to rebuild more or less often (comments in the file explain how).

### Custom domain (optional)
Under Settings → Pages you can point a subdomain (e.g. `recasting.americansforprosperityfoundation.org`)
at the site. AFP-F's web team handles the DNS `CNAME`.

---

## Option B — Netlify / Vercel / Cloudflare Pages

Any host that can run a build command works. Use:

- **Build command:** `python build_tracker.py index.html`
- **Publish directory:** the repo root (or wherever `index.html` lands)
- **Environment variable:** `AIRTABLE_TOKEN = pat...`
- **Scheduled rebuild:** Netlify "Scheduled Functions" / a build hook on a cron, or
  Cloudflare "Cron Triggers." Point the schedule at a rebuild.

The script is stdlib-only, so no `requirements.txt` / `pip install` is needed.

---

## Running it locally (to test before deploying)

```bash
export AIRTABLE_TOKEN=pat_your_token_here
python build_tracker.py index.html
open index.html          # or just double-click it
```

Expected output ends with something like:

```
OK  wrote index.html  (1,443,xxx bytes)
    1,678 actions | EO 14192: 1,435 | high/high: 216 | cite Loper: 115
```

---

## The safety guard (why a bad table change can't break the live site)

Before it writes anything, the script validates the Airtable table against a known
contract (see `CORE_FIELDS` and `EO_CHECKBOX` near the top of `build_tracker.py`):

- **Every mapped field must still exist with the right type.** If a core field or an
  EO checkbox is renamed away, deleted, or retyped, the script **aborts (exit code 2)
  and writes nothing** — the previous published site stays live. The Actions run shows
  a red X so you know to look.
- **A new, unmapped checkbox triggers a warning, not a failure.** If you add a new EO
  as a new checkbox column, the build still succeeds but prints
  `WARN unmapped checkbox '<name>' [fld...]`. That's your signal to add one line to
  `EO_CHECKBOX` so the new EO shows up in the report.
- **A too-small fetch aborts too.** If fewer than 100 rows come back (a sign of a bad
  fetch rather than a real change), it refuses to overwrite the good build.

### Adding a new EO checkbox to the report
When you add an EO checkbox column in Airtable and see the WARN above, open
`build_tracker.py` and add two lines:

```python
# in EO_CHECKBOX:
"14999": "fldXXXXXXXXXXXXXX",   # <- the new column's field ID (from the WARN line)

# in EO_INFO:
"14999": {"name": "Full EO Title Here", "date": "2026-01-15"},
```

Commit, and the next build includes it everywhere (cards, filter, counts).

> **Field IDs, not names.** The mapping uses Airtable field *IDs* (`fld...`), which do
> **not** change when you rename a column — so renaming a column for clarity is always
> safe. Only deleting/recreating a column changes its ID.

---

## How the EO data model works (by design)

- **Major EOs** are read from their **dedicated checkbox column** — the authoritative
  source of truth. This is where the big counts live (e.g. EO 14192 = 1,435).
- **Minor EOs** (14222, 14238, 14356) have no checkbox; they are read from the
  **multi-select tag field** and simply *added* to a rule's EO list.
- **Checkbox wins.** A stray tag for a major EO does not add it — the checkbox decides.
  This is the agreed rule and prevents double-counting or drift.
- The **"cite only EO 14192"** figure counts rules whose *only tracked* EO flag is
  14192. It tightens automatically as tagging fills in; the report states this caveat.

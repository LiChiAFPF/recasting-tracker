#!/usr/bin/env python3
"""
build_tracker.py  --  Recasting Regulations: Federal Deregulation Tracker
=========================================================================
Fetches the tracker data from Airtable, validates the table's shape against a
known-good contract, then rebuilds the standalone HTML report with fresh data.

Designed to run unattended on a schedule (e.g. a nightly GitHub Action). It is
stdlib-only -- no pip install required.

REQUIRED ENVIRONMENT VARIABLE
    AIRTABLE_TOKEN   A read-only Airtable Personal Access Token with scopes:
                       - data.records:read     (to read the rows)
                       - schema.bases:read      (to validate the table shape)
                     Scope it to ONLY this base. It never needs write access.

USAGE
    AIRTABLE_TOKEN=pat_xxx python build_tracker.py [output.html]

EXIT CODES
    0  success (HTML written)
    2  schema validation failed -- a mapped field is missing or changed type.
       NOTHING is written, so the last good build stays live.
    3  fetch / network / auth error.

WHY THE GUARD MATTERS
    EO associations live in two places in the table: dedicated checkbox columns
    for the major orders, and the multi-select tag field for the handful of
    minor ones. If a checkbox column is renamed, retyped, or a NEW EO checkbox
    is added without being wired in here, a naive script would silently drop
    data (this is exactly how EO 14192 once went invisible). The guard refuses
    to publish in that case, and prints any unmapped checkbox so it can be added.
"""

import os
import sys
import json
import base64
import urllib.request
import urllib.error
import urllib.parse
from collections import Counter
from datetime import date

# =============================================================================
# CONFIG  --  the authoritative contract with the Airtable base
# =============================================================================
BASE_ID = "appPUrk3pj2BEN3sZ"
TABLE_ID = "tblHrsoSoqSoKlpv4"
API = "https://api.airtable.com/v0"

# Core fields (field ID -> expected Airtable type). Validated by the guard.
CORE_FIELDS = {
    "fldRpFlrZUO40mVxE": ("title",           "singleLineText"),
    "fldCJYupgPD0Jf9aV": ("agency",          "singleSelect"),
    "fldsXXljpYgXCnyGe": ("subAgency",       "singleSelect"),
    "fldCbEJizbYcaRcYy": ("ruleType",        "singleSelect"),
    "fldTo9mHipkxesPw3": ("deregImpact",     "singleSelect"),   # Impact of Deregulatory Action
    "fldvcEFyep5EKYhhE": ("impactPotential", "singleSelect"),   # Impact Potential
    "fldpVtSmPDOSDJ5Q2": ("policyArea",      "singleSelect"),
    "fldfseGX1D0EHjQAX": ("datePublished",   None),             # date/text; type not enforced
    "fldpcO04XenqvYh7J": ("deadlineDate",    None),
    "fldO7Ad6cg4wUe8LK": ("url",             None),
    "fldOk5Vfsk3xfA5pQ": ("citation",        None),
    "fldJN9MVwC2gE9p3Z": ("description",     "multilineText"),
    "fldX6IeppsTs62iC6": ("eoTags",          "multipleSelects"),  # tag-only EOs live here
    "fldAHYCrKc1noJlqQ": ("citesLoper",      "checkbox"),
}

# MAJOR EOs -> their dedicated checkbox column (authoritative source of truth).
# Per the agreed rule: checkbox wins for these; the tag field only ADDS the
# minor tag-only EOs below. This mapping is stable and hand-maintained.
EO_CHECKBOX = {
    "14154": "fldfqPkwwysl9xHGo",  # Unleashing American Energy
    "14192": "fldNh6ebD2uUGjMMG",  # Unleashing Prosperity Through Deregulation (the 10-to-1 flagship)
    "14215": "fldhE7GGx8pKlKnDp",  # Ensuring Accountability for All Agencies
    "14219": "fldd3Zk2n7fQZrJrA",  # Ensuring Lawful Governance & DOGE Deregulatory Initiative
    "14267": "fldK6rWxap6vJkf0C",  # Reducing Anti-Competitive Regulatory Barriers
    "14276": "fldJh8EDTt69FpbtB",  # Restoring American Seafood Competitiveness
    "14281": "fldCHRKSTiGUNf8c6",  # Restoring Equality of Opportunity and Meritocracy
    "14294": "fld7B4R3woLsHCPXy",  # Fighting Overcriminalization in Federal Regulations
    "14332": "fldrTKWzMx22MDHXm",  # Improving Oversight of Federal Grantmaking
    "14335": "fldKsJ0LnLzX5Imz9",  # Enabling Competition in the Commercial Space Industry
}

# Checkbox fields that are NOT EO flags (so the guard doesn't flag them as
# "unmapped EO checkbox"). fldAHYCrKc1noJlqQ = Cites Loper Bright.
KNOWN_NON_EO_CHECKBOXES = {"fldAHYCrKc1noJlqQ"}

# EO reference (names + signing dates) from the authoritative EO table.
EO_INFO = {
    "14154": {"name": "Unleashing American Energy", "date": "2025-01-20"},
    "14192": {"name": "Unleashing Prosperity Through Deregulation", "date": "2025-01-31"},
    "14215": {"name": "Ensuring Accountability for All Agencies", "date": "2025-02-18"},
    "14219": {"name": "Ensuring Lawful Governance & Implementing the DOGE Deregulatory Initiative", "date": "2025-02-19"},
    "14222": {"name": "Implementing the President's DOGE Cost Efficiency Initiative", "date": "2025-02-26"},
    "14238": {"name": "Continuing the Reduction of Federal Bureaucracy", "date": "2025-03-14"},
    "14267": {"name": "Reducing Anti-Competitive Regulatory Barriers", "date": "2025-04-09"},
    "14276": {"name": "Restoring American Seafood Competitiveness", "date": "2025-04-17"},
    "14281": {"name": "Restoring Equality of Opportunity and Meritocracy", "date": "2025-04-23"},
    "14294": {"name": "Fighting Overcriminalization in Federal Regulations", "date": "2025-05-09"},
    "14332": {"name": "Improving Oversight of Federal Grantmaking", "date": "2025-08-07"},
    "14335": {"name": "Enabling Competition in the Commercial Space Industry", "date": "2025-08-13"},
    "14356": {"name": "Ensuring Continued Accountability in Federal Hiring", "date": "2025-10-15"},
}

TOKEN = os.environ.get("AIRTABLE_TOKEN", "").strip()
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def log(msg):
    print(msg, flush=True)


def die(code, msg):
    log("\n" + "=" * 60)
    log("BUILD ABORTED -- output NOT written; last good build stays live.")
    log(msg)
    log("=" * 60)
    sys.exit(code)


# =============================================================================
# 1. FETCH
# =============================================================================
def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_schema():
    """Return {fieldId: {'name':.., 'type':..}} for the tracker table, or None
    if the metadata scope isn't granted (guard then degrades to a soft check)."""
    try:
        data = _get(f"{API}/meta/bases/{BASE_ID}/tables")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log("WARN  schema.bases:read not granted -- skipping strict schema "
                "validation. (Recommend adding the scope to the token.)")
            return None
        die(3, f"Metadata fetch failed: HTTP {e.code} {e.reason}")
    except Exception as e:
        die(3, f"Metadata fetch failed: {e}")
    for t in data.get("tables", []):
        if t.get("id") == TABLE_ID:
            return {f["id"]: {"name": f.get("name", ""), "type": f.get("type", "")}
                    for f in t.get("fields", [])}
    die(2, f"Table {TABLE_ID} not found in base {BASE_ID}.")


def fetch_records():
    """Fetch all rows, keyed by FIELD ID (returnFieldsByFieldId=true)."""
    records, offset = [], None
    while True:
        url = (f"{API}/{BASE_ID}/{TABLE_ID}"
               f"?pageSize=100&returnFieldsByFieldId=true")
        if offset:
            url += f"&offset={urllib.parse.quote(offset)}"
        try:
            data = _get(url)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                die(3, "Auth failed (HTTP %d). Check AIRTABLE_TOKEN and that it "
                       "has data.records:read on this base." % e.code)
            die(3, f"Record fetch failed: HTTP {e.code} {e.reason}")
        except Exception as e:
            die(3, f"Record fetch failed: {e}")
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    log(f"Fetched {len(records)} records from Airtable.")
    return records


# =============================================================================
# 2. GUARD  --  validate the table's shape against the contract
# =============================================================================
def validate(schema):
    """Abort the build (exit 2) if the table no longer matches the contract.
    Warn (but proceed) if a new, unmapped checkbox appears -- likely a new EO
    that needs wiring into EO_CHECKBOX."""
    if schema is None:
        return  # metadata scope absent; soft mode
    errors, warnings = [], []

    # a) every core field must still exist with the expected type
    for fid, (label, want_type) in CORE_FIELDS.items():
        if fid not in schema:
            errors.append(f"core field '{label}' [{fid}] is MISSING from the table")
        elif want_type and schema[fid]["type"] != want_type:
            errors.append(f"core field '{label}' [{fid}] changed type: "
                          f"expected {want_type}, found {schema[fid]['type']}")

    # b) every mapped EO checkbox must still exist and still be a checkbox
    for eo, fid in EO_CHECKBOX.items():
        if fid not in schema:
            errors.append(f"EO {eo} checkbox [{fid}] is MISSING from the table")
        elif schema[fid]["type"] != "checkbox":
            errors.append(f"EO {eo} checkbox [{fid}] is no longer a checkbox "
                          f"(now {schema[fid]['type']})")

    # c) any checkbox in the table that we DON'T know about -> warn (new EO?)
    known = set(EO_CHECKBOX.values()) | KNOWN_NON_EO_CHECKBOXES
    for fid, meta in schema.items():
        if meta["type"] == "checkbox" and fid not in known:
            warnings.append(f"unmapped checkbox '{meta['name']}' [{fid}] "
                            f"-- if this is a new EO, add it to EO_CHECKBOX")

    for w in warnings:
        log(f"WARN  {w}")
    if errors:
        die(2, "Schema validation failed:\n  - " + "\n  - ".join(errors))
    log("Schema OK: all core fields and %d EO checkboxes present and correctly "
        "typed." % len(EO_CHECKBOX))


# =============================================================================
# 3. TRANSFORM  --  reshape rows; checkbox wins for major EOs, tags add minors
# =============================================================================
def _sel(v):
    """Single-select value -> option name. Handles REST (string) and MCP (dict)."""
    if isinstance(v, dict):
        return v.get("name")
    return v if isinstance(v, str) else None


def _multi(v):
    """Multi-select -> list of names. Handles REST (list of str) and MCP (list of dict)."""
    if isinstance(v, list):
        return [x.get("name") if isinstance(x, dict) else x for x in v if x]
    return []


def _txt(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _url(v):
    if isinstance(v, dict):
        return v.get("url")
    return v if isinstance(v, str) else None


def clean_records(raw):
    F = {fid: label for fid, (label, _) in CORE_FIELDS.items()}
    tag_only = set(EO_INFO) - set(EO_CHECKBOX)  # 14222, 14238, 14356
    clean = []
    for r in raw:
        f = r.get("fields", {})
        title = _txt(f.get("fldRpFlrZUO40mVxE"))
        if not title:
            continue
        # EO set: checked boxes (authoritative for majors) UNION tag-field EOs
        # that are tag-only. A stray tag for a MAJOR eo does not add it -- the
        # checkbox is the source of truth for those. (Agreed rule #3.)
        eos = set(eo for eo, fid in EO_CHECKBOX.items() if f.get(fid) is True)
        for t in _multi(f.get("fldX6IeppsTs62iC6")):
            if t in tag_only:
                eos.add(t)
        ip = _sel(f.get("fldvcEFyep5EKYhhE"))
        di = _sel(f.get("fldTo9mHipkxesPw3"))
        clean.append({
            "id": r.get("id"),
            "title": title,
            "agency": _sel(f.get("fldCJYupgPD0Jf9aV")),
            "subAgency": _sel(f.get("fldsXXljpYgXCnyGe")),
            "ruleType": _sel(f.get("fldCbEJizbYcaRcYy")),
            "deregImpact": di,
            "impactPotential": ip,
            "policyArea": _sel(f.get("fldpVtSmPDOSDJ5Q2")),
            "datePublished": (_txt(f.get("fldfseGX1D0EHjQAX")) or "")[:10] or None,
            "deadlineDate": (_txt(f.get("fldpcO04XenqvYh7J")) or "")[:10] or None,
            "url": _url(f.get("fldO7Ad6cg4wUe8LK")),
            "citation": _txt(f.get("fldOk5Vfsk3xfA5pQ")),
            "description": _txt(f.get("fldJN9MVwC2gE9p3Z")),
            "eoNumbers": sorted(eos),
            "citesLoper": f.get("fldAHYCrKc1noJlqQ") is True,
            "highHigh": (ip == "High" and di == "High"),
        })
    log(f"Transformed {len(clean)} rows with a title.")
    return clean


# =============================================================================
# 4. METADATA  --  all the derived stats the report renders
# =============================================================================
def compute_meta(clean):
    total = len(clean)
    high = [r for r in clean if r["impactPotential"] == "High"]
    area_total = Counter(r["policyArea"] for r in clean if r["policyArea"])
    area_high = Counter(r["policyArea"] for r in high if r["policyArea"])
    agency_high = Counter(r["agency"] for r in high if r["agency"])
    dhigh = [r for r in clean if r["deregImpact"] == "High"]
    hh = [r for r in clean if r["highHigh"]]

    eo_counts, eo_high, eo_hh = Counter(), Counter(), Counter()
    for r in clean:
        for e in r["eoNumbers"]:
            eo_counts[e] += 1
            if r["impactPotential"] == "High":
                eo_high[e] += 1
            if r["highHigh"]:
                eo_hh[e] += 1

    loper = [r for r in clean if r["citesLoper"]]
    e192 = [r for r in clean if "14192" in r["eoNumbers"]]
    excl192 = [r for r in e192 if len(r["eoNumbers"]) == 1]
    hh192 = [r for r in e192 if r["highHigh"]]
    excl_hh192 = [r for r in excl192 if r["highHigh"]]

    month = Counter()
    for r in clean:
        d = r["datePublished"]
        if d and len(d) == 10 and d.startswith("20") and "2025-01" <= d[:7] <= "2026-12":
            month[d[:7]] += 1
    timeline = [(m, month[m]) for m in sorted(month)]
    types = Counter(r["ruleType"] for r in clean if r["ruleType"])

    return {
        "total": total,
        "finals": types.get("Final Rule", 0),
        "nprm": types.get("Notice of Proposed Rule Making", 0),
        "high_potential": len(high),
        "high_dereg": len(dhigh),
        "highhigh": len(hh),
        "agencies_count": len(set(r["agency"] for r in clean if r["agency"])),
        "agencies": sorted(set(r["agency"] for r in clean if r["agency"])),
        "policy_areas": sorted(area_total.keys()),
        "area_high": area_high.most_common(),
        "area_total": dict(area_total),
        "agency_high": agency_high.most_common(12),
        "eo_counts": eo_counts.most_common(),
        "eo_high": dict(eo_high),
        "eo_hh": dict(eo_hh),
        "eo_info": EO_INFO,
        "eos_by_count": [e for e, _ in eo_counts.most_common()],
        "eos_sorted": sorted(eo_counts.keys()),
        "timeline": timeline,
        "type_counts": types.most_common(),
        "loper_total": len(loper),
        "loper_high": sum(1 for r in loper if r["impactPotential"] == "High"),
        "loper_hh": sum(1 for r in loper if r["highHigh"]),
        "loper_by_agency": Counter(r["agency"] for r in loper if r["agency"]).most_common(10),
        "loper_by_area": Counter(r["policyArea"] for r in loper if r["policyArea"]).most_common(),
        "e192_total": len(e192),
        "e192_excl": len(excl192),
        "e192_hh": len(hh192),
        "e192_excl_hh": len(excl_hh192),
        "e192_high": sum(1 for r in e192 if r["impactPotential"] == "High"),
        "hh_area": Counter(r["policyArea"] for r in hh if r["policyArea"]).most_common(),
        "today": date.today().isoformat(),
    }


# =============================================================================
# 5. ASSETS  --  base64 the fonts + logo committed under assets/
# =============================================================================
def _b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def load_assets():
    need = {
        "logo_white": "logo_white.png",
        "logo_teal": "logo_teal.png",
        "ch400": "cooper-hewitt-400.woff2",
        "ch600": "cooper-hewitt-600.woff2",
        "ch700": "cooper-hewitt-700.woff2",
        "ch800": "cooper-hewitt-800.woff2",
    }
    out = {}
    for key, fname in need.items():
        p = os.path.join(ASSET_DIR, fname)
        if not os.path.exists(p):
            die(3, f"Missing asset: {p}. Commit the assets/ folder to the repo.")
        out[key] = _b64(p)
    return out


# =============================================================================
# 6. RENDER + MAIN  (render_html defined in the template section below)
# =============================================================================
def main():
    if not TOKEN:
        die(3, "AIRTABLE_TOKEN not set. Export a read-only token first.")
    out_path = sys.argv[1] if len(sys.argv) > 1 else "index.html"

    schema = fetch_schema()
    validate(schema)
    raw = fetch_records()
    records = clean_records(raw)
    if len(records) < 100:
        # sanity floor: the tracker has >1,600 rows; a tiny count means a bad
        # fetch, not a real drop. Refuse to overwrite a good build with garbage.
        die(2, f"Only {len(records)} rows returned -- refusing to publish "
               "(expected >1,600). Likely a fetch problem.")
    meta = compute_meta(records)
    assets = load_assets()

    html = render_html(records, meta, assets)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    log(f"\nOK  wrote {out_path}  ({len(html):,} bytes)")
    log(f"    {meta['total']:,} actions | EO 14192: {meta['e192_total']:,} "
        f"| high/high: {meta['highhigh']} | cite Loper: {meta['loper_total']}")


# ---------------------------------------------------------------------------
# render_html(records, meta, assets) -> str
# (Template ported verbatim from the verified interactive build.)
# ---------------------------------------------------------------------------
def render_html(records, meta, assets):
    logo_white = assets["logo_white"]; logo_teal = assets["logo_teal"]
    ch400 = assets["ch400"]; ch600 = assets["ch600"]
    ch700 = assets["ch700"]; ch800 = assets["ch800"]
    data_json = json.dumps(records, ensure_ascii=False)
    esc = lambda s: str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    # ---- Analysis bars: High impact-potential by policy area ----
    area_high = meta['area_high']
    area_total = meta['area_total']
    maxv = area_high[0][1]
    area_high_bars = ""
    for name, n in area_high:
        tot = area_total.get(name, n)
        pct_of_area = n/tot*100 if tot else 0
        barpct = n/maxv*100
        area_high_bars += f'''      <div class="hbar" data-area="{esc(name)}" title="{n} of {tot} actions in {esc(name)} rated High impact potential">
            <div class="hbar-label">{esc(name)}</div>
            <div class="hbar-track"><div class="hbar-fill" style="width:{barpct:.1f}%"></div></div>
            <div class="hbar-num">{n}<span class="hbar-pct">{pct_of_area:.0f}%</span></div>
          </div>
    '''

    # ---- Agency high-impact bars ----
    agency_high = meta['agency_high']
    amax = agency_high[0][1]
    agency_high_bars = ""
    for name, n in agency_high:
        short = name.replace("Comm'n","Commission").replace("Admin.","Administration").replace("Nat'l","National").replace("Environmental Protection Agency","EPA").replace("Health & Human Services","HHS")
        agency_high_bars += f'''      <div class="hbar" data-agency="{esc(name)}">
            <div class="hbar-label">{esc(short)}</div>
            <div class="hbar-track"><div class="hbar-fill alt" style="width:{n/amax*100:.1f}%"></div></div>
            <div class="hbar-num">{n}</div>
          </div>
    '''

    # ---- EO cards ---- (checkbox-based counts, sorted by volume; 14192 leads)
    eo_counts = dict(meta['eo_counts'])
    eo_high = meta['eo_high']
    eo_hh = meta['eo_hh']
    eo_info = meta['eo_info']
    def eo_date_txt(e):
        ed = eo_info.get(e,{}).get('date','')
        if not ed: return ''
        y,m,dy = ed.split('-'); M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        return f"{M[int(m)-1]} {int(dy)}, {y}"
    eo_cards = ""
    for e in meta['eos_by_count'][:6]:
        n = eo_counts[e]
        info = eo_info.get(e, {})
        flag = ' eo-flagship' if e=='14192' else ''
        eo_cards += f'''      <div class="eo-card{flag}" data-eo="{e}">
            <div class="eo-num">EO {e}</div>
            <div class="eo-date-sm">{eo_date_txt(e)}</div>
            <div class="eo-name">{esc(info.get('name','Executive Order'))}</div>
            <div class="eo-stat"><b>{n:,}</b> actions <span class="eo-hi">· {eo_hh.get(e,0)} high/high</span></div>
          </div>
    '''

    # ---- Loper-citing rules: top rules list + breakdown bars ----
    loper_area = meta['loper_by_area']
    lmax = loper_area[0][1] if loper_area else 1
    loper_area_bars = ""
    for name, n in loper_area:
        loper_area_bars += f'''        <div class="lbar" data-area="{esc(name)}">
              <div class="lbar-label">{esc(name)}</div>
              <div class="lbar-track"><div class="lbar-fill" style="width:{n/lmax*100:.1f}%"></div></div>
              <div class="lbar-num">{n}</div>
            </div>
    '''
    loper_agency = meta['loper_by_agency']
    lamax = loper_agency[0][1] if loper_agency else 1
    loper_agency_bars = ""
    for name, n in loper_agency:
        short = name.replace("Comm'n","Commission").replace("Environmental Protection Agency","EPA").replace("Health & Human Services","HHS").replace("Nat'l","National")
        loper_agency_bars += f'''        <div class="lbar" data-agency="{esc(name)}">
              <div class="lbar-label">{esc(short)}</div>
              <div class="lbar-track"><div class="lbar-fill alt" style="width:{n/lamax*100:.1f}%"></div></div>
              <div class="lbar-num">{n}</div>
            </div>
    '''

    # ---- Timeline sparkline (SVG) ----
    tl = meta['timeline']
    tl_max = max(v for _,v in tl) if tl else 1
    W, H, PAD = 900, 200, 8
    n = len(tl)
    bw = (W - PAD*2) / n
    tl_bars = ""
    tl_labels = ""
    for i,(m,v) in enumerate(tl):
        bh = (v/tl_max)*(H-40)
        x = PAD + i*bw
        y = H-20-bh
        tl_bars += f'<rect x="{x+bw*0.12:.1f}" y="{y:.1f}" width="{bw*0.76:.1f}" height="{bh:.1f}" rx="2" fill="url(#tlgrad)"><title>{m}: {v} actions</title></rect>'
        if m.endswith('-01') or m.endswith('-07'):
            yr = m[:4]; mo = m[5:7]
            lab = {'01':'Jan','07':'Jul'}[mo] + " '" + yr[2:]
            tl_labels += f'<text x="{x+bw/2:.1f}" y="{H-4}" text-anchor="middle" class="tl-lab">{lab}</text>'

    # ---- Rule type distribution stacked bar ----
    types = meta['type_counts']
    tc = {k:v for k,v in types}
    total = meta['total']
    type_order = [('Final Rule','#00788c'),('Notice of Proposed Rule Making','#3a9db0'),('Direct Final Rule','#6bbcc9'),
                  ('Other Notice','#89d2d9'),('Interim/Interim Final Rule','#b5a642'),('Temporary Rule','#d57a28'),
                  ('Amended Rule','#758592'),('Withdrawal','#0c1f2c')]
    type_seg = ""; type_leg = ""
    for name,color in type_order:
        v = tc.get(name,0)
        if not v: continue
        pct = v/total*100
        label = name.replace('Notice of Proposed Rule Making','NPRM').replace('Interim/Interim Final Rule','Interim')
        type_seg += f'<div class="tseg" style="width:{pct:.2f}%;background:{color}" title="{esc(name)}: {v}"></div>'
        type_leg += f'<div class="tleg-item"><span class="tleg-sw" style="background:{color}"></span>{esc(label)} <b>{v}</b></div>'

    # Fonts
    font_css = f"""
    @font-face{{font-family:'Cooper Hewitt';src:url(data:font/woff2;base64,{ch400}) format('woff2');font-weight:400;font-display:swap}}
    @font-face{{font-family:'Cooper Hewitt';src:url(data:font/woff2;base64,{ch600}) format('woff2');font-weight:600;font-display:swap}}
    @font-face{{font-family:'Cooper Hewitt';src:url(data:font/woff2;base64,{ch700}) format('woff2');font-weight:700;font-display:swap}}
    @font-face{{font-family:'Cooper Hewitt';src:url(data:font/woff2;base64,{ch800}) format('woff2');font-weight:800;font-display:swap}}
    """

    # select options
    agency_opts = "\n".join(f'        <option>{esc(a)}</option>' for a in meta['agencies'])
    area_opts = "\n".join(f'        <option>{esc(a)}</option>' for a in meta['policy_areas'])
    eo_opts = "\n".join(f'        <option value="{e}">EO {e}: {esc(eo_info.get(e,{}).get("name",""))} ({eo_counts[e]:,})</option>' for e in meta['eos_sorted'])

    top_area = meta['area_high'][0]
    html = f'''<!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Recasting Regulations: Tracking Deregulation After Loper Bright | AFP Foundation</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
    {font_css}
    :root {{
      /* AFP-F blue/teal family (from AFP Style Guide, Mar 2023) */
      --ocean: #004750;      /* Ocean */
      --ocean-deep: #003views; 
      --harbor: #00788c;     /* Harbor */
      --sky: #89d2d9;        /* Sky */
      --sky-soft: #d4edf0;
      --midnight: #0c1f2c;   /* Midnight */
      --steel: #758592;      /* Steel */
      --mist: #d8dfe1;       /* Mist */
      --goldenrod: #febd3d;  /* Goldenrod */
      --copper: #d57a28;     /* Copper */
      --paper: #ffffff;
      --canvas: #f2f6f7;
      --canvas2: #e7eef0;
      --ink: #0c1f2c;
      --ink-soft: #45525c;
      --ink-faint: #7c8a93;
      --line: #d9e3e6;
      --line-soft: #e9eff1;
      --high: #c0392b;
      --high-bg: #fbe7e4;
      --med: #b8860b;
      --med-bg: #faf0d8;
      --low: #5a8a93;
      --low-bg: #e4f0f2;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    html{{scroll-behavior:smooth}}
    body{{background:var(--canvas);color:var(--ink);font-family:'Inter',-apple-system,sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}}
    .serif{{font-family:'Source Serif 4',Georgia,serif}}
    h1,h2,h3,.display{{font-family:'Cooper Hewitt','Inter',sans-serif}}

    /* TOPBAR */
    .topbar{{background:var(--ocean);color:#fff;border-bottom:3px solid var(--sky)}}
    .topbar-inner{{max-width:1360px;margin:0 auto;padding:0 32px;height:66px;display:flex;align-items:center;justify-content:space-between}}
    .brand{{display:flex;align-items:center;gap:13px}}
    .brand img{{height:40px;width:auto;display:block}}
    .brand-text{{display:flex;flex-direction:column;line-height:1.12}}
    .brand-org{{font-family:'Cooper Hewitt';font-weight:800;font-size:15px;letter-spacing:0.01em}}
    .brand-sub{{font-size:10.5px;color:var(--sky);letter-spacing:0.03em;text-transform:uppercase}}
    .topbar-nav{{display:flex;gap:26px;align-items:center}}
    .topbar-nav a{{color:#bfe2e6;text-decoration:none;font-size:13px;font-weight:500;transition:color .15s}}
    .topbar-nav a:hover{{color:#fff}}
    .topbar-cta{{background:var(--sky);color:var(--ocean);padding:8px 16px;border-radius:6px;font-size:13px;font-weight:700;text-decoration:none}}
    .topbar-cta:hover{{background:#fff}}

    /* HERO */
    .hero{{background:linear-gradient(155deg,var(--ocean) 0%,var(--midnight) 100%);color:#fff;position:relative;overflow:hidden}}
    .hero::before{{content:'';position:absolute;inset:0;background:radial-gradient(circle at 82% 25%,rgba(137,210,217,0.16),transparent 55%);pointer-events:none}}
    .hero::after{{content:'';position:absolute;right:-80px;top:-40px;width:420px;height:420px;background:url(data:image/png;base64,{logo_teal}) no-repeat center/contain;opacity:0.06;pointer-events:none}}
    .hero-inner{{max-width:1360px;margin:0 auto;padding:60px 32px 52px;position:relative}}
    .eyebrow{{display:inline-flex;align-items:center;gap:9px;font-family:'Cooper Hewitt';font-size:12px;font-weight:700;letter-spacing:0.16em;text-transform:uppercase;color:var(--sky);margin-bottom:20px}}
    .eyebrow::before{{content:'';width:26px;height:2px;background:var(--sky)}}
    .hero h1{{font-weight:800;font-size:clamp(34px,5vw,62px);line-height:1.0;letter-spacing:-0.02em;max-width:15ch;margin-bottom:22px}}
    .hero h1 .accent{{color:var(--sky)}}
    .hero-lede{{font-family:'Source Serif 4',serif;font-size:18px;line-height:1.6;color:#cfe5e8;max-width:60ch}}
    .hero-meta{{margin-top:26px;font-size:13px;color:#8fb5bb;display:flex;gap:18px;flex-wrap:wrap;align-items:center}}
    .hero-meta strong{{color:#fff;font-weight:600}}
    .live-dot{{display:inline-flex;align-items:center;gap:7px}}
    .live-dot::before{{content:'';width:8px;height:8px;border-radius:50%;background:#4fd18a;box-shadow:0 0 0 0 rgba(79,209,138,.6);animation:pulse 2s infinite}}
    @keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(79,209,138,.6)}}70%{{box-shadow:0 0 0 8px rgba(79,209,138,0)}}100%{{box-shadow:0 0 0 0 rgba(79,209,138,0)}}}}

    /* STATS */
    .stats{{max-width:1360px;margin:-34px auto 0;padding:0 32px;position:relative;z-index:5}}
    .stats-grid{{background:var(--paper);border:1px solid var(--line);border-radius:14px;box-shadow:0 14px 44px rgba(0,71,80,.12);display:grid;grid-template-columns:repeat(5,1fr)}}
    .stat{{padding:24px 26px;border-right:1px solid var(--line-soft)}}
    .stat:last-child{{border-right:none}}
    .stat-num{{font-family:'Cooper Hewitt';font-weight:800;font-size:40px;letter-spacing:-0.02em;color:var(--ocean);line-height:1}}
    .stat-label{{font-size:11px;text-transform:uppercase;letter-spacing:0.09em;font-weight:600;color:var(--ink-faint);margin-top:9px}}
    .stat-note{{font-size:12px;color:var(--harbor);margin-top:4px;font-weight:500}}

    .wrap{{max-width:1360px;margin:0 auto;padding:0 32px}}
    .section{{padding:52px 0 8px}}
    .section-head{{margin-bottom:26px;max-width:74ch}}
    .section-kicker{{font-family:'Cooper Hewitt';font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;color:var(--harbor);margin-bottom:9px}}
    .section-title{{font-weight:800;font-size:28px;letter-spacing:-0.02em;color:var(--ocean);line-height:1.1}}
    .section-desc{{font-family:'Source Serif 4',serif;color:var(--ink-soft);font-size:15.5px;margin-top:10px;line-height:1.6}}

    /* KEY FINDING callout */
    .finding{{background:linear-gradient(120deg,var(--ocean),var(--harbor));color:#fff;border-radius:14px;padding:30px 34px;margin-bottom:28px;display:flex;gap:34px;align-items:center;flex-wrap:wrap}}
    .finding-big{{font-family:'Cooper Hewitt';font-weight:800;font-size:58px;line-height:0.95;color:var(--sky)}}
    .finding-big small{{display:block;font-size:14px;color:#cfe5e8;font-weight:600;letter-spacing:0.03em;margin-top:6px;font-family:'Inter'}}
    .finding-text{{flex:1;min-width:260px}}
    .finding-text h3{{font-size:20px;font-weight:700;margin-bottom:8px}}
    .finding-text p{{font-family:'Source Serif 4',serif;font-size:15px;color:#dceef0;line-height:1.6}}

    /* CARDS + CHARTS */
    .grid2{{display:grid;grid-template-columns:1.15fr 1fr;gap:24px}}
    .card{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:24px 26px}}
    .card-title{{font-family:'Cooper Hewitt';font-weight:700;font-size:16px;color:var(--ocean);margin-bottom:3px}}
    .card-sub{{font-size:12.5px;color:var(--ink-faint);margin-bottom:20px}}
    .hbar{{display:grid;grid-template-columns:150px 1fr 62px;align-items:center;gap:13px;margin-bottom:11px;cursor:pointer;padding:3px 5px;border-radius:6px;transition:background .12s}}
    .hbar:hover{{background:var(--canvas)}}
    .hbar-label{{font-size:12.5px;color:var(--ink-soft);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .hbar-track{{background:var(--canvas2);border-radius:5px;height:22px;overflow:hidden}}
    .hbar-fill{{height:100%;background:linear-gradient(90deg,var(--harbor),var(--sky));border-radius:5px;transition:width .9s cubic-bezier(.2,.8,.2,1)}}
    .hbar-fill.alt{{background:linear-gradient(90deg,var(--ocean),var(--harbor))}}
    .hbar-num{{font-family:'Cooper Hewitt';font-weight:700;font-size:14px;color:var(--ocean);text-align:right;white-space:nowrap}}
    .hbar-pct{{font-family:'Inter';font-weight:500;font-size:10.5px;color:var(--ink-faint);margin-left:5px}}

    /* EO cards */
    .eo-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:4px}}
    .eo-card{{background:var(--paper);border:1px solid var(--line);border-left:4px solid var(--harbor);border-radius:9px;padding:16px 18px;cursor:pointer;transition:all .14s}}
    .eo-card:hover{{border-left-color:var(--sky);box-shadow:0 6px 18px rgba(0,71,80,.1);transform:translateY(-2px)}}
    .eo-num{{font-family:'Cooper Hewitt';font-weight:800;font-size:19px;color:var(--ocean)}}
    .eo-date-sm{{font-size:11px;color:var(--ink-faint);margin-top:1px}}
    .eo-name{{font-size:12.5px;color:var(--ink-soft);margin:6px 0 10px;line-height:1.35;min-height:34px}}
    .eo-stat{{font-size:12px;color:var(--ink-faint)}}
    .eo-stat b{{color:var(--harbor);font-family:'Cooper Hewitt';font-size:14px}}
    .eo-hi{{color:var(--copper)}}
    .eo-card.eo-flagship{{border-left-color:var(--goldenrod);background:linear-gradient(120deg,#fffaf0,#fff);grid-column:1/-1}}
    .eo-card.eo-flagship .eo-num{{font-size:22px}}
    .eo-flag-tag{{display:inline-block;font-size:10px;font-weight:700;background:var(--goldenrod);color:var(--midnight);padding:2px 8px;border-radius:4px;margin-left:8px;letter-spacing:0.03em;vertical-align:middle}}

    /* 14192 spotlight */
    .spotlight{{background:var(--paper);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:8px}}
    .sl-top{{padding:30px 34px 26px;background:linear-gradient(120deg,var(--ocean),var(--harbor));color:#fff}}
    .sl-eo{{font-family:'Cooper Hewitt';font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:var(--sky)}}
    .sl-top h3{{font-family:'Cooper Hewitt';font-weight:800;font-size:24px;margin:6px 0 12px}}
    .sl-top p{{font-family:'Source Serif 4',serif;font-size:15px;line-height:1.65;color:#dceef0;max-width:80ch}}
    .sl-metrics{{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line)}}
    .sl-metric{{padding:22px 24px;border-right:1px solid var(--line-soft);cursor:pointer;transition:background .12s}}
    .sl-metric:last-child{{border-right:none}}
    .sl-metric:hover{{background:var(--canvas)}}
    .sl-metric-num{{font-family:'Cooper Hewitt';font-weight:800;font-size:34px;color:var(--ocean);line-height:1}}
    .sl-metric-label{{font-size:11.5px;color:var(--ink-soft);margin-top:8px;line-height:1.4}}
    .sl-metric-sub{{font-size:11px;color:var(--harbor);margin-top:3px;font-weight:600}}
    .sl-metric.hh .sl-metric-num{{color:var(--high)}}
    @media(max-width:760px){{.sl-metrics{{grid-template-columns:1fr 1fr}}.sl-metric:nth-child(2){{border-right:none}}}}

    /* high/high chip + row marker */
    .chip.hh-chip.active{{background:var(--high);border-color:var(--high)}}
    .hh-marker{{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:700;color:var(--high);background:var(--high-bg);padding:2px 7px;border-radius:4px;margin-top:4px}}

    /* LOPER-CITING section */
    .loper-cite{{background:linear-gradient(135deg,var(--ocean),var(--midnight));border-radius:14px;overflow:hidden;margin-top:8px;color:#fff}}
    .lc-head{{padding:30px 34px 24px;display:flex;gap:32px;align-items:center;flex-wrap:wrap;border-bottom:1px solid rgba(255,255,255,.1)}}
    .lc-big{{font-family:'Cooper Hewitt';font-weight:800;font-size:64px;line-height:0.9;color:var(--sky)}}
    .lc-big small{{display:block;font-family:'Inter';font-size:13px;font-weight:600;color:#cfe5e8;letter-spacing:0.03em;margin-top:8px}}
    .lc-head-text{{flex:1;min-width:280px}}
    .lc-head-text h3{{font-family:'Cooper Hewitt';font-size:21px;font-weight:700;margin-bottom:8px;color:#fff}}
    .lc-head-text p{{font-family:'Source Serif 4',serif;font-size:14.5px;line-height:1.6;color:#cfe5e8}}
    .lc-body{{display:grid;grid-template-columns:1fr 1fr;gap:0}}
    .lc-col{{padding:24px 34px 28px}}
    .lc-col:first-child{{border-right:1px solid rgba(255,255,255,.1)}}
    .lc-col h4{{font-family:'Cooper Hewitt';font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:var(--sky);margin-bottom:16px}}
    .lbar{{display:grid;grid-template-columns:130px 1fr 34px;align-items:center;gap:11px;margin-bottom:9px;cursor:pointer;padding:2px 4px;border-radius:5px;transition:background .12s}}
    .lbar:hover{{background:rgba(255,255,255,.06)}}
    .lbar-label{{font-size:12px;color:#dceef0;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
    .lbar-track{{background:rgba(255,255,255,.12);border-radius:5px;height:18px;overflow:hidden}}
    .lbar-fill{{height:100%;background:linear-gradient(90deg,var(--sky),#bfe8ec);border-radius:5px}}
    .lbar-fill.alt{{background:linear-gradient(90deg,#4a97a6,var(--sky))}}
    .lbar-num{{font-family:'Cooper Hewitt';font-weight:700;font-size:13px;color:#fff;text-align:right}}
    .lc-cta{{padding:20px 34px;background:rgba(0,0,0,.18);display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
    .lc-cta span{{font-family:'Source Serif 4',serif;font-size:14px;color:#cfe5e8}}
    .lc-btn{{background:var(--sky);color:var(--ocean);border:none;font-family:'Inter';font-size:13px;font-weight:700;padding:10px 18px;border-radius:8px;cursor:pointer;white-space:nowrap}}
    .lc-btn:hover{{background:#fff}}
    .loper-badge{{display:inline-block;font-size:10px;font-weight:700;background:var(--ocean);color:var(--sky);padding:2px 7px;border-radius:4px;margin-top:4px;letter-spacing:0.02em}}
    @media(max-width:760px){{.lc-body{{grid-template-columns:1fr}}.lc-col:first-child{{border-right:none;border-bottom:1px solid rgba(255,255,255,.1)}}}}


    /* Timeline */
    .tl-wrap{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:24px 26px;margin-top:24px}}
    .tl-svg{{width:100%;height:auto;display:block;margin-top:8px}}
    .tl-lab{{font-size:11px;fill:var(--ink-faint);font-family:'Inter'}}

    /* type distribution */
    .tdist{{margin-top:24px}}
    .tbar{{display:flex;height:34px;border-radius:8px;overflow:hidden;border:1px solid var(--line)}}
    .tseg{{transition:opacity .12s}}
    .tseg:hover{{opacity:0.82}}
    .tleg{{display:flex;flex-wrap:wrap;gap:9px 20px;margin-top:14px}}
    .tleg-item{{font-size:12px;color:var(--ink-soft);display:flex;align-items:center;gap:7px}}
    .tleg-sw{{width:11px;height:11px;border-radius:3px}}
    .tleg-item b{{color:var(--ocean);font-family:'Cooper Hewitt'}}

    /* TRACKER */
    .tracker-shell{{background:var(--paper);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:8px}}
    .tracker-toolbar{{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:var(--canvas)}}
    .search-wrap{{flex:1;min-width:240px;position:relative}}
    .search-wrap svg{{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:var(--ink-faint)}}
    .search-input{{width:100%;background:var(--paper);border:1px solid var(--line);color:var(--ink);font-size:14px;padding:10px 14px 10px 38px;border-radius:8px;font-family:'Inter'}}
    .search-input:focus{{outline:none;border-color:var(--harbor);box-shadow:0 0 0 3px rgba(0,120,140,.12)}}
    .select{{background:var(--paper);border:1px solid var(--line);color:var(--ink);font-size:13px;padding:9px 30px 9px 12px;border-radius:8px;appearance:none;cursor:pointer;font-family:'Inter';max-width:230px;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%237c8a93' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 11px center}}
    .select:focus{{outline:none;border-color:var(--harbor)}}
    .toolbar-row2{{padding:12px 22px;border-bottom:1px solid var(--line-soft);display:flex;gap:9px;align-items:center;flex-wrap:wrap;background:var(--paper)}}
    .tlabel{{font-size:11px;color:var(--ink-faint);font-weight:700;text-transform:uppercase;letter-spacing:0.06em}}
    .chip{{display:inline-flex;align-items:center;gap:6px;padding:6px 13px;border-radius:20px;font-size:12.5px;font-weight:500;cursor:pointer;border:1px solid var(--line);background:var(--paper);color:var(--ink-soft);transition:all .12s;user-select:none}}
    .chip:hover{{border-color:var(--harbor);color:var(--harbor)}}
    .chip.active{{background:var(--ocean);border-color:var(--ocean);color:#fff}}
    .chip .dot{{width:7px;height:7px;border-radius:50%}}
    .toolbar-spacer{{flex:1}}
    .result-meta{{font-size:13px;color:var(--ink-faint);white-space:nowrap}}
    .result-meta b{{color:var(--ocean);font-weight:700}}
    .btn-ghost{{background:none;border:1px solid var(--line);color:var(--ink-soft);font-size:12.5px;padding:7px 13px;border-radius:8px;cursor:pointer;font-family:'Inter'}}
    .btn-ghost:hover{{border-color:var(--high);color:var(--high)}}
    .btn-export{{background:var(--harbor);border:none;color:#fff;font-size:12.5px;font-weight:600;padding:8px 15px;border-radius:8px;cursor:pointer;font-family:'Inter';display:inline-flex;gap:7px;align-items:center}}
    .btn-export:hover{{background:var(--ocean)}}
    table{{width:100%;border-collapse:collapse}}
    thead th{{background:var(--canvas);text-align:left;padding:12px 16px;font-family:'Cooper Hewitt';font-size:11px;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--ink-faint);border-bottom:2px solid var(--line);cursor:pointer;white-space:nowrap;position:sticky;top:0;z-index:2}}
    thead th:hover{{color:var(--ocean)}}
    thead th.sorted{{color:var(--harbor)}}
    tbody tr{{border-bottom:1px solid var(--line-soft);transition:background .1s}}
    tbody tr:hover{{background:var(--sky-soft)}}
    tbody td{{padding:14px 16px;vertical-align:top;font-size:13.5px}}
    .rule-title{{font-weight:600;color:var(--ocean);line-height:1.4;margin-bottom:4px}}
    .rule-title a{{color:var(--ocean);text-decoration:none}}
    .rule-title a:hover{{color:var(--harbor);text-decoration:underline}}
    .rule-desc{{font-size:12.5px;color:var(--ink-soft);line-height:1.5;margin:5px 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
    .rule-meta{{font-size:11.5px;color:var(--ink-faint)}}
    .agency-name{{font-weight:500;color:var(--ink);font-size:13px}}
    .agency-sub{{font-size:11.5px;color:var(--ink-faint);margin-top:2px}}
    .badge{{display:inline-block;font-size:11px;font-weight:600;padding:3px 9px;border-radius:4px;white-space:nowrap}}
    .b-high{{color:var(--high);background:var(--high-bg)}}
    .b-medium{{color:var(--med);background:var(--med-bg)}}
    .b-low{{color:var(--low);background:var(--low-bg)}}
    .b-none{{color:var(--ink-faint)}}
    .tbadge{{display:inline-block;font-size:11px;font-weight:600;padding:3px 9px;border-radius:4px;white-space:nowrap}}
    .t-final{{color:#00606f;background:#dcf0f2}}
    .t-nprm{{color:#0a5561;background:#d0e9ec}}
    .t-dfr{{color:#2a7d5a;background:#e0f2e9}}
    .t-notice{{color:#5a6b74;background:#e9eff1}}
    .t-interim{{color:#8a6d0f;background:#f7edd3}}
    .t-temp{{color:#b5591f;background:#f9e6d8}}
    .t-amended{{color:#5a6b74;background:#eceff1}}
    .t-withdrawal{{color:#c0392b;background:#fbe7e4}}
    .eo-tag{{display:inline-block;font-size:10.5px;font-weight:600;background:var(--ocean);color:#fff;padding:2px 7px;border-radius:4px;margin:2px 3px 0 0}}
    .empty{{text-align:center;padding:70px 24px;color:var(--ink-faint)}}
    .pagination{{display:flex;align-items:center;justify-content:space-between;padding:16px 22px;border-top:1px solid var(--line);flex-wrap:wrap;gap:12px;background:var(--canvas)}}
    .page-info{{font-size:12.5px;color:var(--ink-faint)}}
    .page-btns{{display:flex;gap:5px}}
    .pbtn{{background:var(--paper);border:1px solid var(--line);color:var(--ink-soft);font-size:12.5px;padding:6px 11px;border-radius:6px;cursor:pointer;font-family:'Inter';min-width:34px}}
    .pbtn:hover:not(:disabled){{border-color:var(--harbor);color:var(--harbor)}}
    .pbtn.active{{background:var(--ocean);border-color:var(--ocean);color:#fff}}
    .pbtn:disabled{{opacity:.4;cursor:default}}

    footer{{background:var(--midnight);color:#aebfc5;margin-top:60px}}
    .footer-inner{{max-width:1360px;margin:0 auto;padding:44px 32px;display:flex;justify-content:space-between;gap:34px;flex-wrap:wrap}}
    .footer-brand{{max-width:46ch}}
    .footer-brand img{{height:44px;margin-bottom:14px}}
    .footer-brand p{{font-family:'Source Serif 4',serif;font-size:13.5px;line-height:1.65;color:#8ea3aa}}
    .footer-links{{display:flex;gap:52px}}
    .footer-col h4{{font-family:'Cooper Hewitt';font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#fff;margin-bottom:13px}}
    .footer-col a{{display:block;color:#8ea3aa;text-decoration:none;font-size:13px;margin-bottom:9px}}
    .footer-col a:hover{{color:var(--sky)}}
    .footer-bottom{{border-top:1px solid rgba(255,255,255,.09);padding:18px 32px;text-align:center;font-size:12px;color:#5f7178}}
    .methodology{{font-family:'Source Serif 4',serif;font-size:13.5px;color:var(--ink-soft);margin-top:24px;line-height:1.75;max-width:88ch;background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:22px 26px}}
    .methodology b{{color:var(--ocean);font-family:'Inter'}}
    .scoring-guide{{margin-top:14px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--paper)}}
    .scoring-guide summary{{cursor:pointer;padding:14px 22px;font-family:'Cooper Hewitt';font-weight:700;font-size:13px;color:var(--ocean);list-style:none;display:flex;align-items:center;gap:9px;user-select:none}}
    .scoring-guide summary::-webkit-details-marker{{display:none}}
    .scoring-guide summary::before{{content:'+';font-size:18px;line-height:1;color:var(--harbor);font-weight:700}}
    .scoring-guide[open] summary::before{{content:'\\2212'}}
    .scoring-guide summary:hover{{background:var(--canvas)}}
    .sg-body{{padding:4px 22px 20px}}
    .sg-dim{{font-family:'Cooper Hewitt';font-weight:700;font-size:13.5px;color:var(--ocean);margin:16px 0 8px;padding-top:14px;border-top:1px solid var(--line-soft)}}
    .sg-dim:first-child{{border-top:none;padding-top:2px}}
    .sg-row{{font-family:'Source Serif 4',serif;font-size:13.5px;line-height:1.6;color:var(--ink-soft);margin-bottom:8px}}
    .sg-tag{{display:inline-block;font-family:'Inter';font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;margin-right:8px;vertical-align:middle}}
    .sg-h{{color:var(--high);background:var(--high-bg)}}
    .sg-m{{color:var(--med);background:var(--med-bg)}}
    .sg-l{{color:var(--low);background:var(--low-bg)}}

    /* LOPER context band */
    .loper-band{{background:var(--paper);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-top:8px}}
    .loper-top{{padding:34px 38px 30px;background:linear-gradient(120deg,#eef6f7,#ffffff)}}
    .loper-lead{{display:grid;grid-template-columns:1.4fr 1fr;gap:38px;align-items:start}}
    .loper-lead p{{font-family:'Source Serif 4',serif;font-size:16px;line-height:1.72;color:var(--ink-soft)}}
    .loper-lead p+p{{margin-top:14px}}
    .loper-lead em{{color:var(--ocean);font-style:italic}}
    .loper-quote{{border-left:4px solid var(--sky);padding:6px 0 6px 22px}}
    .loper-quote blockquote{{font-family:'Source Serif 4',serif;font-style:italic;font-size:18px;line-height:1.5;color:var(--ocean)}}
    .loper-quote cite{{display:block;margin-top:12px;font-family:'Inter';font-style:normal;font-size:12px;color:var(--ink-faint);letter-spacing:0.02em}}
    .doctrine{{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid var(--line)}}
    .doctrine-col{{padding:26px 38px}}
    .doctrine-col:first-child{{border-right:1px solid var(--line);background:#fbfdfd}}
    .doctrine-tag{{display:inline-block;font-family:'Cooper Hewitt';font-size:11px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;padding:4px 10px;border-radius:5px;margin-bottom:12px}}
    .tag-before{{background:var(--canvas2);color:var(--steel)}}
    .tag-after{{background:var(--ocean);color:#fff}}
    .doctrine-col h4{{font-family:'Cooper Hewitt';font-size:17px;font-weight:700;color:var(--ocean);margin-bottom:8px}}
    .doctrine-col p{{font-family:'Source Serif 4',serif;font-size:14px;line-height:1.65;color:var(--ink-soft)}}

    /* timeline of the era */
    .era{{padding:30px 38px 34px;border-top:1px solid var(--line)}}
    .era h4{{font-family:'Cooper Hewitt';font-size:15px;font-weight:700;color:var(--ocean);margin-bottom:22px}}
    .era-track{{position:relative;display:flex;justify-content:space-between;gap:12px}}
    .era-track::before{{content:'';position:absolute;left:0;right:0;top:9px;height:2px;background:var(--line)}}
    .era-step{{position:relative;flex:1;text-align:center;padding-top:26px}}
    .era-step::before{{content:'';position:absolute;top:3px;left:50%;transform:translateX(-50%);width:15px;height:15px;border-radius:50%;background:#fff;border:3px solid var(--sky)}}
    .era-step.key::before{{background:var(--ocean);border-color:var(--ocean);width:17px;height:17px;top:2px}}
    .era-date{{font-family:'Cooper Hewitt';font-weight:700;font-size:14px;color:var(--ocean)}}
    .era-label{{font-size:12px;color:var(--ink-soft);margin-top:4px;line-height:1.4}}
    @media(max-width:760px){{.loper-lead{{grid-template-columns:1fr}}.doctrine{{grid-template-columns:1fr}}.doctrine-col:first-child{{border-right:none;border-bottom:1px solid var(--line)}}.era-track{{flex-direction:column;gap:0}}.era-track::before{{left:8px;right:auto;top:0;bottom:0;width:2px;height:auto}}.era-step{{text-align:left;padding:10px 0 10px 30px}}.era-step::before{{left:1px;top:12px;transform:none}}}}

    @media(max-width:980px){{.grid2{{grid-template-columns:1fr}}.eo-grid{{grid-template-columns:1fr 1fr}}.stats-grid{{grid-template-columns:repeat(2,1fr)}}.stat:nth-child(2){{border-right:none}}.topbar-nav{{display:none}}}}
    @media(max-width:640px){{.wrap,.hero-inner,.stats,.topbar-inner{{padding-left:18px;padding-right:18px}}.stats-grid{{grid-template-columns:1fr 1fr}}.hbar{{grid-template-columns:96px 1fr 52px}}.eo-grid{{grid-template-columns:1fr}}thead th:nth-child(5),tbody td:nth-child(5),thead th:nth-child(6),tbody td:nth-child(6){{display:none}}}}
    </style>
    </head>
    <body>

    <div class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <img src="data:image/png;base64,{logo_white}" alt="AFP Foundation">
          <div class="brand-text">
            <span class="brand-org">Recasting Regulations</span>
            <span class="brand-sub">Americans for Prosperity Foundation</span>
          </div>
        </div>
        <nav class="topbar-nav">
          <a href="#overview">Overview</a>
          <a href="#loper">Loper Bright</a>
          <a href="#cited">Rules Citing Loper</a>
          <a href="#findings">Key Findings</a>
          <a href="#analysis">Analysis</a>
          <a href="#tracker">Full Tracker</a>
          <a class="topbar-cta" href="https://americansforprosperityfoundation.org/subscribe-to-loper-bright-updates/" target="_blank" rel="noopener">Subscribe</a>
        </nav>
      </div>
    </div>

    <section class="hero" id="overview">
      <div class="hero-inner">
        <span class="eyebrow">The End of Chevron Deference · A Recasting Regulations Report</span>
        <h1>Recasting Regulations After <span class="accent"><em style="font-style:italic;font-weight:800">Loper Bright</em></span></h1>
        <p class="hero-lede">In June 2024, the Supreme Court ended forty years of <em>Chevron</em> deference. Courts, not agencies, now say what the law means. That single decision is reshaping the federal rulebook. This report follows the result, tracking every rule agencies have proposed, finalized, or rescinded in the following deregulatory wave. Each one is scored for its impact and is tied to the executive orders behind it. This data is updated daily from the Federal Register.</p>
        <div class="hero-meta">
          <span class="live-dot" id="asof">Data current as of <strong>{meta['today']}</strong></span>
          <span>·</span><span><strong>{meta['total']:,}</strong> regulatory actions</span>
          <span>·</span><span><strong>{meta['agencies_count']}</strong> federal agencies</span>
          <span>·</span><span><strong>{len(meta['eo_counts'])}</strong> executive orders tracked</span>
        </div>
      </div>
    </section>

    <div class="stats">
      <div class="stats-grid">
        <div class="stat"><div class="stat-num">{meta['total']:,}</div><div class="stat-label">Total Actions</div><div class="stat-note">All rule types</div></div>
        <div class="stat"><div class="stat-num">{meta['high_potential']:,}</div><div class="stat-label">High Impact Potential</div><div class="stat-note">{meta['high_potential']/meta['total']*100:.0f}% of all actions</div></div>
        <div class="stat"><div class="stat-num">{meta['finals']:,}</div><div class="stat-label">Final Rules</div><div class="stat-note">{meta['finals']/meta['total']*100:.0f}% enacted</div></div>
        <div class="stat"><div class="stat-num">{meta['highhigh']:,}</div><div class="stat-label">High / High Actions</div><div class="stat-note">high impact + high dereg</div></div>
        <div class="stat"><div class="stat-num">{meta['agencies_count']}</div><div class="stat-label">Agencies</div><div class="stat-note">Executive &amp; independent</div></div>
      </div>
    </div>

    <!-- LOPER BRIGHT CONTEXT -->
    <div class="wrap">
      <section class="section" id="loper">
        <div class="section-head">
          <div class="section-kicker">The Decision Behind the Data</div>
          <h2 class="section-title">What <em style="font-style:italic">Loper Bright</em> changed</h2>
          <p class="section-desc">Every action in this tracker sits downstream of the <em>Loper Bright</em> Supreme Court decision. It is key to reading the record.</p>
        </div>
        <div class="loper-band">
          <div class="loper-top">
            <div class="loper-lead">
              <div>
                <p>On <b>June 28, 2024</b>, in <em>Loper Bright Enterprises v. Raimondo</em>, the Supreme Court overruled <em>Chevron U.S.A. v. NRDC</em>. For four decades that 1984 precedent told courts to defer to an agency's reasonable interpretation whenever a statute was ambiguous. In a 6–3 decision, Chief Justice Roberts held that the Administrative Procedure Act requires courts to exercise their own independent judgment. Ambiguity alone no longer buys an agency deference.</p>
                <p>The consequence is direct. Hundreds of regulations once shielded by <em>Chevron</em> are now open to challenges on the statute's <em>single best reading</em>. Agencies have started revisiting and rescinding rules built on the old regime rather than waiting for a court to do it for them. The executive branch too has pushed that review forward through a series of deregulatory orders. The regulatory actions on this page represent the current progress made.</p>
              </div>
              <div class="loper-quote">
                <blockquote>"Courts must exercise their independent judgment in deciding whether an agency has acted within its statutory authority."</blockquote>
                <cite>Chief Justice John Roberts, majority opinion, <em>Loper Bright v. Raimondo</em> (2024)</cite>
              </div>
            </div>
          </div>
          <div class="doctrine">
            <div class="doctrine-col">
              <span class="doctrine-tag tag-before">1984 – 2024 · Under Chevron</span>
              <h4>Agencies filled the gaps</h4>
              <p>When a statute was ambiguous, courts deferred to any reasonable agency interpretation. That handed agencies wide latitude to expand their own authority, and made rules difficult to challenge so long as the reading was "permissible."</p>
            </div>
            <div class="doctrine-col">
              <span class="doctrine-tag tag-after">2024 – Present · After Loper Bright</span>
              <h4>Courts say what the law is</h4>
              <p>Judges now decide a statute's best meaning for themselves, giving the agency's view only as much weight as it textually deserves. Rules that leaned on expansive readings of ambiguous language are the most exposed, and agencies are recasting them before a court forces the issue.</p>
            </div>
          </div>
          <div class="era">
            <h4>How the era unfolded</h4>
            <div class="era-track">
              <div class="era-step"><div class="era-date">1984</div><div class="era-label"><em>Chevron</em> establishes agency deference</div></div>
              <div class="era-step key"><div class="era-date">Jun 2024</div><div class="era-label"><em>Loper Bright</em> overrules <em>Chevron</em></div></div>
              <div class="era-step"><div class="era-date">Jan 2025</div><div class="era-label">Deregulatory executive orders begin</div></div>
              <div class="era-step key"><div class="era-date">2025–26</div><div class="era-label">{meta['total']:,} regulatory actions tracked</div></div>
              <div class="era-step"><div class="era-date">Ongoing</div><div class="era-label">Rescissions &amp; court challenges continue</div></div>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- KEY FINDINGS -->
    <div class="wrap">
      <section class="section" id="findings">
        <div class="section-head">
          <div class="section-kicker">Key Findings</div>
          <h2 class="section-title">Where the rollback is landing</h2>
          <p class="section-desc">Each action is scored on two dimensions. <b>Impact Potential</b> measures how substantial the regulatory change is. <b>Impact of Deregulatory Action</b> measures how directly the deregulatory executive orders drove it. The findings below rank the policy areas seeing the most substantive change as agencies rewrite rules for the world after <em>Chevron</em>.</p>
        </div>

        <div class="finding">
          <div class="finding-big">{top_area[1]}<small>HIGH-IMPACT ACTIONS IN {top_area[0].upper()}</small></div>
          <div class="finding-text">
            <h3>{top_area[0]} leads the deregulatory agenda</h3>
            <p>Of {meta['area_total'][top_area[0]]:,} tracked actions in {top_area[0]}, {top_area[1]} carry high impact potential. That is the heaviest concentration of substantive change in any single policy area, and it is where the deregulatory agenda has moved fastest.</p>
          </div>
        </div>

        <div class="grid2">
          <div class="card">
            <div class="card-title">High-Impact Actions by Policy Area</div>
            <div class="card-sub">Count of actions rated <b>High</b> impact potential · share of area in gray · click to filter</div>
    {area_high_bars}      </div>
          <div class="card">
            <div class="card-title">High-Impact Actions by Agency</div>
            <div class="card-sub">Top 12 agencies by high-impact volume · click to filter</div>
    {agency_high_bars}      </div>
        </div>
      </section>
    </div>

    <!-- RULES CITING LOPER BRIGHT -->
    <div class="wrap">
      <section class="section" id="cited">
        <div class="section-head">
          <div class="section-kicker">Direct Citations</div>
          <h2 class="section-title">Rules that cite <em style="font-style:italic">Loper Bright</em> by name</h2>
          <p class="section-desc">Beyond the executive orders, agencies are invoking the decision itself. These are the actions in the tracker where the rulemaking record expressly relies on <em>Loper Bright</em> or the end of <em>Chevron</em> deference to justify its reading of the statute.</p>
        </div>
        <div class="loper-cite">
          <div class="lc-head">
            <div class="lc-big">{meta['loper_total']}<small>ACTIONS CITE LOPER BRIGHT</small></div>
            <div class="lc-head-text">
              <h3>The doctrine is doing work on the page</h3>
              <p>Of {meta['total']:,} tracked actions, {meta['loper_total']} invoke <em>Loper Bright</em> or the fall of <em>Chevron</em> directly in their reasoning, and {meta['loper_high']} of those carry high impact potential. The Environmental Protection Agency leads. It is using the decision to reopen air, water, and permitting rules that stood for decades on deference alone.</p>
            </div>
          </div>
          <div class="lc-body">
            <div class="lc-col">
              <h4>Citing Rules by Policy Area</h4>
    {loper_area_bars}        </div>
            <div class="lc-col">
              <h4>Citing Rules by Agency</h4>
    {loper_agency_bars}        </div>
          </div>
          <div class="lc-cta">
            <span>See every rule that cites the decision, filtered live in the tracker below.</span>
            <button class="lc-btn" onclick="showLoperCited()">View all {meta['loper_total']} citing rules →</button>
          </div>
        </div>
      </section>
    </div>

    <!-- ANALYSIS -->
    <div class="wrap">
      <section class="section" id="analysis">
        <div class="section-head">
          <div class="section-kicker">Analysis</div>
          <h2 class="section-title">The mechanics of the rollback</h2>
          <p class="section-desc"><em>Loper Bright</em> opened the legal door. The executive orders sent agencies through it. The patterns below show which orders, which agencies, and which months account for the bulk of the activity.</p>
        </div>

        <div class="spotlight">
          <div class="sl-top">
            <div class="sl-eo">The Flagship Order · EO 14192</div>
            <h3>Why one order sits behind almost everything</h3>
            <p><em>Unleashing Prosperity Through Deregulation</em> (Jan 31, 2025) is the government-wide "10-to-1" mandate. For every new rule, agencies have to find at least ten to repeal and keep the net cost of regulation below zero. It applies to the whole executive branch rather than a single sector, so almost every deregulatory action falls under it in some form. That is why it is flagged on <b style="color:var(--sky)">{meta['e192_total']:,}</b> of {meta['total']:,} tracked actions, far more than any sector-specific order. The metrics below separate out how much of that is 14192 acting on its own, and how much of it is high-stakes.</p>
          </div>
          <div class="sl-metrics">
            <div class="sl-metric" onclick="filterEO('14192')">
              <div class="sl-metric-num">{meta['e192_total']:,}</div>
              <div class="sl-metric-label">Total actions under EO 14192</div>
              <div class="sl-metric-sub">{meta['e192_total']/meta['total']*100:.0f}% of all tracked actions →</div>
            </div>
            <div class="sl-metric" onclick="filterEOExclusive()">
              <div class="sl-metric-num">{meta['e192_excl']:,}</div>
              <div class="sl-metric-label">Cite <b>only</b> EO 14192</div>
              <div class="sl-metric-sub">no other executive order →</div>
            </div>
            <div class="sl-metric hh" onclick="filterEOHH('14192')">
              <div class="sl-metric-num">{meta['e192_hh']}</div>
              <div class="sl-metric-label">EO 14192 <b>high / high</b> actions</div>
              <div class="sl-metric-sub">high impact + high dereg →</div>
            </div>
            <div class="sl-metric hh" onclick="filterEOExclusiveHH()">
              <div class="sl-metric-num">{meta['e192_excl_hh']}</div>
              <div class="sl-metric-label">Only 14192 <b>and</b> high / high</div>
              <div class="sl-metric-sub">the pure-14192 core →</div>
            </div>
          </div>
        </div>

        <div class="card" style="margin-top:24px">
          <div class="card-title">Executive Orders Driving the Agenda</div>
          <div class="card-sub">Actions flagged to each EO (a rule may fall under several) · high/high = high impact potential + high deregulatory impact · click to filter</div>
          <div class="eo-grid">
    {eo_cards}      </div>
        </div>

        <div class="tl-wrap">
          <div class="card-title">Regulatory Actions Over Time</div>
          <div class="card-sub">Actions by month published in the Federal Register</div>
          <svg class="tl-svg" viewBox="0 0 {W} {H}" preserveAspectRatio="none">
            <defs><linearGradient id="tlgrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#00788c"/><stop offset="1" stop-color="#89d2d9"/></linearGradient></defs>
            {tl_bars}{tl_labels}
          </svg>
        </div>

        <div class="card tdist">
          <div class="card-title">Composition by Rule Type</div>
          <div class="card-sub">{meta['total']:,} total actions</div>
          <div class="tbar">{type_seg}</div>
          <div class="tleg">{type_leg}</div>
        </div>
      </section>
    </div>

    <!-- TRACKER -->
    <div class="wrap">
      <section class="section" id="tracker">
        <div class="section-head">
          <div class="section-kicker">The Record</div>
          <h2 class="section-title">Full deregulation tracker</h2>
          <p class="section-desc">Search and filter every tracked action. Click a rule title to open the source document in the Federal Register.</p>
        </div>
        <div class="tracker-shell">
          <div class="tracker-toolbar">
            <div class="search-wrap">
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none"><circle cx="6.5" cy="6.5" r="5.5" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
              <input class="search-input" id="search" type="text" placeholder="Search rules, agencies, citations, descriptions…">
            </div>
            <select class="select" id="f-type"><option value="">All Rule Types</option><option>Final Rule</option><option>Notice of Proposed Rule Making</option><option>Direct Final Rule</option><option>Interim/Interim Final Rule</option><option>Other Notice</option><option>Temporary Rule</option><option>Amended Rule</option><option>Withdrawal</option></select>
            <select class="select" id="f-area"><option value="">All Policy Areas</option>
    {area_opts}
            </select>
            <select class="select" id="f-agency"><option value="">All Agencies</option>
    {agency_opts}
            </select>
            <select class="select" id="f-eo"><option value="">All Executive Orders</option>
    {eo_opts}
            </select>
          </div>
          <div class="toolbar-row2">
            <span class="tlabel">Impact Potential:</span>
            <div class="chip active" data-prio="" onclick="setPrio(this,'')">All</div>
            <div class="chip" data-prio="High" onclick="setPrio(this,'High')"><span class="dot" style="background:var(--high)"></span>High</div>
            <div class="chip" data-prio="Medium" onclick="setPrio(this,'Medium')"><span class="dot" style="background:var(--med)"></span>Medium</div>
            <div class="chip" data-prio="Low" onclick="setPrio(this,'Low')"><span class="dot" style="background:var(--low)"></span>Low</div>
            <span style="width:1px;height:22px;background:var(--line);margin:0 4px"></span>
            <div class="chip" id="loper-chip" onclick="toggleLoper(this)"><span class="dot" style="background:var(--ocean)"></span>Cites Loper Bright</div>
            <div class="chip hh-chip" id="hh-chip" onclick="toggleHH(this)"><span class="dot" style="background:var(--high)"></span>High / High</div>
            <div class="toolbar-spacer"></div>
            <span class="result-meta" id="result-meta"></span>
            <select class="select" id="sort" style="font-size:12px"><option value="date-desc">Newest First</option><option value="date-asc">Oldest First</option><option value="title-asc">Title A–Z</option><option value="agency-asc">Agency A–Z</option><option value="impact-desc">Impact Potential</option><option value="dereg-desc">Deregulatory Impact</option></select>
            <button class="btn-ghost" onclick="clearAll()">Clear</button>
            <button class="btn-export" onclick="exportCSV()"><svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M8 1v9m0 0l3-3m-3 3L5 7M2 12v2a1 1 0 001 1h10a1 1 0 001-1v-2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>Export CSV</button>
          </div>
          <div style="overflow-x:auto">
            <table>
              <thead><tr>
                <th data-sort="title" style="width:36%">Rule / Action</th>
                <th data-sort="agency" style="width:16%">Agency</th>
                <th data-sort="type" style="width:12%">Type</th>
                <th data-sort="impact" style="width:10%">Impact Potential</th>
                <th data-sort="dereg" style="width:10%">Dereg. Impact</th>
                <th data-sort="date" style="width:8%">Published</th>
                <th data-sort="area" style="width:8%">Policy Area</th>
              </tr></thead>
              <tbody id="tbody"></tbody>
            </table>
          </div>
          <div class="empty" id="empty" style="display:none"><p>No actions match your filters. Try broadening your search or clearing filters.</p></div>
          <div class="pagination" id="pagination"></div>
        </div>

        <div class="methodology" id="methodology">
          <b>Methodology.</b> This tracker follows federal regulatory actions in the wake of <em>Loper Bright Enterprises v. Raimondo</em> (June 28, 2024), which ended <em>Chevron</em> deference and reset how courts review agency rules. Actions are drawn from the Federal Register and classified by agency, rule type, and policy area. Each is scored on two independent dimensions. <b>Impact Potential</b> measures how substantial the regulatory change is: <em>High</em> denotes substantial revisions to eligibility, compliance standards, or rules affecting many regulated parties; <em>Medium</em> denotes narrower but observable change; <em>Low</em> denotes technical corrections and administrative shifts. <b>Impact of Deregulatory Action</b> measures how directly the deregulatory executive orders drove the decision: <em>High</em> actions repeatedly cite the tracked EOs and produce observable deregulatory change or net-negative cost; <em>Medium</em> actions have limited or mixed deregulatory effect; <em>Low</em> actions cite the EOs only in passing or produce minimal burden reduction. This report is for informational and educational purposes and does not constitute legal advice.
          <details class="scoring-guide">
            <summary>Read the full scoring guide</summary>
            <div class="sg-body">
              <div class="sg-dim">Impact of Deregulatory Action</div>
              <div class="sg-row"><span class="sg-tag sg-h">High</span>A rule is deemed to be "highly" impacted by a deregulatory action when it repeatedly lists or identifies one or multiple of the EOs on the table and has a total cost less than zero and/or makes an observable deregulatory change. The sort of actions that typically fall under this category include those that reduce compliance burdens, raise thresholds of exemptions, or actions responsible for cost savings, or those that substantially reduce regulatory burden. Receiving a high in this category does not correlate to the regulation having a substantial economic impact. Instead, this category relies on the impact of deregulatory influence on the decision.</div>
              <div class="sg-row"><span class="sg-tag sg-m">Medium</span>A rule marked as a medium impact deregulatory action refers to a rule that has some deregulatory effect, but the effect is observably limited or mixed or one that does not clearly rely on a deregulatory action for its decision. This category usually includes rules that simplify procedures and clarify requirements, while also including other actions that narrowly reduce overall regulatory burden. This category also includes rules with both regulatory and deregulatory language and impact.</div>
              <div class="sg-row"><span class="sg-tag sg-l">Low</span>Actions are considered to be "low" in this category when the deregulatory EOs are mentioned only in passing in the standard review section, or when the agency directly specifies that the decision is not a deregulatory action. Included in this are rules that fail to reduce any real regulatory burden. Rules of this sort often include SIP approvals, technical corrections, routine approvals, and other actions where the deregulatory impact is minimal.</div>
              <div class="sg-dim">Impact Potential</div>
              <div class="sg-row"><span class="sg-tag sg-h">High</span>Rules receive a high rating in this category when they make a substantial regulatory change or cause a direct change in agency policy. This includes substantial revisions to eligibility requirements, compliance standards, and rules that have an impact on many regulated individuals. Receiving a high rating means that the rule is likely to have a significant impact on economic, social, and legal policy or substantial changes to an industry.</div>
              <div class="sg-row"><span class="sg-tag sg-m">Medium</span>Rules will receive a medium impact potential when they have a narrower scope than a highly ranked rule, but the impact is still observable. Actions of this nature cover updated fees, regulatory category restructuring, and changes that are applicable to a defined, specific subset of a larger regulated population. This is mostly used for practical changes that do not cause major policy shifts.</div>
              <div class="sg-row"><span class="sg-tag sg-l">Low</span>A rule with a "low" regulatory impact refers largely to technical procedure changes, corrections and administrative shifts in department organizations. This often includes changes to terminology, routine state plan approvals, and minor burden reductions. These rules have little practical effect on any specified regulated population, and the direct legal effects of the rules in this category are usually marginal.</div>
            </div>
          </details>
        </div>
      </section>
    </div>

    <footer>
      <div class="footer-inner">
        <div class="footer-brand">
          <img src="data:image/png;base64,{logo_white}" alt="AFP Foundation">
          <p>A project of Americans for Prosperity Foundation tracking the impact of <em>Loper Bright</em> and the deregulatory agenda across the courts, Congress, and the federal agencies.</p>
        </div>
        <div class="footer-links">
          <div class="footer-col"><h4>Program</h4>
            <a href="https://americansforprosperityfoundation.org/legal/loper-bright/" target="_blank" rel="noopener">Loper Bright Updates</a>
            <a href="https://americansforprosperityfoundation.org/legal/" target="_blank" rel="noopener">Legal Policy</a>
            <a href="https://americansforprosperityfoundation.org/subscribe-to-loper-bright-updates/" target="_blank" rel="noopener">Subscribe</a>
          </div>
          <div class="footer-col"><h4>Foundation</h4>
            <a href="https://americansforprosperityfoundation.org/about-afp/" target="_blank" rel="noopener">About AFPF</a>
            <a href="https://americansforprosperityfoundation.org/investigations/" target="_blank" rel="noopener">Investigations</a>
            <a href="https://americansforprosperityfoundation.org/" target="_blank" rel="noopener">Home</a>
          </div>
        </div>
      </div>
      <div class="footer-bottom">© 2026 Americans for Prosperity Foundation · Recasting Regulations Deregulation Tracker</div>
    </footer>

    <script>
    const SNAPSHOT = {data_json};
    const AIRTABLE_CONFIG = {{ token:"", baseId:"appPUrk3pj2BEN3sZ", tableId:"tblHrsoSoqSoKlpv4" }};
    const FIELD_MAP = {{title:'fldRpFlrZUO40mVxE',agency:'fldCJYupgPD0Jf9aV',subAgency:'fldsXXljpYgXCnyGe',ruleType:'fldCbEJizbYcaRcYy',deregImpact:'fldTo9mHipkxesPw3',impactPotential:'fldvcEFyep5EKYhhE',policyArea:'fldpVtSmPDOSDJ5Q2',datePublished:'fldfseGX1D0EHjQAX',deadlineDate:'fldpcO04XenqvYh7J',url:'fldO7Ad6cg4wUe8LK',citation:'fldOk5Vfsk3xfA5pQ',description:'fldJN9MVwC2gE9p3Z',eoNumbers:'fldX6IeppsTs62iC6',citesLoper:'fldAHYCrKc1noJlqQ'}};
    const EO_CHECKBOX = {{'14154':'fldfqPkwwysl9xHGo','14192':'fldNh6ebD2uUGjMMG','14215':'fldhE7GGx8pKlKnDp','14219':'fldd3Zk2n7fQZrJrA','14267':'fldK6rWxap6vJkf0C','14276':'fldJh8EDTt69FpbtB','14281':'fldCHRKSTiGUNf8c6','14294':'fld7B4R3woLsHCPXy','14332':'fldrTKWzMx22MDHXm','14335':'fldKsJ0LnLzX5Imz9'}};
    let DATA = SNAPSHOT;
    async function loadLive(){{
      if(!AIRTABLE_CONFIG.token) return;
      try{{
        let recs=[],off=null;
        do{{
          const u=new URL(`https://api.airtable.com/v0/${{AIRTABLE_CONFIG.baseId}}/${{AIRTABLE_CONFIG.tableId}}`);
          u.searchParams.set('pageSize','100'); if(off)u.searchParams.set('offset',off);
          const r=await fetch(u,{{headers:{{Authorization:`Bearer ${{AIRTABLE_CONFIG.token}}`}}}});
          if(!r.ok)throw new Error('Airtable '+r.status);
          const j=await r.json(); recs=recs.concat(j.records); off=j.offset;
        }}while(off);
        const sel=v=>(v&&v.name)?v.name:(typeof v==='string'?v:null);
        const ms=v=>Array.isArray(v)?v.map(x=>x.name||x).filter(Boolean):[];
        DATA=recs.map(r=>{{const f=r.fields,g=k=>f[FIELD_MAP[k]],uv=g('url');
          const eoset=new Set(ms(g('eoNumbers')));
          for(const eo in EO_CHECKBOX){{ if(f[EO_CHECKBOX[eo]]===true) eoset.add(eo); }}
          return {{id:r.id,title:g('title'),agency:sel(g('agency')),subAgency:sel(g('subAgency')),ruleType:sel(g('ruleType')),deregImpact:sel(g('deregImpact')),impactPotential:sel(g('impactPotential')),policyArea:sel(g('policyArea')),datePublished:g('datePublished'),deadlineDate:g('deadlineDate'),url:(uv&&uv.url)?uv.url:(typeof uv==='string'?uv:null),citation:g('citation'),description:g('description'),eoNumbers:Array.from(eoset).sort(),citesLoper:g('citesLoper')===true,highHigh:(sel(g('impactPotential'))==='High'&&sel(g('deregImpact'))==='High')}};
        }}).filter(r=>r.title);
        applyFilters();
        document.getElementById('asof').innerHTML='Data current as of <strong>'+new Date().toISOString().slice(0,10)+' (live)</strong>';
      }}catch(e){{console.warn('Live load failed, using snapshot:',e);}}
    }}
    const PAGE=50; let filtered=[...DATA],page=1,sortKey='date-desc',fPrio='',fType='',fArea='',fAgency='',fEO='',q='',fLoper=false,fHH=false,fExcl=false;
    const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    const vd=d=>d&&/^20\\d\\d-\\d\\d-\\d\\d$/.test(d);
    function fmtDate(d){{if(!vd(d))return '—';const[y,m,day]=d.split('-');const M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];return `${{M[+m-1]}} ${{+day}}, ${{y}}`;}}
    function tBadge(t){{const m={{'Final Rule':['t-final','Final Rule'],'Notice of Proposed Rule Making':['t-nprm','NPRM'],'Direct Final Rule':['t-dfr','Direct Final'],'Other Notice':['t-notice','Notice'],'Interim/Interim Final Rule':['t-interim','Interim'],'Temporary Rule':['t-temp','Temporary'],'Amended Rule':['t-amended','Amended'],'Withdrawal':['t-withdrawal','Withdrawal']}};const[c,l]=m[t]||['t-notice',t||'—'];return `<span class="tbadge ${{c}}">${{l}}</span>`;}}
    function pBadge(p){{return p?`<span class="badge b-${{p.toLowerCase()}}">${{p}}</span>`:'<span class="badge b-none">—</span>';}}
    function render(){{
      const tb=document.getElementById('tbody'),empty=document.getElementById('empty');
      const total=filtered.length,start=(page-1)*PAGE,end=Math.min(start+PAGE,total);
      document.getElementById('result-meta').innerHTML=total===DATA.length?`<b>${{total.toLocaleString()}}</b> actions`:`<b>${{total.toLocaleString()}}</b> of ${{DATA.length.toLocaleString()}}`;
      if(!total){{tb.innerHTML='';empty.style.display='block';document.getElementById('pagination').innerHTML='';return;}}
      empty.style.display='none';
      tb.innerHTML=filtered.slice(start,end).map(r=>{{
        const title=(r.title||'').replace(/\\n/g,' ').trim();
        const t=r.url?`<a href="${{esc(r.url)}}" target="_blank" rel="noopener">${{esc(title)}}</a>`:esc(title);
        const eos=(r.eoNumbers||[]).map(e=>`<span class="eo-tag">EO ${{esc(e)}}</span>`).join('');
        const loperTag=r.citesLoper?`<span class="loper-badge">Cites Loper Bright</span>`:'';
        const hhTag=r.highHigh?`<span class="hh-marker">◆ High / High</span>`:'';
        const desc=r.description?`<div class="rule-desc">${{esc(r.description)}}</div>`:'';
        const cit=r.citation?`<span>${{esc(r.citation)}}</span> `:'';
        return `<tr>
          <td><div class="rule-title">${{t}}</div>${{desc}}<div class="rule-meta">${{cit}}${{eos}}</div><div>${{loperTag}} ${{hhTag}}</div></td>
          <td><div class="agency-name">${{esc(r.agency||'—')}}</div>${{r.subAgency?`<div class="agency-sub">${{esc(r.subAgency)}}</div>`:''}}</td>
          <td>${{tBadge(r.ruleType)}}</td>
          <td>${{pBadge(r.impactPotential)}}</td>
          <td>${{pBadge(r.deregImpact)}}</td>
          <td style="white-space:nowrap;color:var(--ink-soft)">${{fmtDate(r.datePublished)}}</td>
          <td style="color:var(--ink-soft)">${{esc(r.policyArea||'—')}}</td>
        </tr>`;
      }}).join('');
      renderPag(total,start,end);
    }}
    function renderPag(total,start,end){{
      const pg=document.getElementById('pagination'),pages=Math.ceil(total/PAGE);let b='';
      for(let i=1;i<=pages;i++){{if(i===1||i===pages||(i>=page-2&&i<=page+2))b+=`<button class="pbtn${{i===page?' active':''}}" onclick="goPage(${{i}})">${{i}}</button>`;else if(i===page-3||i===page+3)b+=`<span style="align-self:center;color:var(--ink-faint);padding:0 2px">…</span>`;}}
      pg.innerHTML=`<div class="page-info">Showing ${{start+1}}–${{end}} of ${{total.toLocaleString()}}</div><div class="page-btns"><button class="pbtn" onclick="goPage(${{page-1}})" ${{page===1?'disabled':''}}>‹</button>${{b}}<button class="pbtn" onclick="goPage(${{page+1}})" ${{page===pages?'disabled':''}}>›</button></div>`;
    }}
    function goPage(p){{page=Math.max(1,Math.min(p,Math.ceil(filtered.length/PAGE)));render();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function applyFilters(){{
      const query=q.toLowerCase();
      filtered=DATA.filter(r=>{{
        if(fPrio&&r.impactPotential!==fPrio)return false;
        if(fType&&r.ruleType!==fType)return false;
        if(fArea&&r.policyArea!==fArea)return false;
        if(fAgency&&r.agency!==fAgency)return false;
        if(fEO&&!(r.eoNumbers||[]).includes(fEO))return false;
        if(fLoper&&!r.citesLoper)return false;
        if(fHH&&!r.highHigh)return false;
        if(fExcl&&!((r.eoNumbers||[]).length===1&&(r.eoNumbers||[])[0]===fEO))return false;
        if(query){{const h=[r.title,r.agency,r.subAgency,r.description,r.citation,r.policyArea].filter(Boolean).join(' ').toLowerCase();if(!h.includes(query))return false;}}
        return true;
      }});
      const po={{High:0,Medium:1,Low:2}};
      filtered.sort((a,b)=>{{switch(sortKey){{
        case 'date-desc':return (b.datePublished||'').localeCompare(a.datePublished||'');
        case 'date-asc':return (a.datePublished||'').localeCompare(b.datePublished||'');
        case 'title-asc':return (a.title||'').localeCompare(b.title||'');
        case 'agency-asc':return (a.agency||'').localeCompare(b.agency||'');
        case 'impact-desc':return (po[a.impactPotential]??9)-(po[b.impactPotential]??9);
        case 'dereg-desc':return (po[a.deregImpact]??9)-(po[b.deregImpact]??9);
        default:return 0;}}}});
      page=1;render();
    }}
    function setPrio(el,v){{fPrio=v;document.querySelectorAll('.chip[data-prio]').forEach(c=>c.classList.remove('active'));el.classList.add('active');applyFilters();}}
    function clearAll(){{fPrio=fType=fArea=fAgency=fEO=q='';fLoper=false;fHH=false;fExcl=false;sortKey='date-desc';document.getElementById('search').value='';['f-type','f-area','f-agency','f-eo'].forEach(id=>document.getElementById(id).value='');document.getElementById('sort').value='date-desc';document.querySelectorAll('.chip[data-prio]').forEach(c=>c.classList.toggle('active',c.dataset.prio===''));document.getElementById('loper-chip').classList.remove('active');document.getElementById('hh-chip').classList.remove('active');applyFilters();}}
    function toggleLoper(el){{fLoper=!fLoper;el.classList.toggle('active',fLoper);applyFilters();}}
    function toggleHH(el){{fHH=!fHH;el.classList.toggle('active',fHH);applyFilters();}}
    function showLoperCited(){{fLoper=true;document.getElementById('loper-chip').classList.add('active');applyFilters();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function _resetSpot(){{fPrio=fArea=fAgency=q='';fLoper=false;fExcl=false;fHH=false;document.getElementById('search').value='';['f-area','f-agency'].forEach(id=>document.getElementById(id).value='');document.querySelectorAll('.chip[data-prio]').forEach(c=>c.classList.toggle('active',c.dataset.prio===''));document.getElementById('loper-chip').classList.remove('active');document.getElementById('hh-chip').classList.remove('active');}}
    function filterEO(eo){{_resetSpot();fEO=eo;document.getElementById('f-eo').value=eo;applyFilters();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function filterEOExclusive(){{_resetSpot();fEO='14192';document.getElementById('f-eo').value='14192';fExcl=true;applyFilters();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function filterEOHH(eo){{_resetSpot();fEO=eo;document.getElementById('f-eo').value=eo;fHH=true;document.getElementById('hh-chip').classList.add('active');applyFilters();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function filterEOExclusiveHH(){{_resetSpot();fEO='14192';document.getElementById('f-eo').value='14192';fExcl=true;fHH=true;document.getElementById('hh-chip').classList.add('active');applyFilters();document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    function exportCSV(){{
      const cols=['title','agency','subAgency','ruleType','impactPotential','deregImpact','highHigh','policyArea','datePublished','citation','eoNumbers','citesLoper','url'];
      const head=['Rule/Action','Agency','Sub-Agency','Type','Impact Potential','Deregulatory Impact','High/High','Policy Area','Published','Citation','Executive Orders','Cites Loper Bright','URL'];
      const rows=filtered.map(r=>cols.map(c=>{{let v=r[c];if(c==='citesLoper'||c==='highHigh')v=v?'Yes':'No';if(Array.isArray(v))v=v.join('; ');v=(v==null?'':String(v)).replace(/\\n/g,' ').replace(/"/g,'""');return `"${{v}}"`;}}).join(','));
      const csv=[head.map(h=>`"${{h}}"`).join(','),...rows].join('\\n');
      const blob=new Blob([csv],{{type:'text/csv'}}),url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='recasting-regulations-tracker.csv';a.click();URL.revokeObjectURL(url);
    }}
    document.getElementById('f-type').onchange=e=>{{fType=e.target.value;applyFilters();}};
    document.getElementById('f-area').onchange=e=>{{fArea=e.target.value;applyFilters();}};
    document.getElementById('f-agency').onchange=e=>{{fAgency=e.target.value;applyFilters();}};
    document.getElementById('f-eo').onchange=e=>{{fEO=e.target.value;if(fEO!=='14192')fExcl=false;applyFilters();}};
    document.getElementById('sort').onchange=e=>{{sortKey=e.target.value;applyFilters();}};
    let st;document.getElementById('search').oninput=e=>{{clearTimeout(st);st=setTimeout(()=>{{q=e.target.value;applyFilters();}},200);}};
    document.querySelectorAll('thead th[data-sort]').forEach(th=>th.onclick=()=>{{
      const k=th.dataset.sort;const map={{title:'title-asc',agency:'agency-asc',type:'date-desc',impact:'impact-desc',dereg:'dereg-desc',date:'date-desc',area:'date-desc'}};
      sortKey=(k==='date')?(sortKey==='date-desc'?'date-asc':'date-desc'):(map[k]||'date-desc');
      const opts=['date-desc','date-asc','title-asc','agency-asc','impact-desc','dereg-desc'];
      document.getElementById('sort').value=opts.includes(sortKey)?sortKey:'date-desc';
      document.querySelectorAll('thead th').forEach(t=>t.classList.remove('sorted'));th.classList.add('sorted');applyFilters();
    }});
    function jump(){{document.getElementById('tracker').scrollIntoView({{behavior:'smooth'}});}}
    document.querySelectorAll('.hbar[data-area]').forEach(b=>b.onclick=()=>{{fArea=b.dataset.area;document.getElementById('f-area').value=fArea;applyFilters();jump();}});
    document.querySelectorAll('.hbar[data-agency]').forEach(b=>b.onclick=()=>{{fAgency=b.dataset.agency;document.getElementById('f-agency').value=fAgency;applyFilters();jump();}});
    document.querySelectorAll('.eo-card[data-eo]').forEach(b=>b.onclick=()=>{{fEO=b.dataset.eo;document.getElementById('f-eo').value=fEO;applyFilters();jump();}});
    document.querySelectorAll('.lbar[data-area]').forEach(b=>b.onclick=()=>{{fLoper=true;document.getElementById('loper-chip').classList.add('active');fArea=b.dataset.area;document.getElementById('f-area').value=fArea;applyFilters();jump();}});
    document.querySelectorAll('.lbar[data-agency]').forEach(b=>b.onclick=()=>{{fLoper=true;document.getElementById('loper-chip').classList.add('active');fAgency=b.dataset.agency;document.getElementById('f-agency').value=fAgency;applyFilters();jump();}});
    applyFilters();loadLive();
    </script>
    </body>
    </html>'''


    html = html.replace('--ocean-deep: #003views;', '--ocean-deep: #003038;')
    return html


if __name__ == "__main__":
    main()

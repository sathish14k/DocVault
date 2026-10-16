"""DocVault - private, self-hosted document vault.

Files live on disk under DATA_DIR/files/<id>/, metadata + full-text index in SQLite (FTS5).
"""
import calendar
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from starlette.background import BackgroundTask

from .countries import COUNTRIES
from .extract import IMG_EXT, extract_text

DATA = Path(os.environ.get("DATA_DIR", "/data"))
FILES, CACHE, EXPORTS = DATA / "files", DATA / "cache", DATA / "exports"
for _d in (FILES, CACHE, EXPORTS):
    _d.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "vault.db"
PASSWORD = os.environ.get("APP_PASSWORD", "")
HL_S, HL_E = "\x01", "\x02"  # snippet highlight markers (escaped client-side)
NO_CAT = "__none__"  # category filter value meaning "no category"
SHARED = "__shared__"  # member filter value meaning "shared / whole family" (no member)

# Documents whose title, tags or file name mention one of these must carry a full expiry date
# (day, month, year). Any other document may carry an optional month/year expiry.
# "pp" is the passport abbreviation used in file names such as KP_PP_New_2024.pdf.
EXPIRY_REQUIRED = ["passport", "pp", "lease", "service contract", "rental contract", "tenancy contract"]
VISA_TYPES = ["Residence", "Employment", "Family / Dependent", "Golden", "Green", "Investor", "Student",
              "Freelance", "Tourist", "Visit", "Business", "Transit", "Schengen"]
LICENSE_TYPES = ["Light vehicle", "Motorcycle", "Heavy vehicle", "Light bus", "Heavy bus", "Light forklift",
                 "Heavy forklift", "International Driving Permit"]
# Permit categories (visas, driving licences). Their documents need a full expiry date, can be acknowledged
# as expired, and carry a type, issuing country and optional validity. Value: the type field's label and
# its suggestions (free text is accepted; COUNTRIES suggests issuing countries). The columns are named
# visa_type / visa_country / visa_validity because visas came first.
PERMIT_CATEGORIES = {
    "Visa & Immigration": ("Visa type", VISA_TYPES),
    "Drivers License": ("License type", LICENSE_TYPES),
}
# Every document filed under one of these categories also needs a full expiry date.
EXPIRY_REQUIRED_CATEGORIES = list(PERMIT_CATEGORIES)
# Documents in these categories must have a type and issuing country.
VISA_TYPE_CATEGORIES = list(PERMIT_CATEGORIES)
# Expired documents in these categories can be acknowledged, which hides them from the Expired list.
EXPIRY_ACK_CATEGORIES = list(PERMIT_CATEGORIES)
_EXPIRY_RE = re.compile(r"\b(?:" + "|".join(k.replace(" ", r"\s+") for k in EXPIRY_REQUIRED) + r")s?\b")

DEFAULT_CATEGORIES = [
    "Identity", "Visa & Immigration", "Education", "Employment", "Finance & Tax",
    "Property & Tenancy", "Vehicle", "Medical", "Insurance", "Certificates", "Other",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, relation TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  member_id INTEGER REFERENCES members(id),
  filename TEXT NOT NULL,
  stored_path TEXT NOT NULL DEFAULT '',
  mime TEXT, size INTEGER, sha256 TEXT,
  doc_date TEXT DEFAULT '', expiry_date TEXT DEFAULT '', expiry_ack INTEGER NOT NULL DEFAULT 0, visa_type TEXT DEFAULT '', visa_country TEXT DEFAULT '', visa_validity TEXT DEFAULT '', notes TEXT DEFAULT '', tags TEXT DEFAULT '', content TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'current',
  supersedes_id INTEGER, superseded_by_id INTEGER,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collections(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, notes TEXT DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_items(
  collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
  document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  PRIMARY KEY(collection_id, document_id));
"""
# content stays column 4 (used by snippet()); member is searchable so "priya passport" works,
# visa_type/visa_country so "golden visa" or "schengen france" work
FTS_SQL = ("CREATE VIRTUAL TABLE docs_fts USING fts5("
           "title, filename, tags, notes, content, member, visa_type, visa_country, tokenize='porter unicode61')")

BASE_SQL = ("SELECT d.*, c.name AS category, m.name AS member FROM documents d "
            "LEFT JOIN categories c ON c.id = d.category_id "
            "LEFT JOIN members m ON m.id = d.member_id")
FIELDS = ("id", "title", "filename", "mime", "size", "doc_date", "expiry_date", "expiry_ack", "visa_type", "visa_country", "visa_validity", "notes", "tags", "status",
          "supersedes_id", "superseded_by_id", "created_at", "category", "member_id", "member")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    con = db()
    con.executescript(SCHEMA)
    # migrations for vaults created before family members existed
    if "member_id" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN member_id INTEGER REFERENCES members(id)")
    if "expiry_date" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN expiry_date TEXT DEFAULT ''")
    if "expiry_ack" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN expiry_ack INTEGER NOT NULL DEFAULT 0")
    if "visa_type" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN visa_type TEXT DEFAULT ''")
    if "visa_country" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN visa_country TEXT DEFAULT ''")
    if "visa_validity" not in [r["name"] for r in con.execute("PRAGMA table_info(documents)")]:
        con.execute("ALTER TABLE documents ADD COLUMN visa_validity TEXT DEFAULT ''")
    fts = con.execute("SELECT sql FROM sqlite_master WHERE name='docs_fts'").fetchone()
    if not fts or "visa_country" not in fts["sql"]:
        con.execute("DROP TABLE IF EXISTS docs_fts")
        con.execute(FTS_SQL)
        con.execute("INSERT INTO docs_fts(rowid,title,filename,tags,notes,content,member,visa_type,visa_country) "
                    "SELECT d.id, d.title, d.filename, d.tags, d.notes, d.content, COALESCE(m.name,''), "
                    "COALESCE(d.visa_type,''), COALESCE(d.visa_country,'') "
                    "FROM documents d LEFT JOIN members m ON m.id=d.member_id")
    if not con.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        con.executemany("INSERT INTO categories(name) VALUES(?)", [(c,) for c in DEFAULT_CATEGORIES])
    con.commit()
    con.close()


init()

security = HTTPBasic(auto_error=False)


def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)):
    if not PASSWORD:
        return
    if not creds or not secrets.compare_digest(creds.password.encode(), PASSWORD.encode()):
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="DocVault"'})


app = FastAPI(title="DocVault", dependencies=[Depends(auth)])


# ---------- helpers ----------
def safe(s: str, limit: int = 80) -> str:
    return (re.sub(r"[^\w\-. ()&]+", "_", s or "").strip(" ._")[:limit]) or "item"


def fts_query(q: str) -> str:
    return " ".join(f'"{t}"*' for t in re.findall(r"\w+", q or "", re.UNICODE))


def version_of(con, doc_id: int) -> int:
    n, cur = 1, con.execute("SELECT supersedes_id FROM documents WHERE id=?", (doc_id,)).fetchone()
    while cur and cur["supersedes_id"]:
        n += 1
        cur = con.execute("SELECT supersedes_id FROM documents WHERE id=?", (cur["supersedes_id"],)).fetchone()
    return n


def doc_out(con, r) -> dict:
    d = {k: r[k] for k in FIELDS}
    d["version"] = version_of(con, r["id"])
    if "snip" in r.keys():
        d["snippet"] = r["snip"]
    return d


def get_row(con, i: int):
    r = con.execute(BASE_SQL + " WHERE d.id=?", (i,)).fetchone()
    if not r:
        raise HTTPException(404, "Document not found")
    return r


def doc_path(r) -> Path:
    return DATA / r["stored_path"]


def index_doc(con, i: int):
    r = con.execute(BASE_SQL + " WHERE d.id=?", (i,)).fetchone()
    con.execute("DELETE FROM docs_fts WHERE rowid=?", (i,))
    con.execute("INSERT INTO docs_fts(rowid,title,filename,tags,notes,content,member,visa_type,visa_country) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (i, r["title"], r["filename"], r["tags"], r["notes"], r["content"], r["member"] or "",
                 r["visa_type"] or "", r["visa_country"] or ""))


def cat_id(con, name: Optional[str]) -> Optional[int]:
    name = (name or "").strip()
    if not name:
        return None
    r = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if r:
        return r["id"]
    return con.execute("INSERT INTO categories(name) VALUES(?)", (name,)).lastrowid


def member_id(con, name: Optional[str]) -> Optional[int]:
    """Blank means shared (whole family); an unknown name adds a new member."""
    name = (name or "").strip()
    if not name:
        return None
    r = con.execute("SELECT id FROM members WHERE name=?", (name,)).fetchone()
    if r:
        return r["id"]
    return con.execute("INSERT INTO members(name) VALUES(?)", (name,)).lastrowid


def member_filter(member: str, where: list, args: list):
    if member == SHARED:
        where.append("d.member_id IS NULL")
    elif member:
        where.append("m.name=?")
        args.append(member)


def needs_expiry(category: Optional[str], *texts) -> bool:
    if (category or "").strip().lower() in (c.lower() for c in EXPIRY_REQUIRED_CATEGORIES):
        return True
    s = re.sub(r"[_\-.]+", " ", " ".join(t or "" for t in texts).lower())
    return bool(_EXPIRY_RE.search(s))


def has_visa_type(category: Optional[str]) -> bool:
    return (category or "").strip().lower() in (c.lower() for c in VISA_TYPE_CATEGORIES)


def type_label(category: Optional[str]) -> str:
    key = (category or "").strip().lower()
    return next((label for c, (label, _) in PERMIT_CATEGORIES.items() if c.lower() == key), "Type")


def clean_visa_field(category: Optional[str], v: Optional[str], label: str) -> str:
    """Type / issuing country: required for permit categories; not kept on other documents."""
    if not has_visa_type(category):
        return ""
    v = (v or "").strip()[:60]
    if not v:
        raise HTTPException(400, f"{label} is required for {category.strip()} documents")
    return v


def clean_validity(category: Optional[str], v: Optional[str]) -> str:
    """Optional visa validity, stored as '<n> days' or '<n> months'; not kept on other documents."""
    v = re.sub(r"\s+", " ", (v or "").strip().lower())
    if not v or not has_visa_type(category):
        return ""
    m = re.fullmatch(r"(\d{1,4}) (day|month)s?", v)
    if not m or int(m[1]) == 0:
        raise HTTPException(400, "Validity must be a number of days or months, e.g. 30 days or 24 months")
    return f"{int(m[1])} {m[2]}s"


def expiry_from_validity(issue: str, validity: str) -> str:
    """Last valid day, counting the issue date as day 1 (24 months from 2026-01-15 -> 2028-01-14)."""
    try:
        start = date.fromisoformat((issue or "").strip())
    except ValueError:
        return ""
    if not validity:
        return ""
    n, unit = validity.split()
    if unit == "days":
        return (start + timedelta(days=int(n) - 1)).isoformat()
    end = add_months(start, int(n))
    if end.day < start.day:  # no such day in that month (31 Jan + 1 month): valid to its last day
        return end.isoformat()
    return (end - timedelta(days=1)).isoformat()


def can_ack(category: Optional[str]) -> bool:
    return (category or "").strip().lower() in (c.lower() for c in EXPIRY_ACK_CATEGORIES)


def clean_expiry(v: Optional[str], required: bool = False) -> str:
    """Expiry is YYYY-MM-DD, or YYYY-MM (month/year only) for documents that do not require a full date."""
    v = (v or "").strip()
    if v:
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                date.fromisoformat(v)
            elif re.fullmatch(r"\d{4}-\d{2}", v):
                date.fromisoformat(v + "-01")
            else:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Expiry must be a date (YYYY-MM-DD) or a month (YYYY-MM)")
    if required and len(v) != 10:
        raise HTTPException(400, "This document needs a full expiry date (day, month and year)")
    return v


def expiry_day(v: str) -> Optional[date]:
    """Last valid day: the date itself, or the last day of the month for month-only expiries."""
    if len(v) == 10:
        return date.fromisoformat(v)
    if len(v) == 7:
        y, m = map(int, v.split("-"))
        return date(y, m, calendar.monthrange(y, m)[1])
    return None


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


HEIC_EXT = {".heic", ".heif"}


def heic_to_jpg(src: Path, dest: Path):
    """Convert an iPhone HEIC/HEIF photo to JPEG (upright, EXIF kept) so it can be OCR'd and previewed."""
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener
    register_heif_opener()
    with Image.open(src) as im:
        exif = im.getexif()
        out = ImageOps.exif_transpose(im).convert("RGB")
    exif.pop(0x0112, None)  # orientation already applied
    out.save(dest, "JPEG", quality=92, exif=exif.tobytes())


def file_digest(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def libre_to_pdf(src: Path, out: Path):
    work = Path(tempfile.mkdtemp(dir=DATA))
    try:
        tmp_src = work / f"in{src.suffix.lower()}"
        shutil.copy(src, tmp_src)
        subprocess.run(
            ["soffice", "--headless", "--norestore", f"-env:UserInstallation=file://{work}/profile",
             "--convert-to", "pdf", "--outdir", str(work), str(tmp_src)],
            check=True, timeout=240, capture_output=True)
        shutil.move(str(work / "in.pdf"), out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def as_pdf(doc_id: int, src: Path, cache_name: Optional[str] = None) -> Path:
    """Return a PDF rendition of src (cached). PDFs are returned as-is."""
    ext = src.suffix.lower()
    if ext == ".pdf":
        return src
    out = CACHE / f"{cache_name or doc_id}.pdf"
    if out.exists():
        return out
    if ext in IMG_EXT:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(src))
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.save(out, "PDF", resolution=150.0)
    else:
        libre_to_pdf(src, out)
    return out


# ---------- categories ----------
class NameBody(BaseModel):
    name: str
    notes: str = ""


@app.get("/api/categories")
def list_categories(member: str = ""):
    """Counts are of current documents, limited to one member (or shared) when given."""
    where, args = ["d.category_id=c.id", "d.status='current'"], []
    member_filter(member, where, args)
    con = db()
    rows = con.execute(
        "SELECT c.id, c.name, "
        "(SELECT COUNT(*) FROM documents d LEFT JOIN members m ON m.id=d.member_id "
        f"WHERE {' AND '.join(where)}) AS count "
        "FROM categories c ORDER BY c.name COLLATE NOCASE", args).fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


@app.get("/api/summary")
def summary(member: str = ""):
    where, args = ["d.status='current'"], []
    member_filter(member, where, args)
    con = db()
    r = con.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(d.category_id IS NULL),0) AS uncategorised, "
        "COALESCE(SUM(d.member_id IS NULL),0) AS shared "
        "FROM documents d LEFT JOIN members m ON m.id=d.member_id "
        f"WHERE {' AND '.join(where)}", args).fetchone()
    con.close()
    return dict(r)


@app.get("/api/expiry-rules")
def expiry_rules():
    con = db()
    def with_used(base, col, category=None):  # suggestions plus any value already typed in
        seen = {t.lower() for t in base}
        sql = (f"SELECT DISTINCT d.{col} FROM documents d LEFT JOIN categories c ON c.id=d.category_id "
               f"WHERE d.{col}!=''" + (" AND c.name=? COLLATE NOCASE" if category else "") +
               f" ORDER BY d.{col} COLLATE NOCASE")
        used = [r[0] for r in con.execute(sql, (category,) if category else ())]
        return base + [t for t in used if t.lower() not in seen]
    try:
        return {"required": EXPIRY_REQUIRED, "categories": EXPIRY_REQUIRED_CATEGORIES,
                "ack_categories": EXPIRY_ACK_CATEGORIES, "visa_categories": VISA_TYPE_CATEGORIES,
                "type_fields": {c.lower(): {"label": label, "options": with_used(opts, "visa_type", c)}
                                for c, (label, opts) in PERMIT_CATEGORIES.items()},
                "countries": with_used(COUNTRIES, "visa_country")}
    finally:
        con.close()


@app.get("/api/expiring")
def expiring(member: str = ""):
    """Current documents expiring within 6 months (or already expired), bucketed by calendar months,
    plus documents that require an expiry date but have none."""
    where, args = ["d.status='current'"], []
    member_filter(member, where, args)
    today = date.today()
    limits = [("month1", add_months(today, 1)), ("month3", add_months(today, 3)), ("month6", add_months(today, 6))]
    out = {"expired": [], "month1": [], "month3": [], "month6": [], "missing": []}
    con = db()
    try:
        for r in con.execute(BASE_SQL + " WHERE " + " AND ".join(where), args).fetchall():
            exp = expiry_day(r["expiry_date"] or "")
            if len(r["expiry_date"] or "") != 10 and needs_expiry(r["category"], r["title"], r["tags"], r["filename"]):
                out["missing"].append(doc_out(con, r))  # no expiry, or only month/year where a full date is due
            if not exp:
                continue
            bucket = "expired" if exp < today else next((k for k, lim in limits if exp <= lim), None)
            if bucket == "expired" and r["expiry_ack"] and can_ack(r["category"]):
                continue  # acknowledged as expired
            if bucket:
                d = doc_out(con, r)
                d["expires_on"], d["days_left"] = exp.isoformat(), (exp - today).days
                out[bucket].append(d)
    finally:
        con.close()
    for k, v in out.items():
        v.sort(key=(lambda d: d["title"].lower()) if k == "missing" else (lambda d: d["days_left"]))
    return out


# ---------- family members ----------
class MemberBody(BaseModel):
    name: str
    relation: str = ""


@app.get("/api/members")
def list_members():
    con = db()
    rows = con.execute(
        "SELECT m.id, m.name, m.relation, "
        "(SELECT COUNT(*) FROM documents d WHERE d.member_id=m.id AND d.status='current') AS count "
        "FROM members m ORDER BY m.id").fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


@app.post("/api/members")
def add_member(b: MemberBody):
    name = b.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    if name in (SHARED, NO_CAT):
        raise HTTPException(400, "Reserved name")
    con = db()
    try:
        try:
            i = con.execute("INSERT INTO members(name,relation) VALUES(?,?)", (name, b.relation.strip())).lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A family member with that name exists")
        con.commit()
        return {"id": i, "name": name, "relation": b.relation.strip()}
    finally:
        con.close()


@app.put("/api/members/{mid}")
def update_member(mid: int, b: MemberBody):
    name = b.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    con = db()
    try:
        try:
            con.execute("UPDATE members SET name=?, relation=? WHERE id=?", (name, b.relation.strip(), mid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A family member with that name exists")
        for r in con.execute("SELECT id FROM documents WHERE member_id=?", (mid,)).fetchall():
            index_doc(con, r["id"])  # keep the name searchable
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/members/{mid}")
def delete_member(mid: int):
    con = db()
    try:
        if con.execute("SELECT 1 FROM documents WHERE member_id=? LIMIT 1", (mid,)).fetchone():
            raise HTTPException(409, "This person still has documents (including superseded ones). "
                                     "Move them to someone else or to Shared first.")
        con.execute("DELETE FROM members WHERE id=?", (mid,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.post("/api/categories")
def add_category(b: NameBody):
    con = db()
    try:
        if not b.name.strip():
            raise HTTPException(400, "Name required")
        i = cat_id(con, b.name)
        con.commit()
        return {"id": i, "name": b.name.strip()}
    finally:
        con.close()


@app.put("/api/categories/{cid}")
def rename_category(cid: int, b: NameBody):
    con = db()
    try:
        try:
            con.execute("UPDATE categories SET name=? WHERE id=?", (b.name.strip(), cid))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A category with that name exists")
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/categories/{cid}")
def delete_category(cid: int):
    con = db()
    try:
        if con.execute("SELECT 1 FROM documents WHERE category_id=? LIMIT 1", (cid,)).fetchone():
            raise HTTPException(409, "Category still has documents (including superseded ones)")
        con.execute("DELETE FROM categories WHERE id=?", (cid,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ---------- documents ----------
@app.get("/api/documents")
def list_documents(q: str = "", category: str = "", member: str = "", include_superseded: bool = False,
                   limit: int = 300):
    con = db()
    try:
        where, args = [], []
        if category == NO_CAT:
            where.append("d.category_id IS NULL")
        elif category:
            where.append("c.name=?")
            args.append(category)
        member_filter(member, where, args)
        if not include_superseded:
            where.append("d.status='current'")
        fq = fts_query(q)
        if fq:
            sql = ("SELECT d.*, c.name AS category, m.name AS member, snippet(docs_fts,4,?,?,'…',14) AS snip "
                   "FROM docs_fts JOIN documents d ON d.id=docs_fts.rowid "
                   "LEFT JOIN categories c ON c.id=d.category_id "
                   "LEFT JOIN members m ON m.id=d.member_id WHERE docs_fts MATCH ?")
            a = [HL_S, HL_E, fq] + args
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " ORDER BY bm25(docs_fts,10,5,5,3,1,8,8,8) LIMIT ?"
        else:
            sql = BASE_SQL + (" WHERE " + " AND ".join(where) if where else "") + \
                  " ORDER BY d.created_at DESC, d.id DESC LIMIT ?"
            a = args
        rows = con.execute(sql, a + [limit]).fetchall()
        return [doc_out(con, r) for r in rows]
    finally:
        con.close()


@app.post("/api/documents")
def upload_document(
    file: UploadFile = File(...),
    title: str = Form(""),
    category: str = Form(""),
    member: Optional[str] = Form(None),  # "" = shared; omitted = keep the superseded doc's member
    doc_date: str = Form(""),
    expiry_date: str = Form(""),
    expiry_ack: bool = Form(False),
    visa_type: str = Form(""),
    visa_country: str = Form(""),
    visa_validity: str = Form(""),
    notes: str = Form(""),
    tags: str = Form(""),
    supersedes_id: Optional[int] = Form(None),
    force: bool = Form(False),
):
    con = db()
    tmp = DATA / f"upload-{secrets.token_hex(6)}"
    dest_dir = None
    try:
        h, size = hashlib.sha256(), 0
        with open(tmp, "wb") as out:
            while chunk := file.file.read(1 << 20):
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if size == 0:
            raise HTTPException(400, "Empty file")
        digest = h.hexdigest()
        fname = safe(file.filename or "file", 120)
        converted_from = None
        if Path(fname).suffix.lower() in HEIC_EXT:
            jpg = tmp.with_suffix(".jpg")
            try:
                heic_to_jpg(tmp, jpg)
            except Exception:
                jpg.unlink(missing_ok=True)
                raise HTTPException(422, "Could not convert this HEIC photo to JPG")
            jpg.replace(tmp)
            converted_from, fname = fname, f"{Path(fname).stem}.jpg"
            digest, size = file_digest(tmp), tmp.stat().st_size
        if not force:
            dup = con.execute(BASE_SQL + " WHERE d.sha256=? LIMIT 1", (digest,)).fetchone()
            if dup:
                return JSONResponse(status_code=409, content={
                    "detail": "This exact file is already in the vault",
                    "duplicate": {"id": dup["id"], "title": dup["title"], "status": dup["status"]}})
        old = None
        if supersedes_id:
            old = get_row(con, supersedes_id)
            if old["status"] != "current":
                raise HTTPException(400, "Only the current version can be superseded")
        title = title.strip() or (old["title"] if old else Path(fname).stem)
        cid = cat_id(con, category) if category.strip() else (old["category_id"] if old else None)
        mid = member_id(con, member) if member is not None else (old["member_id"] if old else None)
        cat_name = category.strip() or (old["category"] if old else "")
        visa_validity = clean_validity(cat_name, visa_validity.strip() or (old["visa_validity"] if old else ""))
        if not expiry_date.strip() and visa_validity:
            expiry_date = expiry_from_validity(doc_date, visa_validity)
            if not expiry_date:
                raise HTTPException(400, "Enter the issue date so the expiry can be calculated from the validity")
        expiry_date = clean_expiry(expiry_date, needs_expiry(cat_name, title, tags, fname))
        visa_type = clean_visa_field(cat_name, visa_type.strip() or (old["visa_type"] if old else ""),
                                     type_label(cat_name))
        visa_country = clean_visa_field(cat_name, visa_country.strip() or (old["visa_country"] if old else ""),
                                        "Issuing country")
        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        cur = con.execute(
            "INSERT INTO documents(title,category_id,member_id,filename,mime,size,sha256,doc_date,expiry_date,"
            "expiry_ack,visa_type,visa_country,visa_validity,notes,tags,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title, cid, mid, fname, mime, size, digest, doc_date.strip(), expiry_date,
             int(expiry_ack and can_ack(cat_name)), visa_type, visa_country, visa_validity, notes.strip(), tags.strip(),
             datetime.now().isoformat(timespec="seconds")))
        i = cur.lastrowid
        dest_dir = FILES / str(i)
        dest_dir.mkdir(parents=True)
        dest = dest_dir / fname
        shutil.move(str(tmp), dest)
        text = extract_text(dest)
        con.execute("UPDATE documents SET stored_path=?, content=? WHERE id=?",
                    (f"files/{i}/{fname}", text, i))
        if old:
            con.execute("UPDATE documents SET status='superseded', superseded_by_id=? WHERE id=?", (i, old["id"]))
            con.execute("UPDATE documents SET supersedes_id=? WHERE id=?", (old["id"], i))
        index_doc(con, i)
        con.commit()
        return {**doc_out(con, get_row(con, i)), "converted_from": converted_from}
    except Exception:
        con.rollback()
        if dest_dir:
            shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
        con.close()


class DocPatch(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    member: Optional[str] = None  # "" = shared
    doc_date: Optional[str] = None
    expiry_date: Optional[str] = None
    expiry_ack: Optional[bool] = None
    visa_type: Optional[str] = None
    visa_country: Optional[str] = None
    visa_validity: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[str] = None


@app.get("/api/documents/{i}")
def get_document(i: int):
    con = db()
    try:
        return doc_out(con, get_row(con, i))
    finally:
        con.close()


@app.patch("/api/documents/{i}")
def patch_document(i: int, p: DocPatch):
    con = db()
    try:
        get_row(con, i)
        if p.title is not None and p.title.strip():
            con.execute("UPDATE documents SET title=? WHERE id=?", (p.title.strip(), i))
        if p.category is not None:
            con.execute("UPDATE documents SET category_id=? WHERE id=?", (cat_id(con, p.category), i))
        if p.member is not None:
            con.execute("UPDATE documents SET member_id=? WHERE id=?", (member_id(con, p.member), i))
        for f in ("doc_date", "notes", "tags"):
            v = getattr(p, f)
            if v is not None:
                con.execute(f"UPDATE documents SET {f}=? WHERE id=?", (v.strip(), i))
        if p.expiry_date is not None:
            con.execute("UPDATE documents SET expiry_date=? WHERE id=?", (clean_expiry(p.expiry_date), i))
        r = get_row(con, i)
        for col, label in (("visa_type", type_label(r["category"])), ("visa_country", "Issuing country")):
            v = getattr(p, col) if getattr(p, col) is not None else r[col]
            if r["status"] == "current":
                v = clean_visa_field(r["category"], v, label)
            elif not has_visa_type(r["category"]):
                v = ""  # superseded history is never forced to gain these fields
            con.execute(f"UPDATE documents SET {col}=? WHERE id=?", ((v or "").strip()[:60], i))
        validity = clean_validity(r["category"], p.visa_validity if p.visa_validity is not None else r["visa_validity"])
        con.execute("UPDATE documents SET visa_validity=? WHERE id=?", (validity, i))
        if not r["expiry_date"] and validity:
            con.execute("UPDATE documents SET expiry_date=? WHERE id=?",
                        (expiry_from_validity(r["doc_date"], validity), i))
        if p.expiry_ack is not None:
            con.execute("UPDATE documents SET expiry_ack=? WHERE id=?", (int(p.expiry_ack and can_ack(r["category"])), i))
        r = get_row(con, i)
        if r["status"] == "current" and needs_expiry(r["category"], r["title"], r["tags"], r["filename"]):
            clean_expiry(r["expiry_date"], required=True)  # uncommitted changes are discarded on close
        index_doc(con, i)
        con.commit()
        return doc_out(con, get_row(con, i))
    finally:
        con.close()


@app.delete("/api/documents/{i}")
def delete_document(i: int):
    con = db()
    try:
        r = get_row(con, i)
        prev, nxt = r["supersedes_id"], r["superseded_by_id"]
        if prev:
            con.execute("UPDATE documents SET superseded_by_id=?, status=? WHERE id=?",
                        (nxt, "superseded" if nxt else "current", prev))
        if nxt:
            con.execute("UPDATE documents SET supersedes_id=? WHERE id=?", (prev, nxt))
        con.execute("DELETE FROM docs_fts WHERE rowid=?", (i,))
        con.execute("DELETE FROM documents WHERE id=?", (i,))
        con.commit()
        shutil.rmtree(FILES / str(i), ignore_errors=True)
        (CACHE / f"{i}.pdf").unlink(missing_ok=True)
        return {"ok": True}
    finally:
        con.close()


@app.get("/api/documents/{i}/history")
def document_history(i: int):
    con = db()
    try:
        get_row(con, i)
        head = i
        while True:
            p = con.execute("SELECT supersedes_id FROM documents WHERE id=?", (head,)).fetchone()["supersedes_id"]
            if not p:
                break
            head = p
        chain, cur = [], head
        while cur:
            r = get_row(con, cur)
            chain.append(doc_out(con, r))
            cur = r["superseded_by_id"]
        return chain
    finally:
        con.close()


@app.get("/api/documents/{i}/file")
def document_file(i: int, download: bool = False):
    con = db()
    try:
        r = get_row(con, i)
    finally:
        con.close()
    p = doc_path(r)
    if not p.exists():
        raise HTTPException(404, "File missing on disk")
    return FileResponse(p, media_type=r["mime"], filename=r["filename"],
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/documents/{i}/preview")
def document_preview(i: int):
    """Reader: PDFs/images/text stream as-is; Office files are rendered to PDF (cached)."""
    con = db()
    try:
        r = get_row(con, i)
    finally:
        con.close()
    p = doc_path(r)
    ext = p.suffix.lower()
    if ext == ".pdf" or ext in IMG_EXT or (r["mime"] or "").startswith("text/"):
        return FileResponse(p, media_type=r["mime"], filename=r["filename"], content_disposition_type="inline")
    try:
        pdf = as_pdf(i, p)
    except Exception:
        raise HTTPException(415, "No preview available for this file type. Use download.")
    return FileResponse(pdf, media_type="application/pdf", filename=f"{p.stem}.pdf",
                        content_disposition_type="inline")


@app.get("/api/documents/{i}/jpeg")
def document_jpeg(i: int, dpi: int = 200):
    """Each page as a JPEG: one page -> .jpg, several -> .zip of <title>_p01.jpg, _p02.jpg, ..."""
    con = db()
    try:
        r = get_row(con, i)
    finally:
        con.close()
    p = doc_path(r)
    if p.suffix.lower() in IMG_EXT:
        raise HTTPException(400, "This document is already an image. Use Download.")
    try:
        pdf = as_pdf(i, p)
    except Exception:
        raise HTTPException(415, "This file type cannot be converted to pages.")
    work = Path(tempfile.mkdtemp(dir=DATA))
    try:
        subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=90", "-r", str(max(72, min(dpi, 300))),
                        str(pdf), str(work / "page")], check=True, timeout=600, capture_output=True)
        pages = sorted(work.glob("page-*.jpg"), key=lambda f: int(f.stem.rsplit("-", 1)[1]))
        if not pages:
            raise HTTPException(422, "No pages could be rendered from this document.")
        base = safe(r["title"])
        cleanup = BackgroundTask(shutil.rmtree, work, ignore_errors=True)
        if len(pages) == 1:
            return FileResponse(pages[0], media_type="image/jpeg", filename=f"{base}.jpg", background=cleanup)
        zpath = work / f"{base}_pages.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # JPEGs are already compressed
            for n, f in enumerate(pages, 1):
                z.write(f, f"{base}_p{n:02d}.jpg")
        return FileResponse(zpath, media_type="application/zip", filename=zpath.name, background=cleanup)
    except subprocess.CalledProcessError:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(422, "Could not render this document's pages (it may be password protected).")
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ---------- binders (collections) ----------
def binder_items(con, cid: int):
    sql = BASE_SQL.replace("FROM documents d", "FROM collection_items ci JOIN documents d ON d.id=ci.document_id")
    rows = con.execute(sql + " WHERE ci.collection_id=? ORDER BY ci.position", (cid,)).fetchall()
    return [doc_out(con, r) for r in rows]


def get_binder(con, cid: int):
    r = con.execute("SELECT * FROM collections WHERE id=?", (cid,)).fetchone()
    if not r:
        raise HTTPException(404, "Binder not found")
    return r


@app.get("/api/collections")
def list_binders():
    con = db()
    rows = con.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM collection_items WHERE collection_id=c.id) AS count "
        "FROM collections c ORDER BY c.created_at DESC").fetchall()
    out = [dict(r) for r in rows]
    con.close()
    return out


@app.post("/api/collections")
def create_binder(b: NameBody):
    if not b.name.strip():
        raise HTTPException(400, "Name required")
    con = db()
    i = con.execute("INSERT INTO collections(name,notes,created_at) VALUES(?,?,?)",
                    (b.name.strip(), b.notes, datetime.now().isoformat(timespec="seconds"))).lastrowid
    con.commit()
    con.close()
    return {"id": i, "name": b.name.strip()}


@app.put("/api/collections/{cid}")
def update_binder(cid: int, b: NameBody):
    con = db()
    try:
        get_binder(con, cid)
        con.execute("UPDATE collections SET name=?, notes=? WHERE id=?", (b.name.strip(), b.notes, cid))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/collections/{cid}")
def delete_binder(cid: int):
    con = db()
    con.execute("DELETE FROM collections WHERE id=?", (cid,))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/collections/{cid}")
def binder_detail(cid: int):
    con = db()
    try:
        r = get_binder(con, cid)
        return {"id": r["id"], "name": r["name"], "notes": r["notes"], "items": binder_items(con, cid)}
    finally:
        con.close()


class DocRef(BaseModel):
    document_id: int


@app.post("/api/collections/{cid}/items")
def binder_add(cid: int, b: DocRef):
    con = db()
    try:
        get_binder(con, cid)
        get_row(con, b.document_id)
        pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM collection_items WHERE collection_id=?",
                          (cid,)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO collection_items VALUES(?,?,?)", (cid, b.document_id, pos))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


@app.delete("/api/collections/{cid}/items/{did}")
def binder_remove(cid: int, did: int):
    con = db()
    con.execute("DELETE FROM collection_items WHERE collection_id=? AND document_id=?", (cid, did))
    con.commit()
    con.close()
    return {"ok": True}


class Order(BaseModel):
    order: List[int]


@app.post("/api/collections/{cid}/reorder")
def binder_reorder(cid: int, b: Order):
    con = db()
    for pos, did in enumerate(b.order, 1):
        con.execute("UPDATE collection_items SET position=? WHERE collection_id=? AND document_id=?",
                    (pos, cid, did))
    con.commit()
    con.close()
    return {"ok": True}


class ExportReq(BaseModel):
    mode: str  # "pdf" | "folder"


@app.post("/api/collections/{cid}/export")
def binder_export(cid: int, b: ExportReq):
    if b.mode not in ("pdf", "folder"):
        raise HTTPException(400, "mode must be 'pdf' or 'folder'")
    con = db()
    try:
        binder = get_binder(con, cid)
        rows = [get_row(con, d["id"]) for d in binder_items(con, cid)]
    finally:
        con.close()
    if not rows:
        raise HTTPException(400, "Binder is empty")
    base = f"{safe(binder['name'])}_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    skipped = []
    if b.mode == "pdf":
        writer = PdfWriter()
        for r in rows:
            try:
                rd = PdfReader(str(as_pdf(r["id"], doc_path(r))))
                if rd.is_encrypted:
                    rd.decrypt("")
                start = len(writer.pages)
                for page in rd.pages:
                    writer.add_page(page)
                writer.add_outline_item(f"{r['title']} ({r['member']})" if r["member"] else r["title"], start)
            except Exception:
                skipped.append(r["title"])
        if not len(writer.pages):
            raise HTTPException(422, "None of the documents could be converted to PDF")
        out = EXPORTS / f"{base}.pdf"
        with open(out, "wb") as f:
            writer.write(f)
        files = [{"name": out.name, "url": f"/api/exports/{out.name}"}]
    else:
        folder = EXPORTS / base
        folder.mkdir()
        index = [f"Binder: {binder['name']}", f"Created: {datetime.now():%Y-%m-%d %H:%M}", ""]
        for n, r in enumerate(rows, 1):
            p = doc_path(r)
            name = f"{n:02d}_{safe(r['title'])}{p.suffix.lower()}"
            shutil.copy2(p, folder / name)
            index.append(f"{name}  <-  {r['filename']} ({r['member'] or 'Shared'} / {r['category'] or 'Uncategorised'})")
        (folder / "index.txt").write_text("\n".join(index) + "\n")
        zpath = EXPORTS / f"{base}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(folder.iterdir()):
                z.write(f, f"{base}/{f.name}")
        files = [{"name": zpath.name, "url": f"/api/exports/{zpath.name}"}]
    return {"files": files, "skipped": skipped, "saved_in": f"exports/{base}" + (".pdf" if b.mode == "pdf" else "")}


@app.get("/api/exports")
def list_exports():
    items = [p for p in EXPORTS.iterdir() if p.is_file()]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "url": f"/api/exports/{p.name}", "size": p.stat().st_size} for p in items[:30]]


@app.get("/api/exports/{name}")
def get_export(name: str):
    p = (EXPORTS / name).resolve()
    if p.parent != EXPORTS.resolve() or not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p, filename=p.name)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    # no-cache: browsers revalidate (cheap, via ETag) so an updated page is never served stale
    return FileResponse(Path(__file__).parent / "static" / "index.html", headers={"Cache-Control": "no-cache"})

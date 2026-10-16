# DocVault

Private document vault for your home server. Nothing leaves the machine: no cloud calls, no telemetry, no external fonts or scripts.

## Features
- Upload any file: PDF, Word, Excel, PowerPoint, images, text. Text is extracted and indexed for full-text search (scanned PDFs and photos go through OCR, English + Arabic by default).
- Search across title, file name, tags, notes and document contents. Prefix and stemmed matching ("passp" and "renew" work).
- Family: add each family member under **Family**. Every document belongs to one person or to **Shared** (household papers like tenancy, utilities, family insurance). The **Showing** selector in the header narrows the whole library to one person; the home page shows a tile per person. Member names are searchable ("priya passport").
- Categories: 11 defaults, add/rename/delete any time (or type a new name while uploading).
- Expiry: passports (including "PP" in a file name), leases, service contracts and rental/tenancy contracts (matched on title, tags or file name; list in `EXPIRY_REQUIRED` in `app/main.py`), plus everything in the **Visa & Immigration** category (`EXPIRY_REQUIRED_CATEGORIES`), require a full expiry date. Other documents can have an optional month/year expiry (treated as the last day of that month). The home page shows **Expiring soon**: separate counts and lists for Expired, within 1 month, 1–3 months and 3–6 months (each document in exactly one), and documents still missing a required expiry date.
- Visa type and issuing country: **Visa & Immigration** documents require both (pick a suggestion such as Residence / Golden / Tourist and a country from `app/countries.py`, or type your own; values you use are added to the suggestions). They show as a badge ("Golden visa · United Arab Emirates") and on the dashboard, carry over to a new version, and are searchable (`VISA_TYPES`, `VISA_TYPE_CATEGORIES`).
- Acknowledge as expired: expired **Visa & Immigration** documents have an *Acknowledge* tick box (on the Expired row of the home page, and in Edit). Ticked documents leave the Expired list and show an "Expired · acknowledged" badge in the library; untick in Edit to bring them back (`EXPIRY_ACK_CATEGORIES`).
- Supersede: "New version" replaces the current document; the old one is kept, hidden by default, visible via "Show superseded" and the History button.
- Binders: search, click "+ Binder" on each document, reorder, then export as a **combined PDF** (with bookmarks, Word/Excel/images converted) or as a **numbered folder + zip** with an index.txt.
- Reader: built-in viewer for PDFs, images, text; Office files are rendered to PDF for viewing.

## Deploy on ND-Homeserver
```bash
# copy this folder to the server, e.g. /mnt/media/docker/docvault-app
cd /mnt/media/docker/docvault-app
mkdir -p /mnt/media/docker/docvault/data
echo 'APP_PASSWORD=choose-a-password' > .env      # optional but recommended
docker compose up -d --build                       # first build downloads LibreOffice (~1 GB image)
```
### Access: Tailscale only
DocVault is **not** reachable on the LAN. `docker-compose.override.yml` binds the container to `127.0.0.1:8095`, and Caddy
(`/mnt/media/docker/compose/caddy/Caddyfile`) serves it at:

**https://nd-homeserver.tail2ca440.ts.net:3450**

Caddy also drops any client that is not on a Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48).
The Tailscale TLS cert in `/mnt/media/docker/caddy/certs` expires 2026-12-19; renew it with `tailscale cert` before then.
Do **not** use `tailscale funnel`, which publishes to the internet.

## Where things are stored
`/mnt/media/docker/docvault/data/`
- `files/<id>/<original name>`: your documents, untouched
- `vault.db`: metadata (including which family member each document belongs to) + search index (SQLite)
- `exports/`: binder outputs (also reachable over your Samba share if you share this folder)
- `cache/`: PDF renditions for preview; safe to delete

## Backup
Copy the whole data folder to `/mnt/backup`. For a consistent copy of the database while running:
```bash
sqlite3 /mnt/media/docker/docvault/data/vault.db ".backup '/mnt/backup/docvault/vault.db'"
rsync -a /mnt/media/docker/docvault/data/files/ /mnt/backup/docvault/files/
```

## Notes
- More OCR languages: add e.g. `tesseract-ocr-hin` to the Dockerfile and set `OCR_LANGS=eng+ara+hin`.
- HEIC/HEIF photos (iPhone default) are converted to JPG on upload so they can be OCR'd, previewed and merged into binder PDFs. The original HEIC is not kept.
- Deleting a document removes the file permanently; supersede instead if you want to keep history.
- Passwords: `APP_PASSWORD` enables HTTP basic auth (any username). With Tailscale-only access it is a second layer, not the only one.

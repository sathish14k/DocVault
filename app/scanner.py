"""Scanning from a network scanner over eSCL (AirScan), which most network printer-scanners since about 2015
speak: plain HTTP + XML, no drivers. Set SCANNER_URL to the scanner's address, e.g. http://192.168.0.110.

A scan job is POSTed to /eSCL/ScanJobs; the scanner answers with the job's URL, and each page is then
fetched from <job>/NextDocument until it answers 404 (a flatbed gives one page per job).
"""
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

SCANNER_URL = os.environ.get("SCANNER_URL", "").strip().rstrip("/")
COLORS = {"color": "RGB24", "gray": "Grayscale8"}
PAPER = {"A4": (2480, 3508), "Letter": (2550, 3300)}  # in 1/300 inch, as eSCL scan regions are given
_lock = threading.Lock()  # the scanner does one job at a time
_caps: Optional[dict] = None


class ScannerError(Exception):
    pass


def _open(path: str, data: Optional[bytes] = None, timeout: float = 10):
    url = path if path.startswith("http") else SCANNER_URL + path
    req = urllib.request.Request(url, data, {"Content-Type": "text/xml"} if data else {},
                                 method="POST" if data else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _tags(xml: str, tag: str) -> list:
    return re.findall(rf"<(?:\w+:)?{tag}>([^<]*)<", xml)


def capabilities() -> dict:
    """Model, resolutions and glass size, read once from the scanner."""
    global _caps
    if _caps is None:
        xml = _open("/eSCL/ScannerCapabilities").read().decode("utf8", "ignore")
        platen = xml.split("<scan:Platen>")[1].split("</scan:Platen>")[0] if "<scan:Platen>" in xml else xml
        model = next((m for m in _tags(xml, "MakeAndModel") if m.strip("x ")), "")
        if not model:  # HP printers mask it here; their product page has it
            try:
                model = _tags(_open("/DevMgmt/ProductConfigDyn.xml", timeout=5).read().decode("utf8", "ignore"),
                              "MakeAndModel")[0]
            except Exception:
                model = "Network scanner"
        _caps = {
            "model": model.strip(),
            "resolutions": sorted({int(r) for r in _tags(platen, "XResolution")}),
            "max": (int(_tags(platen, "MaxWidth")[0]), int(_tags(platen, "MaxHeight")[0])),
            "adf": "<scan:Adf>" in xml,
        }
    return _caps


def status() -> dict:
    if not SCANNER_URL:
        return {"configured": False}
    try:
        caps = capabilities()
        state = (_tags(_open("/eSCL/ScannerStatus", timeout=5).read().decode("utf8", "ignore"), "State") or ["?"])[0]
        return {"configured": True, "online": True, "state": state, "model": caps["model"],
                "resolutions": [r for r in caps["resolutions"] if 150 <= r <= 600], "paper": list(PAPER)}
    except Exception as e:
        return {"configured": True, "online": False, "error": f"Scanner not reachable ({e.__class__.__name__}). "
                                                              "Is it switched on and awake?"}


def scan_page(color: str = "color", dpi: int = 300, paper: str = "A4") -> bytes:
    """Scan one page from the glass and return it as JPEG."""
    if not SCANNER_URL:
        raise ScannerError("No scanner configured (SCANNER_URL)")
    caps = capabilities()
    if dpi not in caps["resolutions"]:
        raise ScannerError(f"Resolution must be one of {caps['resolutions']}")
    w, h = PAPER.get(paper, PAPER["A4"])
    w, h = min(w, caps["max"][0]), min(h, caps["max"][1])
    settings = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:ScanRegions><pwg:ScanRegion><pwg:Height>{h}</pwg:Height><pwg:Width>{w}</pwg:Width>
    <pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>
    <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits></pwg:ScanRegion></pwg:ScanRegions>
  <pwg:InputSource>Platen</pwg:InputSource>
  <scan:ColorMode>{COLORS.get(color, "RGB24")}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution><scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat><scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>
  <scan:Intent>Document</scan:Intent>
</scan:ScanSettings>""".encode()
    if not _lock.acquire(timeout=1):
        raise ScannerError("The scanner is busy with another scan")
    try:
        for attempt in range(4):  # 503: still finishing a previous job
            try:
                job = _open("/eSCL/ScanJobs", settings, timeout=30).headers["Location"]
                break
            except urllib.error.HTTPError as e:
                if e.code != 503 or attempt == 3:
                    raise ScannerError(f"The scanner refused the scan (HTTP {e.code})")
                time.sleep(2)
        next_url = job.rstrip("/") + "/NextDocument"
        try:
            page = _open(next_url, timeout=300).read()
        except urllib.error.HTTPError as e:
            raise ScannerError(f"The scanner did not return a page (HTTP {e.code})")
        try:
            _open(next_url, timeout=30).read()  # ends the job (404 once the page is taken)
        except Exception:
            pass
        return page
    except urllib.error.URLError as e:
        raise ScannerError(f"Scanner not reachable: {e.reason}")
    finally:
        _lock.release()

"""Job board adapters. Every adapter returns a list of normalized dicts."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

UA = {"User-Agent": "jobradar/0.1 (personal job tracker)"}
TIMEOUT = 25


def _job_id(company: str, title: str, url: str) -> str:
    return hashlib.sha1(f"{company}|{title}|{url}".encode()).hexdigest()[:16]


def _norm(company, title, url, location="", posted_at=None, description="", source="",
          domain=""):
    return {
        "id": _job_id(company, title, url),
        "company": company,
        "title": (title or "").strip(),
        "url": url,
        "location": (location or "").strip(),
        "posted_at": posted_at,          # epoch seconds, or None if the board hides it
        "description": (description or "")[:6000],
        "source": source,
        "domain": domain,
    }


def _get(url: str) -> Any:
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _epoch(value) -> float | None:
    """Boards return ms ints, ISO strings, or nothing. Normalize to epoch seconds."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        from datetime import datetime
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Per-company boards
# --------------------------------------------------------------------------

def greenhouse(name: str, slug: str) -> list[dict]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_norm(name, j.get("title"), j.get("absolute_url"), loc,
                         _epoch(j.get("updated_at") or j.get("first_published")),
                         j.get("content", ""), "greenhouse"))
    return out


def lever(name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append(_norm(name, j.get("text"), j.get("hostedUrl"),
                         cats.get("location", ""), _epoch(j.get("createdAt")),
                         j.get("descriptionPlain", ""), "lever"))
    return out


def ashby(name: str, slug: str) -> list[dict]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(name, j.get("title"), j.get("jobUrl"), j.get("location", ""),
                         _epoch(j.get("publishedAt")), j.get("descriptionPlain", ""), "ashby"))
    return out


def workable(name: str, slug: str) -> list[dict]:
    data = _get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(name, j.get("title"), j.get("url" ) or j.get("application_url"),
                         j.get("location", ""), _epoch(j.get("published_on")),
                         j.get("description", ""), "workable"))
    return out


BOARDS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable}


def fetch_company(entry: dict) -> tuple[list[dict], str | None]:
    """Returns (jobs, error). Never raises - one dead board must not kill the run."""
    board = entry.get("board", "greenhouse").lower()
    fn = BOARDS.get(board)
    if not fn:
        return [], f"unknown board '{board}'"
    try:
        jobs = fn(entry["name"], entry["slug"])
        for j in jobs:
            j["domain"] = entry.get("domain", "")
        return jobs, None
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        reason = f"HTTP {status}" if status is not None else "HTTP error"
    except requests.exceptions.Timeout:
        reason = "request timed out"
    except requests.exceptions.JSONDecodeError:
        reason = "invalid JSON response"
    except requests.exceptions.RequestException:
        reason = "network request failed"
    except Exception as e:  # noqa: BLE001
        # Keep unexpected failures diagnosable without echoing URLs, response
        # bodies, or other values that may contain sensitive information.
        reason = f"unexpected {type(e).__name__}"

    logger.warning(
        "ATS fetch failed company=%s source=%s operation=fetch reason=%s",
        entry.get("name", "?"), board, reason,
    )
    return [], f"source={board} operation=fetch reason={reason}"


# --------------------------------------------------------------------------
# Aggregators (keyless, for resume-driven discovery)
# --------------------------------------------------------------------------

def remotive(query: str, limit: int = 40) -> list[dict]:
    data = _get(f"https://remotive.com/api/remote-jobs?search={requests.utils.quote(query)}&limit={limit}")
    return [
        _norm(j.get("company_name", "?"), j.get("title"), j.get("url"),
              j.get("candidate_required_location", "Remote"),
              _epoch(j.get("publication_date")), j.get("description", ""), "remotive")
        for j in data.get("jobs", [])[:limit]
    ]


def arbeitnow(query: str, limit: int = 40) -> list[dict]:
    data = _get("https://www.arbeitnow.com/api/job-board-api")
    q = query.lower()
    out = []
    for j in data.get("data", []):
        hay = f"{j.get('title','')} {' '.join(j.get('tags') or [])}".lower()
        if q in hay:
            out.append(_norm(j.get("company_name", "?"), j.get("title"), j.get("url"),
                             j.get("location", ""), _epoch(j.get("created_at")),
                             j.get("description", ""), "arbeitnow"))
        if len(out) >= limit:
            break
    return out


def discover(queries: list[str], max_per_query: int = 40) -> tuple[list[dict], list[str]]:
    jobs, errors = [], []
    for q in queries:
        for fn in (remotive, arbeitnow):
            try:
                jobs.extend(fn(q, max_per_query))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{fn.__name__}({q}): {type(e).__name__}: {e}")
            time.sleep(0.6)   # be polite to free endpoints
    return jobs, errors

# --------------------------------------------------------------------------
# Enterprise boards. Large companies rarely use Greenhouse/Lever; most sit on
# Workday or Oracle Recruiting Cloud, each with a public JSON endpoint.
# --------------------------------------------------------------------------

def _post(url: str, body: dict) -> Any:
    r = requests.post(url, headers={**UA, "Content-Type": "application/json",
                                    "Accept": "application/json"},
                      json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def workday(name: str, slug: str) -> list[dict]:
    """slug is 'tenant/wdN/site', e.g. 'adobe/wd5/external_experienced'."""
    tenant, wd, site = slug.split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out: list[dict] = []
    # Workday pages 20 at a time and caps hard; a few pages is plenty per run.
    for offset in range(0, 200, 20):
        data = _post(api, {"appliedFacets": {}, "limit": 20,
                           "offset": offset, "searchText": ""})
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for j in postings:
            path = j.get("externalPath") or ""
            out.append(_norm(
                name, j.get("title"), f"{base}/{site}{path}",
                j.get("locationsText", ""), None,
                j.get("bulletFields") and " ".join(j["bulletFields"]) or "", "workday"))
        if len(postings) < 20:
            break
    return out


def oracle_cloud(name: str, slug: str) -> list[dict]:
    """slug is 'host/siteNumber', e.g. 'eeho.fa.us2.oraclecloud.com/CX_1'."""
    host, site = slug.split("/", 1)
    out: list[dict] = []
    # requisitionList is only returned when explicitly expanded.
    for offset in (0, 200, 400):
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={site},limit=200,offset={offset}")
        items = (_get(url).get("items") or [{}])[0]
        reqs = items.get("requisitionList") or []
        if not reqs:
            break
        for j in reqs:
            rid = j.get("Id") or ""
            out.append(_norm(name, j.get("Title"),
                             f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
                             j.get("PrimaryLocation") or "", _epoch(j.get("PostedDate")),
                             j.get("ShortDescriptionStr") or "", "oracle"))
        if len(reqs) < 200:
            break
    return out


def atlassian_board(name: str, slug: str) -> list[dict]:
    """Atlassian publishes its own JSON feed rather than using a hosted board."""
    data = _get("https://www.atlassian.com/endpoint/careers/listings")
    out = []
    for j in data:
        portal = j.get("portalJobPost") or {}
        locs = j.get("locations") or []
        out.append(_norm(name, j.get("title"), portal.get("portalUrl"),
                         "; ".join(locs[:2]), _epoch(portal.get("updatedDate")),
                         re.sub(r"<[^>]+>", " ", j.get("overview") or ""), "atlassian"))
    return out


BOARDS.update({"workday": workday, "oracle": oracle_cloud, "atlassian": atlassian_board})

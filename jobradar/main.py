"""jobradar CLI.

  scan     fetch every source, diff against the DB, score new jobs
  digest   email everything not yet reported (this is what cron runs weekly)
  run      scan + digest in one shot
  list     print recent finds in the terminal
  test     send a sample email to prove SMTP works
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from pathlib import Path

import yaml

from . import alerts as alerts_mod
from . import digest as digest_mod
from . import sources
from .budget import Budget
from .cursor import CursorError
from .llm import RateLimited, make_scorer
from .localmatch import LocalScorer
from .resume import build_profile, extract_text
from .relevance import apply_cap as apply_relevance_cap
from .seniority import apply_cap
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
PROFILE_CACHE = ROOT / ".resume_profile.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No config at {path}. Copy config.example.yaml to config.yaml and edit it.")
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        sys.exit(
            f"Config at {path} is empty or is not a YAML mapping. "
            "Copy config.example.yaml to config.yaml and edit it."
        )
    return config


def get_scorer(cfg: dict):
    spec = cfg.get("llm", {})
    provider = spec.get("provider", "none")
    if provider == "local":
        # The keyword matcher works off the resume itself, not a model name.
        path = Path(cfg["resume_path"])
        return make_scorer("local", extract_text(path if path.is_absolute()
                                                 else ROOT / path))
    return make_scorer(provider, spec.get("model", ""), spec.get("model_params"))


def get_profile(cfg: dict, scorer, force: bool = False) -> str:
    resume_path = ROOT / cfg["resume_path"] if not Path(cfg["resume_path"]).is_absolute() \
        else Path(cfg["resume_path"])
    stamp = resume_path.stat().st_mtime if resume_path.exists() else 0
    summarizer = scorer if (scorer is not None and hasattr(scorer, "complete")) else None
    # The cache key includes whether a model wrote it. Without this, a raw-text
    # profile written during a `provider: none` run gets reused by a later
    # model run - and raw text truncates mid-resume, hiding the experience
    # section that says how senior the candidate actually is.
    kind = "model" if summarizer else "raw"
    if PROFILE_CACHE.exists() and not force:
        cached = json.loads(PROFILE_CACHE.read_text())
        if cached.get("mtime") == stamp and cached.get("kind") == kind:
            return cached["profile"]
    text = extract_text(resume_path)
    try:
        profile = build_profile(text, summarizer)
    except Exception:  # noqa: BLE001
        # Out of quota or provider down. A summary is a nicety; the run's job
        # is finding openings. Fall back to the resume text and do not cache
        # it, so a proper summary is built once the provider recovers.
        return build_profile(text, None)
    PROFILE_CACHE.write_text(json.dumps({"mtime": stamp, "kind": kind, "profile": profile}))
    return profile


def passes_filters(job: dict, f: dict) -> bool:
    title = job["title"].lower()
    inc = [s.lower() for s in f.get("title_include") or []]
    exc = [s.lower() for s in f.get("title_exclude") or []]
    locs = [s.lower() for s in f.get("locations") or []]
    if inc and not any(s in title for s in inc):
        return False
    if any(s in title for s in exc):
        return False
    if locs and not any(s in job["location"].lower() for s in locs):
        return False
    return True


# ---------------------------------------------------------------------------

def cmd_scan(cfg: dict, store: Store, quiet: bool = False) -> dict:
    all_jobs: list[dict] = []
    errors: list[str] = []
    healthy_companies: list[str] = []

    for entry in cfg.get("companies", []):
        jobs, err = sources.fetch_company(entry)
        if err:
            errors.append(f"{entry['name']}: {err}")
        else:
            healthy_companies.append(entry["name"])
            all_jobs.extend(jobs)
        if not quiet:
            print(f"  {entry['name']:<20} {len(jobs):>4} open" + (f"  ⚠ {err}" if err else ""),
                  flush=True)

    disc = cfg.get("discovery", {})
    if disc.get("enabled"):
        jobs, errs = sources.discover(disc.get("queries", []), disc.get("max_per_query", 40))
        all_jobs.extend(jobs)
        errors.extend(errs)
        if not quiet:
            print(f"  {'discovery':<20} {len(jobs):>4} found")

    kept = [j for j in all_jobs if passes_filters(j, cfg.get("filters", {}))]
    # keep descriptions around for scoring, keyed by id
    desc = {j["id"]: j["description"] for j in kept}

    new = store.upsert(kept)
    closed = store.mark_closed(healthy_companies)
    # Aggregator listings are never absent-confirmed, so they expire on age.
    stale_days = int(cfg.get("discovery", {}).get("expire_after_days", 14))
    closed += store.close_stale(["remotive", "arbeitnow"], stale_days)

    # --- score the new arrivals -------------------------------------------
    llm_cfg = cfg.get("llm", {})
    scored = 0
    # Gate on there being unscored work, NOT on this run finding new jobs: a run
    # that was rate-limited leaves a backlog, and the next run must clear it even
    # if no new postings appeared.
    if llm_cfg.get("provider", "none") != "none":
        rows = []
        scorer = None
        profile = ""
        pending = store.unscored(llm_cfg.get("max_to_score", 5000))
        if pending:
            try:
                scorer = get_scorer(cfg)
                profile = get_profile(cfg, scorer)
            except Exception as e:  # noqa: BLE001
                # No scoring available. New and closed openings are still
                # detected and reported; they simply arrive unranked.
                errors.append(f"scoring unavailable: {type(e).__name__}")
                if not quiet:
                    print(f"  scoring unavailable ({type(e).__name__}); "
                          f"reporting openings unranked", flush=True)
                scorer = None
                pending = []

            # --- stage 1: free triage ---------------------------------------
            # A posting with no overlapping skills and a senior title is not a
            # judgement call. Settle those locally and spend the token budget
            # on the ones where the model's reading actually changes the
            # answer. Roughly halves what reaches the API.
            floor = int(llm_cfg.get("triage_floor", 3))
            if llm_cfg.get("triage", True) and not isinstance(scorer, LocalScorer):
                local = LocalScorer(extract_text(ROOT / cfg["resume_path"]))
                rows, settled, blind = [], 0, 0
                for row in pending:
                    job = dict(row)
                    job["description"] = desc.get(row["id"]) or row["description"] or ""
                    # Triage judges skill overlap, and a title alone rarely
                    # names a stack. Without a description it cannot tell
                    # "Software Engineer, Model Runtime" from "Head of Sales",
                    # so those go to the model rather than being guessed at.
                    if len(job["description"]) < 120:
                        rows.append(row)
                        blind += 1
                        continue
                    s_local, why, bd = local.score(profile, job)
                    if s_local >= floor:
                        rows.append(row)
                    else:
                        store.save_score(row["id"], s_local, f"triage: {why}", bd)
                        settled += 1
                if not quiet:
                    note = f", {blind} had no description to judge" if blind else ""
                    print(f"  triage settled {settled} locally, "
                          f"{len(rows)} go to the model{note}", flush=True)
            else:
                rows = pending

            # --- stage 2: stay inside the daily budget ----------------------
            budget = Budget(store.db, int(llm_cfg.get("daily_token_limit", 500_000)))
            size = int(llm_cfg.get("batch_size", 25))
            per_batch = int(llm_cfg.get("tokens_per_batch", 3400))
            affordable = max(0, budget.remaining() // per_batch) * size
            if len(rows) > affordable:
                if not quiet:
                    print(f"  budget: {budget.report()} -> planning {affordable} "
                          f"of {len(rows)} this run", flush=True)
                rows = rows[:affordable]
            elif not quiet:
                print(f"  budget: {budget.report()}", flush=True)

        # Scoring is network-bound and each job is independent, so run a small
        # pool. Kept modest on purpose: free tiers rate-limit, and the client
        # already backs off on 429. SQLite writes stay on this thread.
        # A batched backend (Cursor) scores many jobs per call, so the thread
        # pool would only multiply expensive round trips. Batch when offered.
        titles = {r["id"]: r["title"] for r in pending}

        def apply(job_id, result):
            nonlocal scored
            score, reason, breakdown = result
            # Two gates, both guaranteed in code rather than asked for in the
            # prompt: a senior role cannot ride a strong skills match, and a
            # wrong-field role cannot ride a strong seniority match.
            score, reason = apply_cap(score, titles.get(job_id, ""), reason)
            score, reason = apply_relevance_cap(score, breakdown, reason)
            store.save_score(job_id, score, reason, breakdown)
            scored += 1

        try:
            if hasattr(scorer, "score_batch"):
                size = int(llm_cfg.get("batch_size", getattr(scorer, "batch_size", 25)))

                def run_chunk(chunk):
                    payload = []
                    for row in chunk:
                        job = dict(row)
                        job["description"] = desc.get(row["id"], "")
                        payload.append(job)
                    return scorer.score_batch(profile, payload)

                dropped = []
                spent_before = getattr(scorer, "tokens_used", 0)
                for start in range(0, len(rows), size):
                    # The upfront plan uses an estimate; this is the real gate.
                    # Checked before every request, so a batch that costs more
                    # than expected can never carry the run past the cap.
                    if not budget.can_afford(per_batch):
                        if not quiet:
                            print(f"    budget reached - {budget.report()}; "
                                  f"{len(rows) - start} jobs wait for the next run",
                                  flush=True)
                        break
                    chunk = rows[start:start + size]
                    try:
                        results = run_chunk(chunk)
                    except (RateLimited, CursorError):
                        raise            # provider-level: stop, keep what we have
                    except Exception as e:  # noqa: BLE001
                        # A single unusable reply must not end the run. Those
                        # jobs stay unscored and are retried below.
                        if not quiet:
                            print(f"    batch failed ({type(e).__name__}), "
                                  f"deferring {len(chunk)}", flush=True)
                        dropped.extend(chunk)
                        continue
                    finally:
                        # Record actual spend immediately, so the next check
                        # sees the true figure rather than the estimate.
                        now_used = getattr(scorer, "tokens_used", 0)
                        if now_used > spent_before:
                            budget.record(now_used - spent_before)
                            spent_before = now_used
                    for row in chunk:
                        got = results.get(row["id"])
                        if got is None:
                            # The model skipped this row. Collect it rather than
                            # writing a zero, and retry in smaller groups after.
                            dropped.append(row)
                        else:
                            apply(row["id"], got)
                    if not quiet:
                        print(f"    scored {scored}/{len(rows)}"
                              + (f" ({len(dropped)} dropped)" if dropped else ""), flush=True)

                # Models reliably drop a row or two from a long array. One retry
                # in smaller groups recovers nearly all of them; anything still
                # missing stays unscored and is picked up by the next run.
                if dropped:
                    if not quiet:
                        print(f"    retrying {len(dropped)} dropped", flush=True)
                    for start in range(0, len(dropped), 8):
                        if not budget.can_afford(per_batch):
                            break
                        chunk = dropped[start:start + 8]
                        try:
                            results = run_chunk(chunk)
                        except Exception:  # noqa: BLE001 - retry is best effort
                            break
                        finally:
                            now_used = getattr(scorer, "tokens_used", 0)
                            if now_used > spent_before:
                                budget.record(now_used - spent_before)
                                spent_before = now_used
                        for row in chunk:
                            got = results.get(row["id"])
                            if got is not None:
                                apply(row["id"], got)
            else:
                workers = int(llm_cfg.get("concurrency", 3))

                def work(row):
                    job = dict(row)
                    job["description"] = desc.get(row["id"], "")
                    return row, scorer.score(profile, job)

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for row, result in pool.map(work, rows):
                        apply(row["id"], result)
                        if not quiet:
                            print(f"    scored {result[0]}/10  {row['title'][:55]}", flush=True)
        except (RateLimited, CursorError) as e:
            # Not fatal. Scores already written are committed, and whatever was
            # not reached stays unscored so the next run picks it up.
            errors.append(f"scoring stopped early: {e}")
            if not quiet:
                print(f"    ! {e} - {scored} scored, rest will wait for the next run",
                      flush=True)

    store.log_run(len(new), len(closed), errors)
    if not quiet:
        cost = ""
        if scored and scorer is not None and hasattr(scorer, "tokens_used"):
            cost = f" · {scorer.tokens_used:,} tokens"
        elif scored and scorer is not None and hasattr(scorer, "cost_usd"):
            cost = f" · ${scorer.cost_usd():.4f}"
        print(f"\n{len(new)} new · {len(closed)} closed · {scored} scored · "
              f"{len(errors)} errors{cost}")
    return {"new": len(new), "closed": len(closed), "errors": errors}


def cmd_digest(cfg: dict, store: Store, days: int, dry_run: bool) -> None:
    llm_cfg = cfg.get("llm", {})
    min_score = llm_cfg.get("min_score", 0)
    scoring_on = llm_cfg.get("provider", "none") not in ("none", None, "")
    pending = store.unnotified(min_score, require_scored=scoring_on)
    closed = store.closed_since(days * 86400)

    # Cap what one email shows. The first run after a fresh scan has the entire
    # board pending - thousands of jobs - and mailing all of them is useless.
    # Everything pending is still marked notified, so this run establishes the
    # baseline and later emails carry only genuinely new postings.
    cap = int(cfg.get("digest", {}).get("max_jobs", 60))
    new = pending[:cap]
    overflow = len(pending) - len(new)
    label = time.strftime("Week of %d %b %Y")
    notes = []
    if overflow > 0:
        notes.append(f"Showing the top {len(new)} of {len(pending)} tracked openings; "
                     f"{overflow} more are on file. Later emails carry only new ones.")
    html, text = digest_mod.render(new, closed, label, notes)

    if dry_run:
        _ = notes
        out = ROOT / "digest_preview.html"
        out.write_text(html)
        print(text)
        print(f"\nPreview written to {out}")
        return
    if not pending and not closed:
        print("Nothing to report; no email sent.")
        return

    count = len(pending)
    subject = f"Job Radar — {count} new opening{'s' if count != 1 else ''}"
    digest_mod.send(cfg["email"], subject, html, text)
    store.mark_notified([r["id"] for r in pending])   # all of them, not just the shown ones
    print(f"Sent to {cfg['email']['to']}: {len(new)} shown of {count} new, "
          f"{len(closed)} closed.")


def cmd_alert(cfg: dict, store: Store, dry_run: bool) -> None:
    """Check only the prioritised companies, then mail anything new at once.

    This runs daily, so it deliberately does NOT do a full scan: it fetches
    just the watched boards and skips aggregator discovery. A dozen companies
    is seconds of network and a handful of scoring batches, against minutes and
    ~130 batches for the whole board.
    """
    watch = alerts_mod.watched_names(cfg)
    narrow = dict(cfg)
    narrow["companies"] = [e for e in cfg.get("companies", []) if e["name"] in watch]
    narrow["discovery"] = {"enabled": False}
    cmd_scan(narrow, store, quiet=True)
    baselined = store.baselined_companies()
    pending = store.unalerted()
    rows = alerts_mod.pick(pending, cfg, baselined)
    fresh = {r["company"] for r in pending} - baselined
    if fresh and not dry_run:
        print(f"Baselining {len(fresh)} new compan(y/ies): "
              f"{', '.join(sorted(fresh))[:100]} — alerts start from their next opening.")
    if not rows:
        print("Nothing new at watched companies.")
        # Everything seen this run becomes the new baseline either way, so a
        # job that was below threshold is not re-examined forever.
        store.mark_alerted([r["id"] for r in store.unalerted()])
        return
    cap = int(cfg.get("alerts", {}).get("max_per_alert", 15))
    label = time.strftime("%d %b %Y, %H:%M")
    if dry_run:
        html, text = alerts_mod.render(rows[:cap], label)
        Path(ROOT / "alert_preview.html").write_text(html)
        print(text or "(nothing)")
        print(f"\n{len(rows)} would alert; preview at alert_preview.html")
        return
    alerts_mod.send(cfg, rows[:cap], label)
    store.mark_alerted([r["id"] for r in store.unalerted()])
    print(f"Alerted {len(rows)} new opening(s) at watched companies.")


def cmd_recap(store: Store, dry_run: bool) -> None:
    """Re-apply the seniority and relevance caps to existing scores.

    Costs nothing: both are arithmetic on data already stored, so they need no
    model. Use after changing a threshold, or to repair scores written before
    a gate existed.
    """
    rows = store.db.execute(
        "SELECT id, title, score, reason, breakdown FROM jobs WHERE score IS NOT NULL"
    ).fetchall()
    changed = []
    for r in rows:
        try:
            bd = json.loads(r["breakdown"]) if r["breakdown"] else {}
        except (TypeError, ValueError):
            bd = {}
        new_score, new_reason = apply_cap(r["score"], r["title"], r["reason"] or "")
        new_score, new_reason = apply_relevance_cap(new_score, bd, new_reason)
        if new_score != r["score"]:
            changed.append((r["id"], new_score, new_reason, r["title"], r["score"]))
    if dry_run:
        for _, ns, _, title, old in changed[:15]:
            print(f"  {old} -> {ns}  {title[:60]}")
        print(f"\n{len(changed)} of {len(rows)} scores would change (0 tokens)")
        return
    for jid, ns, nr, _, _ in changed:
        store.db.execute("UPDATE jobs SET score = ?, reason = ? WHERE id = ?", (ns, nr, jid))
    store.db.commit()
    print(f"Capped {len(changed)} jobs (senior title or wrong field). No tokens used.")


def cmd_list(store: Store, days: int) -> None:
    rows = store.since(days * 86400)
    if not rows:
        print("Nothing new.")
        return
    for r in rows:
        s = f"{r['score']}/10" if r["score"] is not None else "  - "
        print(f"[{s}] {r['title'][:60]:<60} {r['company'][:18]:<18} {r['url']}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="jobradar")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    d = sub.add_parser("digest"); d.add_argument("--days", type=int, default=7)
    d.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run"); r.add_argument("--days", type=int, default=7)
    r.add_argument("--dry-run", action="store_true")
    l = sub.add_parser("list"); l.add_argument("--days", type=int, default=7)
    a = sub.add_parser("alert"); a.add_argument("--dry-run", action="store_true")
    rc = sub.add_parser("recap"); rc.add_argument("--dry-run", action="store_true")
    sub.add_parser("test")
    args = p.parse_args(argv)

    cfg = load_config(Path(args.config))
    store = Store(ROOT / "jobradar.db")

    if args.cmd == "scan":
        cmd_scan(cfg, store)
    elif args.cmd == "digest":
        cmd_digest(cfg, store, args.days, args.dry_run)
    elif args.cmd == "run":
        cmd_scan(cfg, store)
        cmd_digest(cfg, store, args.days, args.dry_run)
    elif args.cmd == "list":
        cmd_list(store, args.days)
    elif args.cmd == "recap":
        cmd_recap(store, args.dry_run)
    elif args.cmd == "alert":
        cmd_alert(cfg, store, args.dry_run)
    elif args.cmd == "test":
        digest_mod.send(cfg["email"], "Job Radar — test",
                        "<p>SMTP works. Your weekly digest will arrive here.</p>",
                        "SMTP works.")
        print(f"Test email sent to {cfg['email']['to']}.")


if __name__ == "__main__":
    main()

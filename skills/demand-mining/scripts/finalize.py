"""Local ownership of run identity, current artifacts, receipts and completion.

An attempt can fail after any write. The run plan is immutable across retries;
an ambiguous delivery is never retried automatically. Reconciliation supplies the
actual adapter receipt through reconcile(), bound to the saved content and run.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from data_safety import atomic_json, data_root, file_lock, require_private
from lib import iso, now_utc, parse_ts
from redact import safe_data, safe_text
import dedup as dd
import digest as dg
import push_card as pc
from verify_gate import gate_batch


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def logical_identity(cfg, supplied=None, *, dry_run=False):
    if supplied is not None and not isinstance(supplied, dict):
        raise ValueError("run identity must be an object")
    zone = cfg.get("timezone")
    product = cfg.get("product_id") or cfg.get("slug")
    if dry_run:
        zone, product = zone or "UTC", product or "uninitialized-preview"
    if not isinstance(zone, str) or not zone:
        raise ValueError("configure an IANA timezone before a real run (for example UTC)")
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {zone}") from exc
    if not isinstance(product, str) or not product.strip():
        raise ValueError("configure product_id before a real run")
    try:
        day = date.fromisoformat(supplied["date"]) if supplied else now_utc().astimezone(tz).date()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run identity needs an ISO local date") from exc
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = now_utc()
    if end <= start:
        start -= timedelta(days=1)
    window = (supplied or {}).get("source_window") or cfg.get("source_window") or {
        "start": iso(start), "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    if not isinstance(window, dict) or not window:
        raise ValueError("source_window must declare start/end or a cursor")
    if "start" in window or "end" in window:
        if not {"start", "end"} <= window.keys():
            raise ValueError("source_window requires both start and end")
        if parse_ts(window["start"]) >= parse_ts(window["end"]):
            raise ValueError("source_window start must precede end")
    elif not isinstance(window.get("cursor"), str) or not window["cursor"]:
        raise ValueError("source_window requires a nonempty cursor")
    result = {"product_id": product, "timezone": zone, "date": day.isoformat(),
              "source_window": window}
    if supplied is not None and supplied != result:
        raise ValueError("handoff identity does not match the current configured logical run")
    return result


def _collection(value, candidates, identity, dry_run):
    if value is None:
        if not candidates and not dry_run:
            raise ValueError("empty input requires successful collection and classification evidence")
        return {"status": "complete", "classification": "complete",
                "source_window": identity["source_window"], "input": "explicit candidates",
                "candidate_count": len(candidates)}
    if not isinstance(value, dict) or value.get("status") != "complete":
        raise ValueError("collection did not complete successfully")
    classification = value.get("classification")
    if classification not in ("complete", "completed", True):
        raise ValueError("classification did not complete successfully")
    if value.get("source_window") != identity["source_window"]:
        raise ValueError("collection source_window differs from the logical run")
    if value.get("errors") or value.get("failed_sources"):
        raise ValueError("collection reports unresolved source failures")
    return value


def _prepare(candidates, cfg, ledger, identity, run_id):
    from run import build_card, merge_candidates
    cards = [build_card(candidate, cfg, run_id) for candidate in merge_candidates(candidates)]
    if ledger is not None:
        ledger.run_identity = identity
    rows = ledger.list_active() if ledger is not None else []
    if not isinstance(rows, list):
        raise ValueError("ledger list_active did not return a list")
    rows = dd.product_rows(rows, identity["product_id"])
    new, resurfaced, suppressed, merges = [], [], [], []
    for card in cards:
        band = dd.in_candidate_band(card, rows, cfg)
        if band is not None:
            merges.append({"title": card["title"], "with": dd._row_key(band)})
        match = dd.match_existing(card, rows, cfg)
        decision = dd.decide(card, match, cfg)
        card["_branch"], card["_dedup_delta"] = decision["branch"], decision["delta"]
        prior = dd._row_ext(match) if match else {}
        card["first_seen"] = prior.get(dd.EXT + "first_seen")
        card["push_count"] = int(prior.get(dd.EXT + "push_count", 0))
        if decision["branch"] == dd.SUPPRESS:
            suppressed.append(card)
        elif decision["branch"] == dd.RESURFACE:
            resurfaced.append(card)
        else:
            new.append(card)
    actionable = new + resurfaced
    gate = gate_batch(actionable, cfg)
    pushable, archivable = gate["pushable"], gate["archivable"]
    # Validate suppressed observations too before they can enter the pool.
    suppressed_gate = gate_batch(suppressed, cfg)
    mutations = []
    for card in actionable + suppressed_gate["archivable"]:
        if card in actionable and card not in archivable:
            continue
        previous = dd.match_existing(card, rows, cfg)
        ext = dd.build_ext(card, dd._row_ext(previous) if previous else {}, cfg)
        ext[dd.EXT + "identity"] = identity
        ext[dd.EXT + "run_id"] = run_id
        priority = 1 if card.get("tier") == "tier0" else max(1, min(9, 10 - int(
            round(float(card.get("final_score", 0)) / 11.2))))
        mutations.append({"card": card, "ext": ext, "priority": priority,
                          "pushed": card in pushable})
    coverage = {"internal": sum(e.get("origin_type", "internal") == "internal"
                                 for c in cards for e in c.get("evidence", [])),
                "external": sum(e.get("origin_type") == "external"
                                 for c in cards for e in c.get("evidence", [])),
                "candidates": len(candidates), "pushed": len(pushable),
                "candidate_merge": len(merges)}
    markdown = safe_text(dg.build_markdown(archivable, coverage, date=identity["date"], cfg=cfg))
    headlines = safe_text(dg.build_headlines(pushable, coverage, date=identity["date"],
        cap=int(cfg.get("push", {}).get("max_per_day", 5)), cfg=cfg))
    result = {"run_id": run_id, "identity": identity, "candidates": len(candidates),
              "built": len(cards), "new": len(new), "resurface": len(resurfaced),
              "suppressed": len(suppressed), "candidate_merge": merges,
              "blocked": gate["blocked"] + suppressed_gate["blocked"],
              "pushed": [], "proposed_push": [c["title"] for c in pushable],
              "archivable": [c["title"] for c in archivable],
              "empty_day": not archivable, "digest_markdown": markdown, "digest_path": None}
    return {"result": result, "mutations": mutations, "headlines": headlines,
            "markdown": markdown, "cards": cards}


def _load(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid durable run object: {path}")
    return value


def _artifact(path):
    path = Path(require_private(path)["path"])
    content = path.read_bytes()
    return {"path": str(path), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def verify_manifest(path, identity):
    manifest = _load(path)
    if manifest.get("identity") != identity or not manifest.get("artifacts"):
        raise ValueError("artifact manifest is not bound to this run")
    for item in manifest["artifacts"]:
        if _artifact(item["path"]) != item:
            raise ValueError(f"artifact bytes differ from manifest: {item['path']}")
    return manifest


def _receipt(value, identity, content_sha256):
    if not isinstance(value, dict):
        raise ValueError("adapter receipt must be a JSON object")
    if value.get("status") != "confirmed" or not value.get("message_id"):
        raise ValueError("adapter did not confirm an actual message ID")
    if value.get("identity") != identity:
        raise ValueError("adapter receipt identity mismatch")
    if value.get("content_sha256") != content_sha256:
        raise ValueError("adapter receipt content hash mismatch")
    return {"status": "confirmed", "identity": identity,
            "message_id": str(value["message_id"]), "content_sha256": content_sha256}


def _event_identity(identity):
    fields = {"product_id", "card_id", "event"}
    if (not isinstance(identity, dict) or set(identity) not in (fields, fields | {"revision"})
            or identity.get("event") not in {"new", "update"}
            or any(not isinstance(identity.get(key), str) or not identity[key].strip()
                   for key in ("product_id", "card_id"))):
        raise ValueError("invalid standalone card event identity")
    if "revision" in identity and (identity["event"] != "update"
            or not isinstance(identity["revision"], str) or not identity["revision"].strip()):
        raise ValueError("invalid standalone update revision")
    return dict(identity)


def _event_paths(identity):
    from data_safety import authorize_write
    directory = data_root() / "card-deliveries" / digest(identity)
    state_path, receipt_path = directory / "state.json", directory / "receipt.json"
    for path in (state_path, receipt_path):
        authorize_write(path)
    return state_path, receipt_path


def _load_event(state_path):
    state = _load(state_path)
    identity = _event_identity(state.get("identity"))
    content_hash = state.get("content_sha256")
    if (state.get("schema_version") != 1 or state.get("phase") not in {"delivery_unknown", "complete"}
            or not isinstance(content_hash, str) or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)):
        raise ValueError("invalid durable card delivery state")
    expected, receipt_path = _event_paths(identity)
    if state_path != expected:
        raise ValueError("card delivery state path differs from its identity")
    delivery = state.get("delivery")
    if state["phase"] == "delivery_unknown":
        if (not isinstance(delivery, dict) or delivery.get("status") != "unknown"
                or delivery.get("identity") != identity or delivery.get("content_sha256") != content_hash):
            raise ValueError("saved unknown card delivery contradicts its identity or content")
    else:
        _receipt(delivery, identity, content_hash)
    receipt = _receipt(_load(receipt_path), identity, content_hash) if receipt_path.exists() else None
    if state["phase"] == "complete" and (receipt is None or receipt != delivery):
        raise ValueError("completed card delivery has missing or contradictory receipt")
    return state, receipt


def _event_result(state_path, state):
    complete = state["phase"] == "complete"
    return {"ok": complete, "status": "complete" if complete else "pending_reconciliation",
            "identity": state["identity"], "delivery": state["delivery"], "state_path": str(state_path)}


def deliver_event(identity, message):
    """Own one standalone new/update event; ambiguous attempts never resend automatically."""
    identity = _event_identity(identity)
    if not isinstance(message, str) or not message:
        raise ValueError("card delivery requires nonempty rendered content")
    if any(pc.has_pii(value) for value in (message, *identity.values())):
        raise ValueError("egress DLP blocked standalone card identity or content")
    content_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
    state_path, receipt_path = _event_paths(identity)
    with file_lock(state_path.parent / ".lock"):
        if state_path.exists():
            state, receipt = _load_event(state_path)
            if state["identity"] != identity or state["content_sha256"] != content_hash:
                raise ValueError("card event identity already owns different content")
            if receipt is not None and state["phase"] == "delivery_unknown":
                state.update(phase="complete", delivery=receipt)
                atomic_json(state_path, state)
            return _event_result(state_path, state)
        if receipt_path.exists():
            raise ValueError("card delivery receipt exists without its owning state")
        state = {"schema_version": 1, "identity": identity, "content_sha256": content_hash,
                 "phase": "delivery_unknown", "delivery": {"status": "unknown", "identity": identity,
                                                            "content_sha256": content_hash}}
        atomic_json(state_path, state)
        try:
            with pc.delivery_context(identity):
                delivered = pc.deliver(message, dry_run=False)
            if isinstance(delivered, tuple) and len(delivered) == 2:
                ok, detail = delivered
                if not ok:
                    raise ValueError("adapter did not confirm card delivery")
            else:
                detail = delivered
            if isinstance(detail, str):
                detail = json.loads(detail)
            receipt = _receipt(detail, identity, content_hash)
        except Exception as exc:
            state["delivery"]["error_type"] = type(exc).__name__
            atomic_json(state_path, state)
            return _event_result(state_path, state)
        atomic_json(receipt_path, receipt)
        state.update(phase="complete", delivery=receipt)
        atomic_json(state_path, state)
        return _event_result(state_path, state)


def reconcile_event(state_path, adapter_receipt):
    """Accept bound evidence for one attempted standalone card event without sending."""
    from data_safety import authorize_write
    state_path = Path(authorize_write(state_path)["path"])
    with file_lock(state_path.parent / ".lock"):
        state, saved = _load_event(state_path)
        receipt = _receipt(adapter_receipt, state["identity"], state["content_sha256"])
        if saved is not None and saved != receipt:
            raise ValueError("confirmed card delivery receipt cannot be replaced")
        if state["phase"] == "complete":
            return saved
        if saved is None:
            atomic_json(state_path.with_name("receipt.json"), receipt)
        state.update(phase="complete", delivery=receipt)
        atomic_json(state_path, state)
        return receipt


def _pending(plan, state, state_path):
    return {**plan["result"], "status": "pending_reconciliation", "ok": False,
            "attempt_id": state["attempt_id"], "delivery": state["delivery"],
            "state_path": str(state_path), "manifest_path": state["manifest_path"]}


def reconcile(state_path, adapter_receipt):
    """Persist operator-supplied receipt evidence; never issues another send."""
    state_path = Path(require_private(state_path)["path"])
    with file_lock(state_path.parent / ".lock"):
        state = _load(state_path)
        manifest = verify_manifest(state["manifest_path"], state["identity"])
        if digest(manifest) != state["manifest_sha256"]:
            raise ValueError("current manifest differs from saved run state")
        receipt = _receipt(adapter_receipt, state["identity"], manifest["content_sha256"])
        if state["phase"] not in {"delivery_unknown", "delivered", "complete"}:
            raise ValueError("run has no attempted delivery to reconcile")
        if state["phase"] in {"delivered", "complete"}:
            if receipt != _load(state_path.parent / "receipt.json") or receipt != state["delivery"]:
                raise ValueError("confirmed delivery receipt cannot be replaced")
            return receipt
        atomic_json(state_path.parent / "receipt.json", receipt)
        state.update(phase="delivered", delivery=receipt)
        atomic_json(state_path, state)
    return receipt


def _implicit_finalize(candidates, cfg, ledger, run_id, archive_dir, identity, attempt_id):
    """Freeze the default cutoff for legacy direct callers, including restarts."""
    base = Path(archive_dir).expanduser() if archive_dir else data_root()
    require_private(base)
    key = digest({name: identity[name] for name in ("product_id", "timezone")})
    pointer = base / "implicit" / key / "active.json"
    history_path = pointer.parent / "completed.json"
    input_sha = digest(candidates)
    with file_lock(pointer.parent / ".lock", timeout=300):
        completed = _load(history_path) if history_path.exists() else {}
        lookup = digest({"date": identity["date"], "input_sha256": input_sha})
        previous = _load(pointer) if pointer.exists() else None
        if lookup in completed:
            # An intervening completed input must not erase an earlier receipt
            # or move the latest collection cutoff backwards when replayed.
            saved = logical_identity(cfg, completed[lookup])
            result = finalize(candidates, cfg, ledger, False, run_id, base,
                              identity=saved, attempt_id=attempt_id)
            if previous and previous["identity"] == saved:
                previous.update(status=result["status"], state_path=result["state_path"])
                atomic_json(pointer, previous)
            result["implicit_identity_path"] = str(pointer)
            return result
        if previous:
            prior_identity = logical_identity(cfg, previous["identity"])
            same_input = previous["input_sha256"] == input_sha
            if previous["status"] != "complete":
                prior_state = base / "runs" / digest(prior_identity) / "state.json"
                rejected_before_state = previous["status"] == "rejected" and not prior_state.exists()
                if not same_input and not rejected_before_state:
                    raise ValueError("unfinished implicit run requires its original candidates")
                identity = prior_identity
            elif prior_identity["date"] == identity["date"] and same_input:
                identity = prior_identity
            else:
                window = {"start": prior_identity["source_window"]["end"],
                          "end": identity["source_window"]["end"]}
                identity = logical_identity({**cfg, "source_window": window})
        state = {"identity": identity, "input_sha256": input_sha, "status": "started"}
        atomic_json(pointer, state)
        try:
            result = finalize(candidates, cfg, ledger, False, run_id, base,
                              identity=identity, attempt_id=attempt_id)
        except Exception as exc:
            bound_state = base / "runs" / digest(identity) / "state.json"
            state.update(status="failed" if bound_state.exists() else "rejected",
                         error_type=type(exc).__name__)
            atomic_json(pointer, state)
            raise
        if result["status"] == "complete":
            completed[digest({"date": identity["date"], "input_sha256": input_sha})] = identity
            atomic_json(history_path, completed)
        state.update(status=result["status"], state_path=result["state_path"])
        atomic_json(pointer, state)
        result["implicit_identity_path"] = str(pointer)
        return result


def finalize(candidates, cfg, ledger=None, dry_run=False, run_id=None, archive_dir=None,
             *, identity=None, attempt_id=None, collection=None):
    if isinstance(candidates, dict):
        handoff = candidates
        identity = handoff.get("identity", identity)
        attempt_id = handoff.get("attempt_id", attempt_id)
        collection = handoff.get("collection", collection)
        candidates = handoff.get("candidates")
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        raise ValueError("candidate handoff requires a list of objects")
    implicit = identity is None and not cfg.get("source_window") and collection is None and not dry_run
    identity = logical_identity(cfg, identity, dry_run=dry_run)
    collection = _collection(collection, candidates, identity, dry_run)
    candidates = safe_data(candidates)
    if implicit:
        if ledger is None:
            raise ValueError("a durable ledger is required for a real run")
        return _implicit_finalize(candidates, cfg, ledger, run_id, archive_dir, identity, attempt_id)
    run_key = digest(identity)
    run_id = run_id or "demand-" + run_key[:20]
    attempt_id = attempt_id or secrets.token_hex(16)
    if not isinstance(attempt_id, str) or not attempt_id:
        raise ValueError("attempt_id must be nonempty")
    if dry_run:
        plan = _prepare(candidates, cfg, ledger, identity, run_id)
        return {**plan["result"], "status": "preview", "ok": True, "attempt_id": attempt_id}
    if ledger is None:
        raise ValueError("a durable ledger is required for a real run")
    base = Path(archive_dir).expanduser() if archive_dir else data_root()
    require_private(base)
    run_dir = base / "runs" / run_key
    state_path, plan_path = run_dir / "state.json", run_dir / "plan.json"
    input_sha = digest({"candidates": candidates, "collection": collection})
    with file_lock(run_dir / ".lock", timeout=300):
        if state_path.exists():
            state = _load(state_path)
            if state.get("identity") != identity or state.get("input_sha256") != input_sha:
                raise ValueError("run input changed; use the original handoff or declare a new source window")
            plan = _load(plan_path)
            if digest(plan) != state["plan_sha256"]:
                raise ValueError("durable run plan was modified")
        else:
            plan = _prepare(candidates, cfg, ledger, identity, run_id)
            if plan["result"]["blocked"]:
                raise ValueError("candidate validation failed; this is not a no-content completion")
            atomic_json(plan_path, plan)
            state = {"identity": identity, "attempt_id": attempt_id, "phase": "prepared",
                     "input_sha256": input_sha, "plan_sha256": digest(plan),
                     "delivery": {"status": "not_attempted", "identity": identity},
                     "backup": {"status": "not_attempted"}}
            atomic_json(state_path, state)
        state["attempt_id"] = attempt_id
        ledger.run_identity = identity
        if state["phase"] == "prepared":
            for row in plan["mutations"]:
                ledger.upsert(row["card"], row["ext"], priority=row["priority"])
            dg.register_digest_item(ledger, identity["date"],
                                    summary=f"{len(plan['result']['archivable'])} demands",
                                    identity=identity)
            digest_path = dg.write_digest_file(plan["markdown"], str(run_dir / "archive"), identity["date"])
            cards_path = atomic_json(run_dir / "cards.json", {"identity": identity, "cards": plan["cards"]})
            manifest = {"identity": identity, "run_id": run_id, "attempt_id": attempt_id,
                        "collection": collection, "input_sha256": input_sha,
                        "artifacts": [_artifact(digest_path), _artifact(cards_path), _artifact(plan_path)],
                        "content_sha256": hashlib.sha256(plan["headlines"].encode("utf-8")).hexdigest()}
            manifest_path = atomic_json(run_dir / "manifest.json", manifest)
            state.update(phase="artifacts_ready", manifest_path=str(manifest_path),
                         manifest_sha256=digest(manifest), digest_path=str(digest_path))
            atomic_json(state_path, state)
        manifest = verify_manifest(state["manifest_path"], identity)
        if digest(manifest) != state["manifest_sha256"]:
            raise ValueError("current manifest differs from saved run state")
        plan["result"]["digest_path"] = state["digest_path"]
        if state["phase"] == "delivery_unknown":
            try:
                saved_receipt = _load(require_private(run_dir / "receipt.json")["path"])
            except FileNotFoundError:
                return _pending(plan, state, state_path)
            receipt = _receipt(saved_receipt, identity, manifest["content_sha256"])
            unknown = state.get("delivery", {})
            if (unknown.get("status") != "unknown" or unknown.get("identity") != identity
                    or unknown.get("content_sha256") != manifest["content_sha256"]):
                raise ValueError("saved unknown delivery and adapter receipt disagree")
            state.update(phase="delivered", delivery=receipt)
            atomic_json(state_path, state)
        if state["phase"] == "artifacts_ready":
            state.update(phase="delivery_unknown", delivery={"status": "unknown", "identity": identity,
                         "content_sha256": manifest["content_sha256"]})
            atomic_json(state_path, state)
            try:
                with pc.delivery_context(identity):
                    delivered = pc.deliver(plan["headlines"], dry_run=False)
                if isinstance(delivered, tuple) and len(delivered) == 2:
                    ok, detail = delivered
                    if isinstance(detail, str):
                        detail = json.loads(detail)
                    if not ok:
                        raise ValueError("adapter did not confirm delivery")
                else:
                    detail = delivered
                receipt = _receipt(detail, identity, manifest["content_sha256"])
            except Exception as exc:
                state["delivery"]["error_type"] = type(exc).__name__
                atomic_json(state_path, state)
                return _pending(plan, state, state_path)
            atomic_json(run_dir / "receipt.json", receipt)
            state.update(phase="delivered", delivery=receipt)
            atomic_json(state_path, state)
        receipt = _receipt(_load(run_dir / "receipt.json"), identity, manifest["content_sha256"])
        if receipt != state["delivery"]:
            raise ValueError("saved delivery and adapter receipt disagree")
        if state["phase"] == "delivered":
            # Replay assigns the immutable planned count; it never increments a
            # newly read count after a partially completed bookkeeping attempt.
            for row in plan["mutations"]:
                if row["pushed"]:
                    ext = {**row["ext"], dd.EXT + "push_count": row["ext"][dd.EXT + "push_count"] + 1,
                           dd.EXT + "last_push_identity": identity}
                    ledger.upsert(row["card"], ext, priority=row["priority"])
            # Durable current artifacts and receipt precede cursor advancement.
            ledger.add_watermark(identity["source_window"].get("end") or iso(now_utc()))
            atomic_json(run_dir / "cursor.json", {"identity": identity,
                        "source_window": identity["source_window"], "receipt": receipt})
            state["phase"] = "complete"
            atomic_json(state_path, state)
        if state["phase"] != "complete":
            raise ValueError("invalid finalizer phase")
        return {**plan["result"], "status": "complete", "ok": True, "attempt_id": attempt_id,
                "completion_scope": "local_delivery",
                "pushed": plan["result"]["proposed_push"], "delivery": receipt,
                "manifest_path": state["manifest_path"], "state_path": str(state_path),
                "backup": state["backup"]}

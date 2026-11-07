"""Small, durable household watch history for prepared VOD episodes.

This module has no network, process, or background-thread side effects. It
stores explicit choices and, when supplied by a conservative detector,
separately labelled inferred activity. Reads never prove a frame was watched.
"""

import json
import os
import re
import tempfile
import threading
import time


_ISTANBULLU_RE = re.compile(r"^\s*a\s+noiva\s+de\s+istambul\s+(\d{1,3})\s*-\s*star\s+\d{3,4}p?\s*$", re.I)
_S_EP_RE = re.compile(r"\bS(\d{1,2})\s*E(\d{1,3})\b", re.I)
_X_EP_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I)
_NAMED_EP_RE = re.compile(r"\b(?:epis[oó]dio|ep\.?)\s*(\d{1,3})\b", re.I)


def _clean_title(value):
    return " ".join(str(value or "").replace("@", " ").split()).strip()


def resolve_episode_identity(task):
    """Return a conservative, persistent identity or ``None`` for non-series.

    A catalog-provided series id always wins.  Imported Istanbul Star episodes
    receive an explicit key so their resolution (528p/526p) is never confused
    with the episode number.
    """
    if not isinstance(task, dict):
        return None
    title = _clean_title(task.get("series_title") or task.get("title") or task.get("display_name"))
    raw_series_id = task.get("series_id") or task.get("series_key")
    episode = task.get("episode_num")
    season = task.get("season_num")
    try:
        episode = int(episode) if episode not in (None, "") else None
    except (TypeError, ValueError):
        episode = None
    try:
        season = int(season) if season not in (None, "") else None
    except (TypeError, ValueError):
        season = None

    istanbullu = _ISTANBULLU_RE.match(title)
    if istanbullu:
        episode = int(istanbullu.group(1))
        return {
            "series_key": "import:istanbullu-star",
            "series_title": "A Noiva de Istambul",
            "season_number": None,
            "episode_number": episode,
            "task_id": task.get("id"),
            "media_revision": media_revision(task),
        }

    if not episode:
        match = _S_EP_RE.search(title) or _X_EP_RE.search(title)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
        else:
            match = _NAMED_EP_RE.search(title)
            if match:
                episode = int(match.group(1))
    if not raw_series_id or not episode:
        return None
    return {
        "series_key": "catalog:" + str(raw_series_id),
        "series_title": _clean_title(task.get("series_title") or title),
        "season_number": season,
        "episode_number": episode,
        "task_id": task.get("id"),
        "media_revision": media_revision(task),
    }


def media_revision(task):
    """Identity of bytes, stable across a local -> Drive migration."""
    digest = task.get("remote_md5") or task.get("md5") or task.get("content_md5")
    size = task.get("file_size") or task.get("file_size_bytes") or 0
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    return "%s:%s" % (str(digest or "unknown").lower(), size)


class VodHistory:
    """Atomic JSON store, bounded by series rather than noisy read events."""

    def __init__(self, path, clock=time.time, max_recent=50):
        self.path = path
        self.clock = clock
        self.max_recent = max_recent
        self._lock = threading.RLock()
        self._data = {"version": 1, "series": {}}
        self.load()

    def load(self):
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict) or not isinstance(data.get("series"), dict):
                    raise ValueError("invalid history schema")
                self._data = {"version": 1, "series": data["series"]}
            except (OSError, ValueError, json.JSONDecodeError):
                self._data = {"version": 1, "series": {}}

    def _save_locked(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".watch-history-", delete=False) as handle:
                temp_path = handle.name
                json.dump(self._data, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def _record_locked(self, identity):
        key = identity["series_key"]
        now = float(self.clock())
        record = self._data["series"].setdefault(key, {
            "series_key": key, "series_title": identity["series_title"],
            "completed": {}, "recent": [], "revision": 0,
        })
        record["series_title"] = identity["series_title"] or record.get("series_title") or key
        record["updated_at"] = now
        record["revision"] = int(record.get("revision", 0)) + 1
        return record, now

    @staticmethod
    def _episode(identity, *, source, position_seconds=None, confidence=None):
        value = dict(identity)
        value.update({"source": source, "updated_at": time.time()})
        if position_seconds is not None:
            value["position_seconds"] = position_seconds
        if confidence:
            value["confidence"] = confidence
        return value

    def record_selected(self, task):
        identity = resolve_episode_identity(task)
        if not identity:
            return None
        with self._lock:
            record, now = self._record_locked(identity)
            selected = self._episode(identity, source="selected")
            selected["updated_at"] = now
            selected["history_sequence"] = record["revision"]
            record.setdefault("completed", {}).pop(identity["task_id"], None)
            record["last_selected"] = selected
            record["recent"] = ([selected] + [event for event in record.get("recent", [])
                                   if event.get("task_id") != identity["task_id"]])[:self.max_recent]
            self._save_locked()
            return dict(selected)

    def checkpoint(self, task, position_seconds=None, source="manual"):
        identity = resolve_episode_identity(task)
        if not identity:
            return None
        if position_seconds is not None:
            if isinstance(position_seconds, bool):
                raise ValueError("invalid position")
            position_seconds = float(position_seconds)
            if position_seconds < 0 or position_seconds > 24 * 60 * 60:
                raise ValueError("invalid position")
        with self._lock:
            record, now = self._record_locked(identity)
            checkpoint = self._episode(identity, source=source, position_seconds=position_seconds,
                                       confidence="manual" if source == "manual" else source)
            checkpoint["updated_at"] = now
            checkpoint["history_sequence"] = record["revision"]
            record.setdefault("completed", {}).pop(identity["task_id"], None)
            record["checkpoint"] = checkpoint
            record["recent"] = ([checkpoint] + [event for event in record.get("recent", [])
                                     if event.get("task_id") != identity["task_id"]])[:self.max_recent]
            self._save_locked()
            return dict(checkpoint)

    def record_inferred(self, task):
        """Record a probable episode without marking completion or a timestamp."""
        identity = resolve_episode_identity(task)
        if not identity:
            return None
        with self._lock:
            existing = self._data["series"].get(identity["series_key"], {})
            if identity["task_id"] in existing.get("completed", {}):
                return None
            now = float(self.clock())
            for field in ("checkpoint", "last_selected"):
                explicit = existing.get(field) or {}
                if (explicit and explicit.get("task_id") != identity["task_id"] and
                    now - float(explicit.get("updated_at", 0)) < 60):
                    return None
            record, now = self._record_locked(identity)
            observed = self._episode(identity, source="inferred_stream", confidence="inferred")
            observed["updated_at"] = now
            observed["history_sequence"] = record["revision"]
            record["inferred"] = observed
            record["recent"] = ([observed] + [event for event in record.get("recent", [])
                                if event.get("task_id") != identity["task_id"]])[:self.max_recent]
            self._save_locked()
            return dict(observed)

    def complete(self, task):
        identity = resolve_episode_identity(task)
        if not identity:
            return None
        with self._lock:
            record, now = self._record_locked(identity)
            completed = self._episode(identity, source="manual_completed", confidence="manual")
            completed["updated_at"] = now
            record.setdefault("completed", {})[identity["task_id"]] = completed
            if record.get("checkpoint", {}).get("task_id") == identity["task_id"]:
                record.pop("checkpoint", None)
            if record.get("last_selected", {}).get("task_id") == identity["task_id"]:
                record.pop("last_selected", None)
            if record.get("inferred", {}).get("task_id") == identity["task_id"]:
                record.pop("inferred", None)
            record["recent"] = ([completed] + [event for event in record.get("recent", [])
                                    if event.get("task_id") != identity["task_id"]])[:self.max_recent]
            self._save_locked()
            return dict(completed)

    def reset(self, series_key):
        if not isinstance(series_key, str) or not series_key:
            return False
        with self._lock:
            if series_key not in self._data["series"]:
                return False
            del self._data["series"][series_key]
            self._save_locked()
            return True

    def summary(self, tasks):
        """Return safe UI records and a ready continuation target where possible."""
        by_id = dict(tasks or {})
        with self._lock:
            records = json.loads(json.dumps(self._data["series"], ensure_ascii=False))
        results = []
        for record in records.values():
            explicit = [record.get(field) for field in ("checkpoint", "last_selected")
                        if isinstance(record.get(field), dict)]
            def event_order(event):
                try:
                    sequence = int(event.get("history_sequence") or 0)
                except (ValueError, TypeError, OverflowError):
                    sequence = 0
                try:
                    updated = float(event.get("updated_at") or 0)
                except (ValueError, TypeError, OverflowError):
                    updated = 0
                return sequence, updated
            target = max(explicit, key=event_order) if explicit else None
            inferred = record.get("inferred")
            if isinstance(inferred, dict) and (not target or event_order(inferred) > event_order(target)):
                target = inferred
            target_id = target.get("task_id") if isinstance(target, dict) else None
            task = by_id.get(target_id)
            available = bool(task and task.get("status") == "ready")
            # After explicit completion, select exactly the immediate next known
            # episode only.  A missing episode is never silently skipped.
            if not target_id and record.get("completed"):
                finished = list(record["completed"].values())
                finished.sort(key=lambda item: (item.get("season_number") or 0, item.get("episode_number") or 0), reverse=True)
                if finished:
                    last = finished[0]
                    expected = (last.get("episode_number") or 0) + 1
                    candidates = []
                    for candidate in by_id.values():
                        identity = resolve_episode_identity(candidate)
                        if identity and identity["series_key"] == record.get("series_key"):
                            candidates.append((identity, candidate))
                    for identity, candidate in candidates:
                        if identity.get("episode_number") == expected:
                            target, target_id, task = identity, candidate.get("id"), candidate
                            available = candidate.get("status") == "ready"
                            break
                    else:
                        # Keep the card visible rather than pretending the next
                        # episode does not exist or jumping over a gap.
                        target = dict(last)
                        target["episode_number"] = expected
                        target["task_id"] = None
                        target_id, task, available = None, None, False
            if not target:
                continue
            results.append({
                "series_key": record.get("series_key"), "series_title": record.get("series_title"),
                "task_id": target_id, "episode_number": target.get("episode_number"),
                "season_number": target.get("season_number"), "updated_at": target.get("updated_at", record.get("updated_at")),
                "available": available, "completed": target_id in record.get("completed", {}),
                "position_seconds": target.get("position_seconds"), "position_source": target.get("source"),
                "position_confidence": target.get("confidence"),
                "poster": task.get("poster") if task else None,
                "title": task.get("title") if task else target.get("series_title"),
            })
        results.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
        return results

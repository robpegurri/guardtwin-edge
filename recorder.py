"""
    RECORDER: the raw inputs from the outside world, for exact replays

Each process (broker, escalator) is pointed at the recordings directory
(--record-dir) and records while the dashboard's Record button says so: the
dashboard writes <dir>/control.json ({"on": true, "id": "<UTC start>"}), which
every process checks twice a second, opening or closing its file without a
restart. One NDJSON file per process and recording, <id>-<component>.ndjson
(<id>-<component>-r<n>.ndjson for a process that restarted meanwhile), with
every input exactly as it arrived (radar datagrams, AMF/metrics answers, LLM
calls, ENVELOPE callbacks and queries, Risk Escalation responses):

    {"t": <unix time>, "kind": "<what>", ...}

The first line ("session") holds the configuration of the run; the site
geometry in use is copied next to the files once (<name>-<sha256[:12]>.geojson).
tools/replay_recording.py plays a broker recording back into the stack.

Not recording, record() costs one attribute check.
"""

import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time

log = logging.getLogger("recorder")

CONTROL = "control.json"
CHECK_S = 0.5

_lock = threading.Lock()
_file = None
_seen = set()


def configure(directory, component, meta):
    """Follow the dashboard's Record button. meta() gives the configuration
    for the session line, evaluated at each start."""
    threading.Thread(target=_watch, args=(directory, component, meta),
                     name="recorder", daemon=True).start()


def enabled():
    return _file is not None


def record(kind, **fields):
    if _file is None:
        return
    line = json.dumps({"t": round(time.time(), 6), "kind": kind, **fields},
                      separators=(",", ":"), ensure_ascii=False, default=str)
    with _lock:
        if _file is not None:
            _file.write(line + "\n")


def record_once(kind, key, **fields):
    """record() the first time `key` is seen in a file (e.g. a constant
    system prompt)."""
    if _file is None or (kind, key) in _seen:
        return
    _seen.add((kind, key))
    record(kind, key=key, **fields)


def read_control(directory):
    try:
        with open(os.path.join(directory, CONTROL), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _watch(directory, component, meta):
    current = None                           # the recording id being written
    while True:
        ctl = read_control(directory)
        want = ctl.get("id") if ctl.get("on") else None
        if want != current:
            if current:
                _close(current)
            if want:
                try:
                    _open(directory, component, want, meta)
                except Exception as e:
                    log.warning("cannot start recording %s (%s)", want, e)
                    want = None
            current = want
        time.sleep(CHECK_S)


def _open(directory, component, rec_id, meta):
    global _file
    path = os.path.join(directory, f"{rec_id}-{component}.ndjson")
    n = 1
    while os.path.exists(path):              # restarted during the recording
        n += 1
        path = os.path.join(directory, f"{rec_id}-{component}-r{n}.ndjson")
    f = open(path, "a", buffering=1, encoding="utf-8")
    _give_to_dir_owner(path, directory)
    _seen.clear()
    with _lock:
        _file = f
    record("session", component=component, recording=rec_id, part=n,
           argv=_redact(sys.argv[1:]), **meta())
    log.info("recording to %s", path)


def _close(rec_id):
    global _file
    record("end")
    with _lock:
        f, _file = _file, None
    if f:
        f.close()
    log.info("recording %s stopped", rec_id)


def snapshot_file(path, directory):
    """Copy a file (the site geometry) next to the recordings, named by its
    content hash, once; returns {"path", "sha256", "copy"} for the session."""
    try:
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
    except OSError as e:
        return {"path": path, "error": str(e)}
    root, ext = os.path.splitext(os.path.basename(path))
    copy = os.path.join(directory, f"{root}-{sha[:12]}{ext}")
    if not os.path.exists(copy):
        shutil.copyfile(path, copy)
        _give_to_dir_owner(copy, directory)
    return {"path": path, "sha256": sha, "copy": os.path.basename(copy)}


def _redact(argv):
    """The command line, with the values of secret options masked."""
    out, hide = [], False
    for a in argv:
        out.append("…" if hide else a)
        hide = a.startswith("--") and any(w in a for w in ("token", "secret", "password"))
    return out


def _give_to_dir_owner(path, directory):
    # the containers run as root: files go to whoever owns the directory
    try:
        st = os.stat(directory)
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass

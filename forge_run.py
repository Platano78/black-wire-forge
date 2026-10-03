"""Music-video run: orchestrate audio-led video from a song + photo.

Class MusicVideoRun drives the full pipeline against a small ``backend``
duck-typed object.  No imports of the server module.

Backend contract (all plain Python, raise ``RunError("plain sentence")`` for
anything the person should read; ``RunStopped`` is raised by
``backend.check_stop()`` when the person pressed stop)::

    create_sequence(title) -> sid
    set_canvas(sid, w, h)
    import_master(sid, song_job_id) -> seconds
        (copies the song file of that History job as the sequence's master;
         returns its length in seconds)
    set_audio_led(sid)
    add_shot(sid, window, prompt, start_image_name, w, h) -> slot_id
        (video slot, mode ltx, length=window["frames"], two_stage True,
         image_strength 0.9)
    make_still(prompt, photo_name, w, h) -> job_id
    wait_job(job_id, timeout) -> job dict (status done/error/failed)
    carry_still(job_id) -> lane_file_name
        (puts the still where the video model can read it as start image)
    generate_shot(sid, slot_id) -> job_id
    wait_take(sid, slot_id, job_id, timeout) -> True|False
    pick(sid, slot_id, job_id)
    cut(sid) -> cut_id
    wait_cut(sid, cut_id, timeout) -> {"status", "file"}
    helper_chat(system, user) -> str
        (may raise; forge_master.write_scenes copes)
    check_stop()
        (raises RunStopped when the person pressed stop)
    sleep(s)
    say(stage, message, done, total)
        (progress sink)

"""

import json
import math
import os
import threading
import time
import traceback

import forge_master

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RunError(Exception):
    """Raised when the person should read the message."""


class RunStopped(Exception):
    """Raised by backend.check_stop() when the person pressed stop."""


# ---------------------------------------------------------------------------
# Run state persistence
# ---------------------------------------------------------------------------

FORGE_RUNS = {}          # id -> record
FORGE_RUN_STOP = {}      # id -> True once the person pressed stop
FORGE_RUN_LOCK = threading.Lock()
_DATA_DIR = None


def _forge_runs_path():
    return os.path.join(_DATA_DIR, "forge_runs.json")


def configure(data_dir):
    """Set where run records are kept and load them. A record still "running" belonged to a process that is gone."""
    global _DATA_DIR
    _DATA_DIR = data_dir
    path = _forge_runs_path()
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            runs = json.load(f)
    except Exception:
        traceback.print_exc()
        return
    if not isinstance(runs, dict):
        return
    with FORGE_RUN_LOCK:
        for rec in runs.values():
            if isinstance(rec, dict) and rec.get("status") == "running":
                rec["status"] = "error"
                rec["error"] = rec["message"] = ("The app restarted while this was running. "
                                                  "The shots made so far are in the Cutting Room.")
        FORGE_RUNS.update(runs)


def _save_locked():
    """Write FORGE_RUNS atomically. The caller holds FORGE_RUN_LOCK."""
    if not _DATA_DIR:
        return
    path = _forge_runs_path()
    tmp = "%s.%d.tmp" % (path, threading.get_ident())
    try:
        with open(tmp, "w") as f:
            json.dump(FORGE_RUNS, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        traceback.print_exc()


def new_run():
    """Start a record. Returns the id, or None when a music video is already being made."""
    with FORGE_RUN_LOCK:
        if any(r.get("status") == "running" for r in FORGE_RUNS.values()):
            return None
        rid = os.urandom(6).hex()
        FORGE_RUNS[rid] = {"id": rid, "kind": "music-video", "status": "running", "stage": "planning", "message": "",
                           "done": 0, "total": 0, "plan_line": "", "sequence": None, "cut_id": None, "file": None,
                           "error": None, "created": time.time()}
        _save_locked()
        return rid


def update_run(run_id, **fields):
    with FORGE_RUN_LOCK:
        rec = FORGE_RUNS.get(run_id)
        if rec is not None:
            rec.update(fields)
            _save_locked()


def get_run(run_id):
    """A copy of one record, the newest record when run_id is empty, or None."""
    with FORGE_RUN_LOCK:
        if run_id:
            rec = FORGE_RUNS.get(run_id)
        else:
            rec = max(FORGE_RUNS.values(), key=lambda r: r.get("created", 0), default=None)
        return dict(rec) if rec else None


def request_stop(run_id):
    with FORGE_RUN_LOCK:
        rec = FORGE_RUNS.get(run_id)
        if rec is None:
            return False
        if rec.get("status") == "running":
            FORGE_RUN_STOP[run_id] = True
        return True


# ---------------------------------------------------------------------------
# MusicVideoRun
# ---------------------------------------------------------------------------


class MusicVideoRun:
    """Orchestrate the full music-video pipeline.

    Constructor takes a ``backend`` object (see module docstring for its
    interface).  ``run()`` drives the whole flow.
    """

    def __init__(self, backend):
        self.backend = backend

    # -- helpers ----------------------------------------------------------

    def _check_stop(self):
        """Call backend.check_stop(); re-raise as RunStopped."""
        self.backend.check_stop()

    def _say(self, stage, message, done, total):
        self.backend.say(stage, message, done, total)

    # -- pipeline ---------------------------------------------------------

    def run(self, song_job_id, photo_name, style_note, lyrics, w, h):
        """Execute the full music-video pipeline.

        Parameters
        ----------
        song_job_id : str
            A finished audio job in History.
        photo_name : str
            File name of the reference photo (uploaded to the lane).
        style_note : str
            One-line style hint (may be empty).
        lyrics : str or None
            Song lyrics (may be None for instrumental).
        w, h : int
            Output width / height (multiples of 16, 256..1152).

        Returns
        -------
        dict  with keys ``sequence`` (sid), ``cut_id``, ``file``, ``shots``.
        """
        try:
            # --- planning stage ---
            self._say("planning", "Planning windows …", 0, 0)
            self._check_stop()

            # create sequence
            sid = self.backend.create_sequence(
                "Music video: " + style_note.strip()[:40]
            )
            self._check_stop()
            self.backend.set_canvas(sid, w, h)
            self._check_stop()

            # import master
            seconds = self.backend.import_master(sid, song_job_id)
            self._check_stop()

            # plan windows
            windows = forge_master.plan_windows(seconds)
            self._check_stop()

            # plan_line (for the progress report)
            plan_line = forge_master.plan_line(windows)
            if hasattr(self.backend, "plan"):
                self.backend.plan(plan_line, len(windows))

            # assign lyrics to windows
            if lyrics:
                parsed = forge_master.parse_lyrics(lyrics)
                windows = forge_master.assign_lyrics(windows, parsed)

            n_shots = len(windows)
            total_steps = 2 * n_shots  # still + shot per window

            self._say("scenes", "Writing scenes …", 0, 0)
            self._check_stop()

            # write scenes (has_person True)
            scenes, source = forge_master.write_scenes(
                windows,
                chat=self.backend.helper_chat if self.backend else None,
                style_note=style_note,
                has_person=True,
            )
            self._say("scenes", f"Scenes written ({source})", 0, 0)
            self._check_stop()

            # --- stills stage ---
            self._say("stills", "Making stills …", 0, total_steps)
            still_names = []
            for i, (win, scene) in enumerate(zip(windows, scenes)):
                shot_num = i + 1
                self._check_stop()

                orientation = "portrait" if h >= w else "landscape"
                still_prompt = (
                    "Put the person in the reference picture into a new scene: "
                    f"they are {scene['scene']}. Keep their exact face, hairstyle "
                    f"and likeness. Photorealistic cinematic photo, {orientation} "
                    "orientation, they fill the frame from the chest up, facing "
                    "the camera with their mouth slightly open as if singing."
                )

                job_id = None
                for attempt in range(2):          # one retry: a still lost an hour in would waste the run
                    job_id = self.backend.make_still(
                        still_prompt, photo_name, w, h
                    )
                    self._check_stop()
                    job = self.backend.wait_job(job_id, timeout=600)
                    if job.get("status") not in ("error", "failed"):
                        break
                else:
                    raise RunError(
                        f"Still {shot_num} failed to render twice."
                    )
                lane_name = self.backend.carry_still(job_id)
                self._check_stop()

                still_names.append(lane_name)
                self._say(
                    "stills",
                    f"Still {shot_num}/{n_shots}",
                    shot_num,
                    total_steps,
                )

            # --- shots stage ---
            self._say("shots", "Generating shots …", 0, total_steps)
            slot_ids = []
            for i, (win, scene) in enumerate(zip(windows, scenes)):
                shot_num = i + 1
                self._check_stop()

                shot_prompt = (
                    f"{scene['scene']}. {scene['motion']} "
                    "The person is singing the song straight to the camera, "
                    "shouting the words, their mouth moving in time with the "
                    "music, natural lip movement. The camera stays mostly "
                    "steady."
                )

                slot_id = self.backend.add_shot(
                    sid, win, shot_prompt, still_names[i], w, h
                )
                slot_ids.append(slot_id)
                self._check_stop()

            # audio-led on
            self.backend.set_audio_led(sid)
            self._check_stop()

            # generate shots (up to 2 attempts per shot)
            for i, slot_id in enumerate(slot_ids):
                shot_num = i + 1
                self._check_stop()

                last_error = None
                for attempt in range(2):
                    job_id = self.backend.generate_shot(sid, slot_id)
                    self._check_stop()

                    ok = self.backend.wait_take(
                        sid, slot_id, job_id, timeout=1800
                    )
                    if ok:
                        self.backend.pick(sid, slot_id, job_id)
                        self._check_stop()
                        break
                    last_error = "Shot {} failed on attempt {}.".format(
                        shot_num, attempt + 1
                    )

                if not ok:
                    raise RunError(
                        "Shot {} failed twice. The sequence is open in the "
                        "Cutting Room with the shots made so far.".format(
                            shot_num
                        )
                    )

                self._say(
                    "shots",
                    f"Shot {shot_num}/{n_shots}",
                    n_shots + shot_num,
                    total_steps,
                )

            # --- cutting stage ---
            self._say("cutting", "Cutting …", 0, 0)
            cut_id = self.backend.cut(sid)
            self._check_stop()

            result = self.backend.wait_cut(sid, cut_id, timeout=1800)
            if result.get("status") != "done":
                raise RunError(
                    f"The cut failed with status {result.get('status')}."
                )
            self._check_stop()

            self._say("done", "Done", total_steps, total_steps)

            return {
                "sequence": sid,
                "cut_id": cut_id,
                "file": result.get("file"),
                "shots": n_shots,
            }

        except RunStopped:
            raise
        except RunError:
            raise
        except Exception as e:
            raise RunError(str(e))

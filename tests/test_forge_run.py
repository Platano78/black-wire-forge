"""Tests for forge_run.MusicVideoRun with a FakeBackend.

Run: python3 tests/test_forge_run.py
"""
import json
import sys
import os

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import forge_master  # noqa: E402
from forge_run import MusicVideoRun, RunError, RunStopped  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (
        ("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# FakeBackend
# ---------------------------------------------------------------------------


class FakeBackend:
    """Records every call, returns canned ids, wait_take defaults True."""

    def __init__(self, wait_take_ok=True, helper_raises=False,
                 wait_job_raises_timeout=False):
        self.calls = []          # list of (method, args, kwargs) tuples
        self.wait_take_ok = wait_take_ok
        self.helper_raises = helper_raises
        self.wait_job_raises_timeout = wait_job_raises_timeout
        self._stop = False
        self._shot_failures = {}  # slot_id -> number of times to fail before succeeding
        self._shot_attempt = {}   # slot_id -> current attempt count
        self._counter = 0
        self.data_dir = "/tmp"

    def _next_id(self, prefix="j"):
        self._counter += 1
        return prefix + str(self._counter)

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))

    # -- sequence lifecycle --

    def create_sequence(self, title):
        self._record("create_sequence", title)
        return "s_test001"

    def set_canvas(self, sid, w, h):
        self._record("set_canvas", sid, w, h)

    def import_master(self, sid, song_job_id):
        self._record("import_master", sid, song_job_id)
        return 12.0  # 12 seconds

    def set_audio_led(self, sid):
        self._record("set_audio_led", sid)

    # -- slots --

    def add_shot(self, sid, window, prompt, start_image_name, w, h):
        self._record("add_shot", sid, window, prompt, start_image_name, w, h)
        self._counter += 1
        return "slot_" + str(self._counter)

    # -- stills --

    def make_still(self, prompt, photo_name, w, h):
        self._record("make_still", prompt, photo_name, w, h)
        return self._next_id("still")

    def make_portrait(self, prompt, w, h):
        self._record("make_portrait", prompt, w, h)
        return self._next_id("portrait")

    def wait_job(self, job_id, timeout):
        self._record("wait_job", job_id, timeout)
        if self.wait_job_raises_timeout:
            raise ValueError("Job {} took too long.".format(job_id))
        return {"id": job_id, "status": "done", "outputs": [{"filename": "out.png", "type": "output", "media": "image"}]}

    def carry_still(self, job_id):
        self._record("carry_still", job_id)
        return "carried_" + job_id

    # -- shots --

    def generate_shot(self, sid, slot_id):
        self._record("generate_shot", sid, slot_id)
        return self._next_id("gen")

    def wait_take(self, sid, slot_id, job_id, timeout):
        self._record("wait_take", sid, slot_id, job_id, timeout)
        # Initialize attempt counter for this slot
        if slot_id not in self._shot_attempt:
            self._shot_attempt[slot_id] = 0
        self._shot_attempt[slot_id] += 1
        attempt = self._shot_attempt[slot_id]
        fail_count = self._shot_failures.get(slot_id, 0)
        if attempt <= fail_count:
            return False
        return self.wait_take_ok

    def pick(self, sid, slot_id, job_id):
        self._record("pick", sid, slot_id, job_id)

    # -- cutting --

    def cut(self, sid):
        self._record("cut", sid)
        return "k_cut001"

    def wait_cut(self, sid, cut_id, timeout):
        self._record("wait_cut", sid, cut_id, timeout)
        return {"status": "done", "file": "video.mp4"}

    # -- helpers --

    def helper_chat(self, system, user):
        self._record("helper_chat", system, user)
        if self.helper_raises:
            raise RuntimeError("helper is down")
        # Return a valid scene reply
        n = user.count(".")  # rough estimate of N
        if n < 2:
            n = 3
        return json.dumps({"scenes": [
            {"scene": "scene %d" % i, "motion": "move %d" % i}
            for i in range(n)
        ]})

    def check_stop(self):
        self._record("check_stop")
        if self._stop:
            raise RunStopped()

    def sleep(self, s):
        self._record("sleep", s)

    def say(self, stage, message, done, total):
        self._record("say", stage, message, done, total)

    def set_stop(self):
        self._stop = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _test_exact_call_order():
    """The exact order of backend calls for a 3-window song."""
    fb = FakeBackend()
    run = MusicVideoRun(fb)

    windows = forge_master.plan_windows(12.0)
    # 12 seconds = 36 thirds, n=3, each 12 thirds
    check("_test_exact_call_order: 3 windows", len(windows) == 3,
          str(len(windows)))

    scenes, _ = forge_master.write_scenes(windows, chat=fb.helper_chat,
                                          has_person=True)
    check("_test_exact_call_order: 3 scenes", len(scenes) == 3)

    result = run.run(
        song_job_id="audio_001",
        photo_name="photo.png",
        style_note="neon vibes",
        lyrics="[Verse]\nlyrics here",
        w=576, h=768,
    )

    check("exact_call_order: result has sequence", result["sequence"] == "s_test001")
    check("exact_call_order: result has cut_id", result["cut_id"] == "k_cut001")
    check("exact_call_order: result has file", result["file"] == "video.mp4")
    check("exact_call_order: result has shots=3", result["shots"] == 3)

    # Check the call sequence (check_stop interleaved between steps)
    method_names = [c[0] for c in fb.calls]

    # Find indices of key operations (skip check_stop and say)
    def _idx_of(method, start=0):
        for i in range(start, len(method_names)):
            if method_names[i] == method:
                return i
        return -1

    # Planning phase: say -> check_stop -> create -> check_stop -> set_canvas
    # -> check_stop -> import -> check_stop
    idx_create = _idx_of("create_sequence")
    idx_canvas = _idx_of("set_canvas")
    idx_import = _idx_of("import_master")
    idx_audio_led = _idx_of("set_audio_led")
    idx_cut = _idx_of("cut")
    idx_wait_cut = _idx_of("wait_cut")

    check("exact_call_order: create_sequence exists", idx_create >= 0)
    check("exact_call_order: set_canvas after create",
          idx_canvas > idx_create, "c=%d canvas=%d" % (idx_create, idx_canvas))
    check("exact_call_order: import after canvas",
          idx_import > idx_canvas, "canvas=%d import=%d" % (idx_canvas, idx_import))

    # All stills come before set_audio_led
    still_indices = [_idx_of("make_still", idx_import + 1)]
    for i in range(1, 3):
        still_indices.append(_idx_of("make_still", still_indices[-1] + 1))
    for si in still_indices:
        check("exact_call_order: still exists", si >= 0)
    check("exact_call_order: all stills before audio-led",
          all(si < idx_audio_led for si in still_indices if si >= 0))

    # All shots come after audio_led
    gen_indices = []
    idx = idx_audio_led + 1
    for _ in range(3):
        gi = _idx_of("generate_shot", idx)
        gen_indices.append(gi)
        idx = gi + 1
    for gi in gen_indices:
        check("exact_call_order: gen_shot exists", gi >= 0)
    check("exact_call_order: all shots after audio-led",
          all(gi > idx_audio_led for gi in gen_indices if gi >= 0))

    # cut comes after all shots
    for gi in gen_indices:
        check("exact_call_order: cut after last gen",
              idx_cut > gi, "gen=%d cut=%d" % (gi, idx_cut))
    check("exact_call_order: wait_cut after cut",
          idx_wait_cut > idx_cut, "cut=%d wait_cut=%d" % (idx_cut, idx_wait_cut))

    # Count: 3 make_still, 3 wait_job, 3 carry_still, 3 add_shot
    check("exact_call_order: 3 make_still",
          method_names.count("make_still") == 3,
          str(method_names.count("make_still")))
    check("exact_call_order: 3 wait_job",
          method_names.count("wait_job") == 3,
          str(method_names.count("wait_job")))
    check("exact_call_order: 3 carry_still",
          method_names.count("carry_still") == 3,
          str(method_names.count("carry_still")))
    check("exact_call_order: 3 add_shot",
          method_names.count("add_shot") == 3,
          str(method_names.count("add_shot")))
    check("exact_call_order: 3 generate_shot",
          method_names.count("generate_shot") == 3,
          str(method_names.count("generate_shot")))
    check("exact_call_order: 3 wait_take",
          method_names.count("wait_take") == 3,
          str(method_names.count("wait_take")))
    check("exact_call_order: 3 pick",
          method_names.count("pick") == 3,
          str(method_names.count("pick")))


def _test_prompts_contain_scene_text():
    """Every still and shot prompt contains the scene text."""
    fb = FakeBackend()
    run = MusicVideoRun(fb)

    windows = forge_master.plan_windows(12.0)
    scenes, _ = forge_master.write_scenes(windows, chat=fb.helper_chat,
                                          has_person=True)

    result = run.run(
        song_job_id="audio_001", photo_name="photo.png",
        style_note="neon", lyrics="", w=576, h=768,
    )

    # Collect still prompts
    still_prompts = []
    shot_prompts = []
    for c in fb.calls:
        if c[0] == "make_still":
            still_prompts.append(c[1][0])
        elif c[0] == "add_shot":
            shot_prompts.append(c[1][2])  # prompt is index 2

    check("prompts: 3 still prompts", len(still_prompts) == 3)
    check("prompts: 3 shot prompts", len(shot_prompts) == 3)

    for i, scene in enumerate(scenes):
        check("prompts: still %d contains scene" % (i + 1),
              scene["scene"] in still_prompts[i],
              still_prompts[i][:100])
        check("prompts: shot %d contains scene" % (i + 1),
              scene["scene"] in shot_prompts[i],
              shot_prompts[i][:100])
        check("prompts: shot %d contains motion" % (i + 1),
              scene["motion"] in shot_prompts[i],
              shot_prompts[i][:100])


def _test_progress_say_calls():
    """Progress say() calls reach done == total at the end."""
    fb = FakeBackend()
    run = MusicVideoRun(fb)

    windows = forge_master.plan_windows(12.0)
    scenes, _ = forge_master.write_scenes(windows, chat=fb.helper_chat,
                                          has_person=True)
    total_steps = 2 * len(windows)  # stills + shots

    result = run.run(
        song_job_id="audio_001", photo_name="photo.png",
        style_note="neon", lyrics="", w=576, h=768,
    )

    # say(stage, message, done, total) -> recorded as ("say", args_tuple, kwargs_dict)
    # where args = (stage, message, done, total)
    say_calls = [c for c in fb.calls if c[0] == "say"]
    # Should have: planning, scenes, stills (n), shots (n), cutting, done
    check("progress: say calls present", len(say_calls) >= 1,
          str(len(say_calls)))

    # The last say should have stage="done" with done == total == total_steps
    last_say = say_calls[-1]
    # last_say[1] is the args tuple: (stage, message, done, total)
    last_args = last_say[1]
    check("progress: last stage is done", last_args[0] == "done",
          str(last_say))
    check("progress: done == total == expected",
          last_args[2] == last_args[3] == total_steps,
          "done=%d total=%d expected=%d" % (last_args[2], last_args[3], total_steps))


def _test_shot_retry_succeeds():
    """A shot that fails once (wait_take False) is retried and succeeds."""
    # Use a counter that stays at 0 for add_shot to produce consistent slot_ids
    fb = FakeBackend(wait_take_ok=True)
    fb._counter = 0  # ensure consistent slot IDs

    run = MusicVideoRun(fb)
    windows = forge_master.plan_windows(12.0)
    scenes, _ = forge_master.write_scenes(windows, chat=fb.helper_chat,
                                          has_person=True)

    result = run.run(
        song_job_id="audio_001", photo_name="photo.png",
        style_note="neon", lyrics="", w=576, h=768,
    )

    # Find the first shot's slot_id (calls stored as (method, args_tuple, kwargs))
    first_slot = None
    for c in fb.calls:
        if c[0] == "generate_shot":
            first_slot = c[1][1]  # (sid, slot_id)
            break
    check("retry: found first slot", first_slot is not None, str(first_slot))

    # Create a fresh backend with the first slot configured to fail once
    fb2 = FakeBackend(wait_take_ok=True)
    fb2._counter = 0
    fb2._shot_failures = {first_slot: 1}

    run2 = MusicVideoRun(fb2)
    result = run2.run(
        song_job_id="audio_001", photo_name="photo.png",
        style_note="neon", lyrics="", w=576, h=768,
    )

    check("retry: succeeds", result["file"] == "video.mp4")

    # The first slot should have been attempted twice
    shot_waits = [c for c in fb2.calls
                  if c[0] == "wait_take" and c[1][1] == first_slot]
    check("retry: shot waited twice", len(shot_waits) == 2,
          str(len(shot_waits)))

    # The first slot should have been generated twice
    shot_gens = [c for c in fb2.calls
                 if c[0] == "generate_shot" and c[1][1] == first_slot]
    check("retry: shot generated twice", len(shot_gens) == 2,
          str(len(shot_gens)))


def _test_shot_fails_twice_raises():
    """A shot failing twice raises RunError with the exact sentence and no
    cut was requested."""
    # First, find the slot_id format used by add_shot
    fb0 = FakeBackend(wait_take_ok=True)
    fb0._counter = 0
    run0 = MusicVideoRun(fb0)
    windows = forge_master.plan_windows(12.0)
    scenes, _ = forge_master.write_scenes(windows, chat=fb0.helper_chat, has_person=True)
    try:
        run0.run(song_job_id="audio_001", photo_name="photo.png",
                 style_note="neon", lyrics="", w=576, h=768)
    except RunError:
        pass

    # Find the first shot's slot_id
    first_slot = None
    for c in fb0.calls:
        if c[0] == "generate_shot":
            first_slot = c[1][1]  # (sid, slot_id)
            break
    check("shot_fails_twice: found first slot", first_slot is not None, str(first_slot))

    # Create a fresh backend that always fails on that slot
    fb = FakeBackend(wait_take_ok=False)
    fb._counter = 0
    fb._shot_failures = {first_slot: 999}

    run = MusicVideoRun(fb)
    try:
        run.run(
            song_job_id="audio_001", photo_name="photo.png",
            style_note="neon", lyrics="", w=576, h=768,
        )
        check("shot_fails_twice: raises", False, "no exception")
    except RunError as e:
        msg = str(e)
        check("shot_fails_twice: exact sentence",
              "Shot 1 failed twice" in msg,
              msg)
        check("shot_fails_twice: cutting room mention",
              "Cutting Room" in msg,
              msg)

    # No cut should have been requested
    method_names = [c[0] for c in fb.calls]
    check("shot_fails_twice: no cut requested",
          "cut" not in method_names,
          method_names)


def _test_check_stop_mid_run():
    """check_stop raising RunStopped mid-run stops before the next step."""
    fb = FakeBackend()
    # Stop after 2 stills
    stop_at = [0]

    original_check_stop = fb.check_stop
    def patched_check_stop():
        fb._record("check_stop_patched")
        stop_at[0] += 1
        if stop_at[0] >= 3:  # after first check_stop
            raise RunStopped()
        return original_check_stop()

    fb.check_stop = patched_check_stop

    windows = forge_master.plan_windows(12.0)
    scenes, _ = forge_master.write_scenes(windows, chat=fb.helper_chat,
                                          has_person=True)

    run = MusicVideoRun(fb)
    try:
        run.run(
            song_job_id="audio_001", photo_name="photo.png",
            style_note="neon", lyrics="", w=576, h=768,
        )
        check("check_stop: raises", False, "no exception")
    except RunStopped:
        check("check_stop: raises RunStopped", True)


def _test_helper_raises_fallback():
    """A helper that raises -> fallback scenes still produce a video."""
    fb = FakeBackend(helper_raises=True)

    run = MusicVideoRun(fb)
    windows = forge_master.plan_windows(12.0)

    result = run.run(
        song_job_id="audio_001", photo_name="photo.png",
        style_note="neon", lyrics="", w=576, h=768,
    )

    check("helper_raises: still produces video", result["file"] == "video.mp4")
    check("helper_raises: uses fallback scenes",
          result["shots"] == 3,
          str(result["shots"]))


def _test_wait_job_timeout():
    """A timeout from wait_job -> RunError."""
    fb = FakeBackend(wait_job_raises_timeout=True)

    run = MusicVideoRun(fb)
    windows = forge_master.plan_windows(12.0)

    try:
        run.run(
            song_job_id="audio_001", photo_name="photo.png",
            style_note="neon", lyrics="", w=576, h=768,
        )
        check("wait_job_timeout: raises", False, "no exception")
    except RunError as e:
        check("wait_job_timeout: raises RunError", "took too long" in str(e).lower(),
              str(e))


def _test_still_retry_and_orientation():
    """A still that fails once is retried; failing twice is an error; the still prompt follows the canvas shape; the plan reaches the backend."""
    class FlakyStills(FakeBackend):
        def __init__(self, fails):
            FakeBackend.__init__(self)
            self.fails, self.plans = fails, []

        def wait_job(self, job_id, timeout):
            self._record("wait_job", job_id, timeout)
            if self.fails > 0:
                self.fails -= 1
                return {"id": job_id, "status": "failed", "outputs": []}
            return {"id": job_id, "status": "done", "outputs": [{"filename": "o.png", "type": "output", "media": "image"}]}

        def plan(self, plan_line, n_shots):
            self.plans.append((plan_line, n_shots))

    fb = FlakyStills(fails=1)
    MusicVideoRun(fb).run(song_job_id="a", photo_name="p.png", style_note="", lyrics="", w=832, h=480)
    stills = [c for c in fb.calls if c[0] == "make_still"]
    check("still retry: a failed still is made again", len(stills) >= 2, len(stills))
    check("landscape canvas -> 'landscape orientation' in the still prompt",
          "landscape orientation" in stills[0][1][0] and "portrait orientation" not in stills[0][1][0], stills[0][1][0][:120])
    check("the plan line reaches the backend", fb.plans and "shots" in fb.plans[0][0] and fb.plans[0][1] >= 1, fb.plans)
    fb2 = FlakyStills(fails=2)
    try:
        MusicVideoRun(fb2).run(song_job_id="a", photo_name="p.png", style_note="", lyrics="", w=576, h=768)
        check("still fails twice: raises", False, "no exception")
    except RunError as e:
        check("still fails twice: the sentence", str(e) == "Still 1 failed to render twice.", str(e))
    fb3 = FlakyStills(fails=0)
    MusicVideoRun(fb3).run(song_job_id="a", photo_name="p.png", style_note="", lyrics="", w=576, h=768)
    check("portrait canvas -> 'portrait orientation'", "portrait orientation" in [c for c in fb3.calls if c[0] == "make_still"][0][1][0])


def _test_no_photo_makes_the_person():
    print("\n--- no photo: the person is made first and used as the reference ---")
    fb = FakeBackend()
    MusicVideoRun(fb).run(song_job_id="a", photo_name=None, style_note="neon night", lyrics="", w=576, h=768,
                          person="a woman with short red hair")
    names = [c[0] for c in fb.calls]
    check("make_portrait is called once, before the first still", names.count("make_portrait") == 1
          and names.index("make_portrait") < names.index("make_still"), names[:8])
    portrait = [c for c in fb.calls if c[0] == "make_portrait"][0][1][0]
    check("the portrait prompt carries the description and the mood",
          "a woman with short red hair" in portrait and "neon night" in portrait, portrait)
    stills = [c for c in fb.calls if c[0] == "make_still"]
    check("every still uses the carried portrait as its reference photo",
          stills and all(c[1][1].startswith("carried_portrait") for c in stills), [c[1][1] for c in stills])
    fb2 = FakeBackend()
    MusicVideoRun(fb2).run(song_job_id="a", photo_name="me.png", style_note="", lyrics="", w=576, h=768)
    check("with a photo no portrait is made", "make_portrait" not in [c[0] for c in fb2.calls])
    fb3 = FakeBackend()
    MusicVideoRun(fb3).run(song_job_id="a", photo_name=None, style_note="", lyrics="", w=576, h=768)
    check("no description: it still makes someone who suits the song",
          "suits the song" in [c for c in fb3.calls if c[0] == "make_portrait"][0][1][0])


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _test_exact_call_order()
    _test_prompts_contain_scene_text()
    _test_progress_say_calls()
    _test_shot_retry_succeeds()
    _test_shot_fails_twice_raises()
    _test_check_stop_mid_run()
    _test_helper_raises_fallback()
    _test_wait_job_timeout()
    _test_still_retry_and_orientation()
    _test_no_photo_makes_the_person()

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): {', '.join(FAILED)}")
    else:
        print("All tests passed.")
    sys.exit(1 if FAILED else 0)

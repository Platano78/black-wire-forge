"""Acceptance gate for UX-2 #9: progress as a percentage, not a raw step
counter ("step 4523/7500" doesn't read as progress -- the owner's brother).

Three things this pins, all pure/unit -- no ComfyUI, no real websocket:

1. `server._stage_percent(n_stages, finished_stages, step, total)`: the
   percent math itself, two-stage and single-stage.
2. `engines.stage_class_types()`: the default KSampler family PLUS the
   audio pack's YuE2GenerateMusic/ABC extras (YuE2's own generator nodes,
   which report step progress under a different class_type).
3. `server.handle_ws_message` end to end against synthetic `progress`/
   `executing` messages, for a two-stage job (e.g. LTX upscale: sampler then
   a second sampler), a single-stage job, and a YuE2 job (two generator
   nodes under YuE2's own class_types) -- percent never decreases, and a
   non-stage node after the last stage seen moves the state to "finishing".

Run: python3 tests/test_progress_percent.py
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
spec = importlib.util.spec_from_file_location("srv_progress_pct", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402

print("engines.stage_class_types(): the default KSampler family, plus the audio pack's YuE2 extras")
classes = engines.stage_class_types()
check("KSampler counts as a stage", "KSampler" in classes)
check("SamplerCustomAdvanced counts as a stage", "SamplerCustomAdvanced" in classes)
check("YuE2GenerateMusic counts as a stage (the audio pack's own class_type)", "YuE2GenerateMusic" in classes)
check("YuE2GenerateABC counts as a stage", "YuE2GenerateABC" in classes)
check("KSamplerSelect is NOT a stage (a sampler-algorithm picker, reports no progress)",
      "KSamplerSelect" not in classes)

print()
print("server._stage_percent(n_stages, finished_stages, step, total): the pure math")
check("single-stage, halfway", srv._stage_percent(1, 0, 50, 100) == 50.0)
check("single-stage, done", srv._stage_percent(1, 0, 100, 100) == 100.0)
check("single-stage, no total yet -> 0", srv._stage_percent(1, 0, 0, 0) == 0.0)
check("two-stage, first stage halfway -> 25%", srv._stage_percent(2, 0, 50, 100) == 25.0)
check("two-stage, first stage finished, second not started -> 50%", srv._stage_percent(2, 1, 0, 0) == 50.0)
check("two-stage, second stage halfway -> 75%", srv._stage_percent(2, 1, 50, 100) == 75.0)
check("two-stage, both finished -> 100%", srv._stage_percent(2, 2, 0, 0) == 100.0)
check("clamped: finished_stages beyond n_stages never exceeds 100", srv._stage_percent(2, 5, 100, 100) == 100.0)

print()
print("handle_ws_message: a two-stage job (LTX-shaped -- SamplerCustomAdvanced then a second sampler)")
lane = {"id": "lane1", "name": "Lane 1"}
srv.LANE_CLIENT_ID[lane["id"]] = "client1"
srv.JOBS.clear()
srv.PROMPT_INDEX.clear()
pid = "prompt-two-stage"
jid = "job-two-stage"
srv.JOBS[jid] = {"id": jid, "lane": lane["id"], "kind": "video", "mode": "t2v", "status": "queued",
                  "step": 0, "total": 0, "started": srv.time.time(), "updated": 0,
                  "stage_nodes": ["7", "12"], "stages_seen": [], "progress_state": "loading",
                  "progress_pct": 0.0}
srv.PROMPT_INDEX[(lane["id"], pid)] = jid


def send(mtype, data):
    srv.handle_ws_message(lane, {"type": mtype, "data": dict(data, prompt_id=pid)})


job = srv.JOBS[jid]
send("executing", {"node": "7"})
check("RED: before any progress event, state is still loading", job["progress_state"] == "loading")
send("progress", {"node": "7", "value": 10, "max": 20})
check("GREEN: first stage halfway -> 25% (1 of 2 stages, 50% through it)", job["progress_pct"] == 25.0, job["progress_pct"])
check("stages_seen has the one stage node", job["stages_seen"] == ["7"])
send("progress", {"node": "7", "value": 20, "max": 20})
check("first stage finished -> 50%", job["progress_pct"] == 50.0, job["progress_pct"])
send("executing", {"node": "12"})
send("progress", {"node": "12", "value": 10, "max": 20})
check("second stage halfway -> 75%", job["progress_pct"] == 75.0, job["progress_pct"])
check("stages_seen now has both stage nodes", job["stages_seen"] == ["7", "12"])
send("progress", {"node": "12", "value": 20, "max": 20})
check("both stages finished -> 100%", job["progress_pct"] == 100.0, job["progress_pct"])
send("executing", {"node": "99"})  # VAEDecode/SaveVideo -- not a stage node
check("a non-stage node after the last stage -> state moves to finishing", job["progress_state"] == "finishing")
check("percent never decreases into finishing", job["progress_pct"] == 100.0, job["progress_pct"])

print()
print("handle_ws_message: a single-stage job (one KSampler) never mis-reports a second stage")
srv.JOBS.clear()
srv.PROMPT_INDEX.clear()
pid2, jid2 = "prompt-single", "job-single"
srv.JOBS[jid2] = {"id": jid2, "lane": lane["id"], "kind": "image", "mode": "t2i", "status": "queued",
                   "step": 0, "total": 0, "started": srv.time.time(), "updated": 0,
                   "stage_nodes": ["8"], "stages_seen": [], "progress_state": "loading", "progress_pct": 0.0}
srv.PROMPT_INDEX[(lane["id"], pid2)] = jid2


def send2(mtype, data):
    srv.handle_ws_message(lane, {"type": mtype, "data": dict(data, prompt_id=pid2)})


job2 = srv.JOBS[jid2]
send2("executing", {"node": "8"})
send2("progress", {"node": "8", "value": 15, "max": 30})
check("GREEN: single-stage halfway -> 50%", job2["progress_pct"] == 50.0, job2["progress_pct"])
send2("progress", {"node": "8", "value": 30, "max": 30})
check("single-stage finished -> 100%", job2["progress_pct"] == 100.0, job2["progress_pct"])

print()
print("handle_ws_message: a YuE2 job -- two generator nodes under the pack's own class_type, "
      "each counted as one stage (never mistaken for KSampler's kind of node)")
srv.JOBS.clear()
srv.PROMPT_INDEX.clear()
pid3, jid3 = "prompt-yue2", "job-yue2"
srv.JOBS[jid3] = {"id": jid3, "lane": lane["id"], "kind": "audio", "mode": "yue2", "status": "queued",
                   "step": 0, "total": 0, "started": srv.time.time(), "updated": 0,
                   "stage_nodes": ["4", "9"], "stages_seen": [], "progress_state": "loading", "progress_pct": 0.0}
srv.PROMPT_INDEX[(lane["id"], pid3)] = jid3


def send3(mtype, data):
    srv.handle_ws_message(lane, {"type": mtype, "data": dict(data, prompt_id=pid3)})


job3 = srv.JOBS[jid3]
send3("executing", {"node": "4"})
send3("progress", {"node": "4", "value": 500, "max": 1000})
check("YuE2 first generator node (ABC) halfway -> 25%", job3["progress_pct"] == 25.0, job3["progress_pct"])
send3("progress", {"node": "4", "value": 1000, "max": 1000})
send3("executing", {"node": "9"})
send3("progress", {"node": "9", "value": 500, "max": 1000})
check("YuE2 second generator node (Music) halfway -> 75%", job3["progress_pct"] == 75.0, job3["progress_pct"])

print()
print("server.job_progress_view: the derived {state, stage, stages, percent, elapsed} the API exposes; "
      "raw step/total stay on the job dict for the tooltip, never in this derived view's main text")
view = srv.job_progress_view(job3)
check("state is sampling mid-render", view["state"] == "sampling", view)
check("stage index reflects one stage seen fully + the current one -> 2 of 2 stage nodes named so far",
      view["stage"] == 2, view)
check("stages is the graph's own stage count (2)", view["stages"] == 2, view)
check("percent matches the job's tracked value", view["percent"] == 75.0, view)
check("elapsed is a number (time since started)", isinstance(view["elapsed"], (int, float)), view)
check("job_progress_view returns None once the job is done (History/Monitor fall back)",
      srv.job_progress_view(dict(job3, status="done")) is None)

print()
print("a job record that predates stage_nodes (loaded from an old jobs.json) falls back to plain step/total")
srv.JOBS.clear()
srv.PROMPT_INDEX.clear()
pid4, jid4 = "prompt-legacy", "job-legacy"
srv.JOBS[jid4] = {"id": jid4, "lane": lane["id"], "kind": "image", "mode": "t2i", "status": "queued",
                   "step": 0, "total": 0, "started": srv.time.time(), "updated": 0}  # no stage_nodes key at all
srv.PROMPT_INDEX[(lane["id"], pid4)] = jid4


def send4(mtype, data):
    srv.handle_ws_message(lane, {"type": mtype, "data": dict(data, prompt_id=pid4)})


job4 = srv.JOBS[jid4]
send4("progress", {"node": "anything", "value": 25, "max": 100})
check("legacy job (no stage_nodes key): percent falls back to plain step/total", job4["progress_pct"] == 25.0,
      job4["progress_pct"])

print()
print("handle_ws_message: a node BETWEEN stage 1 and stage 2 (a latent upscale, a second encode...) "
      "is still sampling, not finishing -- 'Finishing...' is only correct once the LAST stage has started")
srv.JOBS.clear()
srv.PROMPT_INDEX.clear()
pid5, jid5 = "prompt-between-stages", "job-between-stages"
srv.JOBS[jid5] = {"id": jid5, "lane": lane["id"], "kind": "video", "mode": "t2v", "status": "queued",
                   "step": 0, "total": 0, "started": srv.time.time(), "updated": 0,
                   "stage_nodes": ["7", "12"], "stages_seen": [], "progress_state": "loading", "progress_pct": 0.0}
srv.PROMPT_INDEX[(lane["id"], pid5)] = jid5


def send5(mtype, data):
    srv.handle_ws_message(lane, {"type": mtype, "data": dict(data, prompt_id=pid5)})


job5 = srv.JOBS[jid5]
send5("executing", {"node": "7"})
send5("progress", {"node": "7", "value": 10, "max": 10})
check("stage A (node 7) finished -> 50%", job5["progress_pct"] == 50.0, job5["progress_pct"])
send5("executing", {"node": "50"})   # e.g. a latent upscaler node BETWEEN the two samplers
check("RED (the pre-fix defect): a non-stage node between stage 1 and stage 2 stayed 'sampling', "
      "never 'finishing' -- only 1 of 2 stages has been seen",
      job5["progress_state"] == "sampling", job5["progress_state"])
check("percent does not regress while between stages", job5["progress_pct"] == 50.0, job5["progress_pct"])
send5("executing", {"node": "12"})
send5("progress", {"node": "12", "value": 5, "max": 10})
check("stage B (node 12) now sampling, halfway -> 75%", job5["progress_state"] == "sampling"
      and job5["progress_pct"] == 75.0, (job5["progress_state"], job5["progress_pct"]))
send5("progress", {"node": "12", "value": 10, "max": 10})
check("stage B finished -> 100%", job5["progress_pct"] == 100.0, job5["progress_pct"])
send5("executing", {"node": "99"})   # decode/save -- AFTER the last stage
check("GREEN: a non-stage node after BOTH stages have been seen -> finishing", job5["progress_state"] == "finishing",
      job5["progress_state"])
check("percent still 100% into finishing", job5["progress_pct"] == 100.0, job5["progress_pct"])

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)

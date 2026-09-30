"""LORA-2E #1 acceptance gate: the Music3 fused-QKV LoRA conversion --
engines.audio._convert_music3_lora (the pack's own converter, block-diagonal
B + row-concat A, no arithmetic), server.py's generic
_read_safetensors_file/_write_safetensors_file/_convert_lora_file (the
engine-agnostic core), and the REAL download-worker path (_run_lora_download)
that runs it after a verified download -- never a reimplementation of any of
these. numpy is used ONLY here, to independently verify the merge is exact;
production code (engines/audio.py, server.py) needs no numpy at all -- see
their own module comments.

RED reproduces the exact pre-slice absence against 72dd41c's own server.py/
engines/audio.py (loaded via `git show`, never a copy on disk); GREEN runs
the same checks against the real, current modules.

Run: python3 tests/test_music3_lora_convert.py
"""
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below
try:
    import numpy as np  # noqa: E402 -- TEST-ONLY numeric verification
except ImportError:
    print("SKIP: numpy is not installed (requirements.txt) -- this suite's exactness check needs it")
    sys.exit(0)


def load_module_from_source(tag, source_path):
    spec = importlib.util.spec_from_file_location("srv_%s" % tag, source_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


srv = load_module_from_source("m3fixed", os.path.join(ROOT, "server.py"))
sys.path.insert(0, ROOT)
import engines  # noqa: E402
import engines.audio as audio_pack  # noqa: E402

RED_COMMIT = "72dd41c"   # the LORA-2B merge, the commit before LORA-2E
HAVE_RED = subprocess.run(["git", "cat-file", "-e", RED_COMMIT + ":server.py"], cwd=ROOT,
                          capture_output=True).returncode == 0
if HAVE_RED:
    base_src = subprocess.run(["git", "show", "72dd41c:server.py"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout
    red_dir = tempfile.mkdtemp(prefix="bwf_lora2e_red_")
    red_path = os.path.join(red_dir, "server_72dd41c.py")
    open(red_path, "w").write(base_src)
    red = load_module_from_source("m3red", red_path)

    print("RED (72dd41c): no safetensors read/write/convert machinery exists yet")
    check("RED: no _read_safetensors_file()", not hasattr(red, "_read_safetensors_file"))
    check("RED: no _convert_lora_file()", not hasattr(red, "_convert_lora_file"))
    # engines/audio.py reads a sibling audio_writers/*.txt relative to its own
    # file at import time, so the git-show copy (no sibling dir) can't be
    # exec'd the way server.py's copy above is -- a source-text check is exact
    # enough for "this string is/isn't declared yet" and avoids that trap.
    red_audio_src = subprocess.run(["git", "show", "72dd41c:engines/audio.py"], cwd=ROOT, capture_output=True,
                                   text=True, check=True).stdout
    check("RED: 72dd41c's engines/audio.py declares no 'music3_fused_qkv' convert name",
          "music3_fused_qkv" not in red_audio_src)
    check("RED: 72dd41c's engines/audio.py declares no 'lora_converters'",
          "lora_converters" not in red_audio_src)
else:
    print("  SKIP  RED half: commit %s is not in this checkout (a ZIP download or a shallow clone);"
          " the GREEN checks below still run" % RED_COMMIT)

print()
print("GREEN (this branch): the real modules have it all")
check("GREEN: server.py has _read_safetensors_file/_write_safetensors_file/_convert_lora_file",
      hasattr(srv, "_read_safetensors_file") and hasattr(srv, "_write_safetensors_file")
      and hasattr(srv, "_convert_lora_file"))
music3 = next(sc for sc in engines.style_catalogs() if sc["id"] == "music3")
check("GREEN: the music3 family declares convert='music3_fused_qkv'",
      music3.get("convert") == "music3_fused_qkv", music3)
check("GREEN: engines.lora_converter('music3_fused_qkv') resolves to the pack's own function",
      engines.lora_converter("music3_fused_qkv") is audio_pack._convert_music3_lora)
check("GREEN: an undeclared convert name resolves to None",
      engines.lora_converter("nope_not_real") is None)

print()
print("Conversion correctness -- synthetic tiny Music3 PEFT file (2 blocks, small ranks, random)")


def f32(shape, arr):
    flat = np.asarray(arr, dtype="<f4").reshape(shape)
    return {"dtype": "F32", "shape": list(shape), "data": flat.tobytes()}


def f32_scalar(value):
    return {"dtype": "F32", "shape": [], "data": struct.pack("<f", float(value))}


def as_f32(t):
    return np.frombuffer(t["data"], dtype="<f4").reshape(t["shape"])


rng = np.random.default_rng(20260928)


def make_block(i, in_dim=4, out_dim=3, ranks=None, alpha=None, out_alpha=None):
    """One Music3 PEFT block's source keys, plus the (Aq,Bq,Ak,Bk,Av,Bv)
    numpy arrays used to compute the independently-derived expected merge."""
    ranks = ranks or {"to_q": 2, "to_k": 2, "to_v": 2}
    keys, ref = {}, {}
    for proj in ("to_q", "to_k", "to_v"):
        r = ranks[proj]
        a_arr = rng.standard_normal((r, in_dim)).astype("<f4")
        b_arr = rng.standard_normal((out_dim, r)).astype("<f4")
        keys["transformer.transformer_blocks.%d.attn.%s.lora_A.weight" % (i, proj)] = f32((r, in_dim), a_arr)
        keys["transformer.transformer_blocks.%d.attn.%s.lora_B.weight" % (i, proj)] = f32((out_dim, r), b_arr)
        ref[proj] = (a_arr, b_arr)
        if alpha is not None:
            keys["transformer.transformer_blocks.%d.attn.%s.alpha" % (i, proj)] = f32_scalar(alpha)
    r_out = 3
    a_out = rng.standard_normal((r_out, out_dim)).astype("<f4")
    b_out = rng.standard_normal((out_dim, r_out)).astype("<f4")
    keys["transformer.transformer_blocks.%d.attn.to_out.0.lora_A.weight" % i] = f32((r_out, out_dim), a_out)
    keys["transformer.transformer_blocks.%d.attn.to_out.0.lora_B.weight" % i] = f32((out_dim, r_out), b_out)
    if out_alpha is not None:
        keys["transformer.transformer_blocks.%d.attn.to_out.0.alpha" % i] = f32_scalar(out_alpha)
    ref["to_out"] = (a_out, b_out)
    return keys, ref


def build_file(n_blocks=2, **kw):
    tensors, refs = {}, {}
    for i in range(n_blocks):
        keys, ref = make_block(i, **kw)
        tensors.update(keys)
        refs[i] = ref
    return tensors, refs


tensors, refs = build_file()
out = audio_pack._convert_music3_lora(tensors)

expect_keys = set()
for i in refs:
    p = "diffusion_model.diffusion_transformer.transformer.layers.%d.self_attn." % i
    expect_keys |= {p + "to_qkv.lora_A.weight", p + "to_qkv.lora_B.weight",
                    p + "to_out.lora_A.weight", p + "to_out.lora_B.weight"}
check("converted keys are exactly the 4-per-block fused/renamed set (no alpha, none declared)",
      set(out.keys()) == expect_keys, sorted(out.keys()))

for i, ref in refs.items():
    p = "diffusion_model.diffusion_transformer.transformer.layers.%d.self_attn." % i
    a_f = as_f32(out[p + "to_qkv.lora_A.weight"])
    b_f = as_f32(out[p + "to_qkv.lora_B.weight"])
    check("block %d: to_qkv.lora_A shape is (r_q+r_k+r_v, in_dim)" % i,
          list(a_f.shape) == [6, 4], a_f.shape)
    check("block %d: to_qkv.lora_B shape is (3*out_dim, r_q+r_k+r_v)" % i,
          list(b_f.shape) == [9, 6], b_f.shape)
    aq, bq = ref["to_q"]; ak, bk = ref["to_k"]; av, bv = ref["to_v"]
    expect_a = np.concatenate([aq, ak, av], axis=0)
    check("block %d: to_qkv.lora_A is the exact row-concat of Aq,Ak,Av (byte placement, no arithmetic)" % i,
          np.array_equal(a_f, expect_a))
    got_merge = b_f.astype("<f8") @ a_f.astype("<f8")
    expect_merge = np.concatenate([bq.astype("<f8") @ aq.astype("<f8"),
                                   bk.astype("<f8") @ ak.astype("<f8"),
                                   bv.astype("<f8") @ av.astype("<f8")], axis=0)
    check("block %d: B_f @ A_f == vstack(Bq@Aq, Bk@Ak, Bv@Av) EXACTLY (block-diagonal, zero cross-terms)" % i,
          np.array_equal(got_merge, expect_merge))
    a_out, b_out = ref["to_out"]
    check("block %d: to_out is a straight rename (byte-identical A/B, no merge)" % i,
          np.array_equal(as_f32(out[p + "to_out.lora_A.weight"]), a_out)
          and np.array_equal(as_f32(out[p + "to_out.lora_B.weight"]), b_out))

print()
print("Different per-projection ranks (rq=2, rk=3, rv=1) -- the general block-diagonal case")
tensors_u, refs_u = build_file(n_blocks=1, ranks={"to_q": 2, "to_k": 3, "to_v": 1})
out_u = audio_pack._convert_music3_lora(tensors_u)
p0 = "diffusion_model.diffusion_transformer.transformer.layers.0.self_attn."
a_fu = as_f32(out_u[p0 + "to_qkv.lora_A.weight"])
b_fu = as_f32(out_u[p0 + "to_qkv.lora_B.weight"])
check("unequal ranks: to_qkv.lora_A shape sums the three ranks (2+3+1=6)", list(a_fu.shape) == [6, 4], a_fu.shape)
aq_u, bq_u = refs_u[0]["to_q"]; ak_u, bk_u = refs_u[0]["to_k"]; av_u, bv_u = refs_u[0]["to_v"]
got_u = b_fu.astype("<f8") @ a_fu.astype("<f8")
expect_u = np.concatenate([bq_u.astype("<f8") @ aq_u.astype("<f8"),
                           bk_u.astype("<f8") @ ak_u.astype("<f8"),
                           bv_u.astype("<f8") @ av_u.astype("<f8")], axis=0)
check("unequal ranks: the merge is still exact", np.array_equal(got_u, expect_u))

print()
print("Alpha branch -- alpha_f = 3*alpha per projection (LORA-2E #1 spec)")
tensors_a, refs_a = build_file(n_blocks=1, alpha=4.0, out_alpha=2.5)
out_a = audio_pack._convert_music3_lora(tensors_a)
alpha_key = p0 + "to_qkv.alpha"
check("alpha key is written for the fused to_qkv", alpha_key in out_a, sorted(out_a.keys()))
check("alpha_f == 3 * the original per-projection alpha (4.0 -> 12.0)",
      abs(as_f32(out_a[alpha_key]).item() - 12.0) < 1e-6, as_f32(out_a[alpha_key]))
check("to_out's own alpha is a straight copy (not tripled -- it isn't merged)",
      abs(as_f32(out_a[p0 + "to_out.alpha"]).item() - 2.5) < 1e-6)

print()
print("Malformed file -> refused (plain ValueError), original untouched by the caller either way")


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


bad_key = dict(tensors)
bad_key["some.unexpected.key.lora_A.weight"] = f32((2, 4), rng.standard_normal((2, 4)))
check("an unexpected key refuses the whole conversion", _raises(lambda: audio_pack._convert_music3_lora(bad_key)))

missing = {k: v for k, v in tensors.items() if "to_out" not in k or "0.attn.to_out" not in k
           or not k.startswith("transformer.transformer_blocks.0.")}
missing = dict(tensors)
del missing["transformer.transformer_blocks.0.attn.to_out.0.lora_B.weight"]
check("a block missing one of its lora_A/lora_B pairs refuses",
      _raises(lambda: audio_pack._convert_music3_lora(missing)))

mismatched = dict(tensors)
mismatched["transformer.transformer_blocks.0.attn.to_k.lora_A.weight"] = f32((2, 5), rng.standard_normal((2, 5)))
check("mismatched in_dim across q/k/v refuses rather than silently merging wrong shapes",
      _raises(lambda: audio_pack._convert_music3_lora(mismatched)))

mixed_alpha = dict(tensors_a)
del mixed_alpha["transformer.transformer_blocks.0.attn.to_k.alpha"]
check("a block with alpha on some q/k/v keys but not others refuses",
      _raises(lambda: audio_pack._convert_music3_lora(mixed_alpha)))

print()
print("Generic safetensors read/write round-trip (server.py core, stdlib only)")
tmp_dir = tempfile.mkdtemp(prefix="bwf_lora2e_st_")
st_path = os.path.join(tmp_dir, "roundtrip.safetensors")
srv._write_safetensors_file(st_path, tensors)
read_back = srv._read_safetensors_file(st_path)
check("every key round-trips with the same dtype/shape/bytes",
      set(read_back.keys()) == set(tensors.keys())
      and all(read_back[k]["dtype"] == tensors[k]["dtype"] and read_back[k]["shape"] == tensors[k]["shape"]
              and read_back[k]["data"] == tensors[k]["data"] for k in tensors))

print()
print("_convert_lora_file() dispatch (server.py's engine-agnostic wrapper)")
src_path = os.path.join(tmp_dir, "src.safetensors")
dest_path = os.path.join(tmp_dir, "dest.safetensors")
srv._write_safetensors_file(src_path, tensors)
srv._convert_lora_file("music3_fused_qkv", src_path, dest_path)
converted_on_disk = srv._read_safetensors_file(dest_path)
check("the file _convert_lora_file wrote matches _convert_music3_lora's own in-memory output",
      set(converted_on_disk.keys()) == set(out.keys())
      and all(converted_on_disk[k]["data"] == out[k]["data"] for k in out))
check("_convert_lora_file refuses an unregistered convert name",
      _raises(lambda: srv._convert_lora_file("not_a_real_converter", src_path, dest_path)))

print()
print("Download-worker path: _run_lora_download() runs the conversion ONLY for the declaring family")


class _StaticFile(BaseHTTPRequestHandler):
    def do_GET(self):
        with open(self.server.file_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _serve(path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StaticFile)
    httpd.file_path = path
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/file.safetensors" % httpd.server_address[1]


LANE = {"id": "dl_m3", "name": "DL Lane"}

# A: convert_name given (as if this family DOES declare "convert") -- the
# landed file is the CONVERTED one.
httpd_a, url_a = _serve(src_path)
dest_a = os.path.join(tmp_dir, "landed_converted.safetensors")
srv._run_lora_download(LANE, url_a, dest_a, 1 << 20, "owner/pack", "landed_converted.safetensors",
                       convert_name="music3_fused_qkv")
httpd_a.shutdown()
with srv.DOWNLOAD_LOCK:
    state_a = dict(srv.DOWNLOADS.get(LANE["id"]) or {})
landed_a = srv._read_safetensors_file(dest_a) if os.path.exists(dest_a) else {}
check("convert_name set: the download finished ok", state_a.get("ok") is True, state_a)
check("convert_name set: the file that landed on disk is the CONVERTED shape, not the raw download",
      set(landed_a.keys()) == set(out.keys()), sorted(landed_a.keys()))

# B: convert_name NOT given (as if this family declares no "convert", e.g.
# ACE-Step/YuE2/every non-Music3 family) -- the landed file is untouched,
# byte-identical to what was served.
httpd_b, url_b = _serve(src_path)
dest_b = os.path.join(tmp_dir, "landed_raw.safetensors")
srv._run_lora_download(LANE, url_b, dest_b, 1 << 20, "owner/other-pack", "landed_raw.safetensors")
httpd_b.shutdown()
with srv.DOWNLOAD_LOCK:
    state_b = dict(srv.DOWNLOADS.get(LANE["id"]) or {})
landed_b = srv._read_safetensors_file(dest_b) if os.path.exists(dest_b) else {}
check("convert_name omitted (a family that declares none): the download finished ok", state_b.get("ok") is True)
check("convert_name omitted: the landed file is untouched -- same raw diffusers-named keys as the source",
      set(landed_b.keys()) == set(tensors.keys()), sorted(landed_b.keys()))

# C: convert_name given but the SOURCE is malformed -- the whole download
# fails with a clear sentence, and no file (converted OR raw) is left behind.
bad_src_path = os.path.join(tmp_dir, "bad_src.safetensors")
srv._write_safetensors_file(bad_src_path, bad_key)   # bad_key from the malformed-file section above
httpd_c, url_c = _serve(bad_src_path)
dest_c = os.path.join(tmp_dir, "landed_bad.safetensors")
srv._run_lora_download(LANE, url_c, dest_c, 1 << 20, "owner/pack", "landed_bad.safetensors",
                       convert_name="music3_fused_qkv")
httpd_c.shutdown()
with srv.DOWNLOAD_LOCK:
    state_c = dict(srv.DOWNLOADS.get(LANE["id"]) or {})
check("a conversion failure fails the WHOLE download, with a clear sentence", state_c.get("ok") is False)
check("...the sentence names what went wrong (mentions converting)", "convert" in (state_c.get("error") or "").lower(),
      state_c.get("error"))
check("...and no partial file (converted or raw) is left on disk", not os.path.exists(dest_c))

print()
print("Adversarial review fix 1 (2026-09-28): _read_safetensors_file validates data_offsets, "
      "shape/dtype byte length, and caps the header size")


def _old_read_safetensors_file(path):
    """The PRE-FIX reader (verbatim, before the adversarial-review hardening
    below) -- used ONLY here to demonstrate RED: it accepted negative/
    overlapping/out-of-file data_offsets and never cross-checked byte length
    against shape*dtype-size, nor capped the header size."""
    with open(path, "rb") as f:
        header_len_bytes = f.read(8)
        if len(header_len_bytes) != 8:
            raise ValueError("Not a valid safetensors file (short header).")
        header_len = struct.unpack("<Q", header_len_bytes)[0]
        header_json = f.read(header_len)
        if len(header_json) != header_len:
            raise ValueError("Not a valid safetensors file (truncated header).")
        header = json.loads(header_json)
        header.pop("__metadata__", None)
        data_start = 8 + header_len
        old_tensors = {}
        for key, info in header.items():
            shape = info.get("shape")
            offsets = info.get("data_offsets")
            if not (isinstance(shape, list) and isinstance(offsets, list) and len(offsets) == 2):
                raise ValueError("Not a valid safetensors file (bad tensor entry %r)." % key)
            start, end = offsets
            f.seek(data_start + start)
            data = f.read(end - start)
            if len(data) != end - start:
                raise ValueError("Not a valid safetensors file (truncated tensor %r)." % key)
            old_tensors[key] = {"dtype": info.get("dtype"), "shape": shape, "data": data}
    return old_tensors


def _write_raw_safetensors(path, header_obj, payload):
    header_bytes = json.dumps(header_obj).encode("utf-8")
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        f.write(payload)


neg_path = os.path.join(tmp_dir, "neg_offsets.safetensors")
_write_raw_safetensors(neg_path, {"w": {"dtype": "F32", "shape": [1, 1], "data_offsets": [-4, 0]}},
                       b"\xAA\xBB\xCC\xDD")
red_neg = _old_read_safetensors_file(neg_path)
check("RED: the pre-fix reader accepted negative data_offsets (wrong-region read, no exception)",
      red_neg["w"]["data"] != b"\xAA\xBB\xCC\xDD", red_neg["w"]["data"])
check("GREEN: the real reader refuses negative data_offsets", _raises(lambda: srv._read_safetensors_file(neg_path)))

both_neg_path = os.path.join(tmp_dir, "both_neg_offsets.safetensors")
_write_raw_safetensors(both_neg_path, {"w": {"dtype": "F32", "shape": [1], "data_offsets": [-4, -2]}},
                       b"\xAA\xBB\xCC\xDD")
red_both_neg = _old_read_safetensors_file(both_neg_path)
check("RED: the pre-fix reader accepted [-4,-2] (both negative, end>start) with no exception",
      "w" in red_both_neg)
check("GREEN: the real reader refuses [-4,-2] too", _raises(lambda: srv._read_safetensors_file(both_neg_path)))

overlap_path = os.path.join(tmp_dir, "overlap.safetensors")
_write_raw_safetensors(overlap_path,
                       {"a": {"dtype": "U8", "shape": [4], "data_offsets": [0, 4]},
                        "b": {"dtype": "U8", "shape": [4], "data_offsets": [2, 6]}},
                       b"\x00" * 6)
red_overlap = _old_read_safetensors_file(overlap_path)
check("RED: the pre-fix reader accepted overlapping data_offsets between two tensors",
      set(red_overlap.keys()) == {"a", "b"})
check("GREEN: the real reader refuses overlapping data_offsets",
      _raises(lambda: srv._read_safetensors_file(overlap_path)))

nested_overlap_path = os.path.join(tmp_dir, "nested_overlap.safetensors")
_write_raw_safetensors(nested_overlap_path,
                       {"a": {"dtype": "U8", "shape": [10], "data_offsets": [0, 10]},
                        "b": {"dtype": "U8", "shape": [1], "data_offsets": [1, 2]},
                        "c": {"dtype": "U8", "shape": [1], "data_offsets": [5, 6]}},
                       b"\x00" * 10)
check("GREEN: a NESTED overlap (c inside a, not adjacent to b) is also refused -- not just adjacent pairs",
      _raises(lambda: srv._read_safetensors_file(nested_overlap_path)))

past_eof_path = os.path.join(tmp_dir, "past_eof.safetensors")
_write_raw_safetensors(past_eof_path, {"w": {"dtype": "U8", "shape": [100], "data_offsets": [0, 100]}}, b"\x00" * 4)
check("GREEN: the real reader refuses data_offsets that reach past the file",
      _raises(lambda: srv._read_safetensors_file(past_eof_path)))

bad_len_path = os.path.join(tmp_dir, "bad_len.safetensors")
_write_raw_safetensors(bad_len_path, {"w": {"dtype": "F32", "shape": [10, 10], "data_offsets": [0, 4]}}, b"\x00" * 4)
check("GREEN: the real reader refuses a byte length that doesn't match shape*dtype-size",
      _raises(lambda: srv._read_safetensors_file(bad_len_path)))

unknown_dtype_path = os.path.join(tmp_dir, "unknown_dtype.safetensors")
_write_raw_safetensors(unknown_dtype_path,
                       {"w": {"dtype": "NOT_A_REAL_DTYPE", "shape": [1], "data_offsets": [0, 4]}}, b"\x00" * 4)
check("GREEN: the real reader refuses an unrecognised dtype",
      _raises(lambda: srv._read_safetensors_file(unknown_dtype_path)))

huge_header_path = os.path.join(tmp_dir, "huge_header.safetensors")
with open(huge_header_path, "wb") as f:
    f.write(struct.pack("<Q", srv._SAFETENSORS_MAX_HEADER_BYTES + 1))
check("GREEN: the real reader refuses (and never tries to read) a header claiming more than the cap",
      _raises(lambda: srv._read_safetensors_file(huge_header_path)))

print()
print("Adversarial review fix 2 (2026-09-28): _convert_music3_lora refuses empty/all-unrecognized-keys files")


def _old_convert_music3_lora_no_guard(tensors):
    """The PRE-FIX body (no upfront 'any key matches' guard) -- demonstrates
    RED: an empty tensors dict silently returns {} instead of refusing."""
    blocks = {}
    for key, tensor in tensors.items():
        m = audio_pack._MUSIC3_SRC_KEY_RE.match(key)
        if not m:
            raise ValueError("unexpected key %r for a Music3 LoRA" % key)
        idx = int(m.group(1))
        proj = m.group(2)
        suffix = audio_pack._MUSIC3_SUFFIX[m.group(3)]
        blocks.setdefault(idx, {}).setdefault(proj, {})[suffix] = tensor
    return {idx: blocks[idx] for idx in sorted(blocks)}   # stands in for the real per-block build


red_empty = _old_convert_music3_lora_no_guard({})
check("RED: the pre-fix converter silently returned {} for an empty file (no exception)", red_empty == {})
check("GREEN: the real converter refuses an empty file", _raises(lambda: audio_pack._convert_music3_lora({})))
try:
    audio_pack._convert_music3_lora({})
    check("...raised the exact required sentence (empty file)", False)
except ValueError as e:
    check("...raised the exact required sentence (empty file)",
          str(e) == "This file has no Music3 LoRA weights to convert.", str(e))

all_unknown = {"totally.unrelated.key.weight": f32((2, 2), rng.standard_normal((2, 2)))}
red_all_unknown = None
try:
    _old_convert_music3_lora_no_guard(all_unknown)
except ValueError as e:
    red_all_unknown = str(e)
check("RED: the pre-fix converter refused an all-unrecognized-keys file, but with the WRONG (unexpected-key) "
      "sentence, not the required one",
      red_all_unknown is not None and red_all_unknown != "This file has no Music3 LoRA weights to convert.",
      red_all_unknown)
check("GREEN: the real converter refuses an all-unrecognized-keys file",
      _raises(lambda: audio_pack._convert_music3_lora(all_unknown)))
try:
    audio_pack._convert_music3_lora(all_unknown)
    check("...raised the exact required sentence (all-unknown case)", False)
except ValueError as e:
    check("...raised the exact required sentence (all-unknown case)",
          str(e) == "This file has no Music3 LoRA weights to convert.", str(e))

print()
print("End-to-end (adversarial review fix 2): an empty Music3 download fails cleanly through the "
      "REAL download worker, not a silent zero-tensor 'success'")
empty_src_path = os.path.join(tmp_dir, "empty_src.safetensors")
srv._write_safetensors_file(empty_src_path, {})
httpd_e, url_e = _serve(empty_src_path)
dest_e = os.path.join(tmp_dir, "landed_empty.safetensors")
srv._run_lora_download(LANE, url_e, dest_e, 1 << 20, "owner/pack", "landed_empty.safetensors",
                       convert_name="music3_fused_qkv")
httpd_e.shutdown()
with srv.DOWNLOAD_LOCK:
    state_e = dict(srv.DOWNLOADS.get(LANE["id"]) or {})
check("an empty Music3 file fails the download, not ok=True", state_e.get("ok") is False, state_e)
check("...with the exact required sentence in the error",
      "This file has no Music3 LoRA weights to convert." in (state_e.get("error") or ""), state_e.get("error"))
check("...and no file (converted or raw) is left on disk", not os.path.exists(dest_e))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)

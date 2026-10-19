"""Shared by the test_autopack_* suites: the public structural replicas in
tests/fixtures/autopack/ (a fake /object_info, templates in UI and API form, the template
index) and small helpers around engines/_autopack.py. Importing this module puts the repo
root on sys.path; the caller sets GENCENTER_DATA to a scratch directory BEFORE importing it
(engines reads packs from there)."""
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
FIX = os.path.join(HERE, "fixtures", "autopack")
PRIVATE = os.path.join(HERE, "fixtures", "autopack_private")

from engines import _autopack as ap       # noqa: E402
from engines import _comfy                # noqa: E402

NAMES = ("image_flat_t2i", "image_sub_edit", "video_multi_ref_switch", "utility_unslotted_da3",
         "3d_cap3d_moge", "image_billed", "image_preview_only", "image_subst_dit")


def read(path, binary=False):
    with open(path, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as f:
        return f.read()


def load(path):
    return json.loads(read(path))


class Fx:
    """One fixture directory (public replicas by default, the private real templates on request)."""

    def __init__(self, root=FIX):
        self.root = root
        self.oi = load(os.path.join(root, "object_info.json"))
        self.index = load(os.path.join(root, "index.json"))
        self.pools = {p: _comfy.pool_names_from_object_info(self.oi, p) for p in _comfy.POOL_NODES}

    def api(self, name):
        return load(os.path.join(self.root, "api", name + ".api.json"))

    def ui_bytes(self, name):
        return read(os.path.join(self.root, "templates", name + ".json"), binary=True)

    def ui(self, name):
        return json.loads(self.ui_bytes(name))

    def meta(self, name):
        """The template's index entry in the shape the server's _wf_templates gives."""
        for cat in self.index:
            for t in cat["templates"]:
                if t["name"] == name:
                    return {"name": name, "title": t.get("title") or name, "description": t.get("description") or "",
                            "models": list(t.get("models") or []), "local": t.get("openSource") is not False,
                            "category": cat.get("title") or ""}
        raise KeyError(name)

    def synth(self, name, taken=None, **kw):
        return ap.synthesize(name, self.ui(name), self.api(name), self.oi, self.meta(name), self.pools,
                             taken if taken is not None else ap.taken_for(None), lane_name="Test lane",
                             small_card=True, **kw)


def provenance(fx, name, lane="rig"):
    """The keyword arguments create_draft needs besides the synthesized draft."""
    return dict(lane=lane, lane_name="Test lane", template=name, title=fx.meta(name)["title"],
                category=fx.meta(name)["category"],
                versions={"comfyui_version": "0.0.1", "frontend_version": "1.0.0", "templates_version": "2.0.0"},
                template_bytes=fx.ui_bytes(name), converted=fx.api(name), converter="export")

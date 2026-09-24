"""Black Wire Forge turntable renderer (slice B2 of the internal Blender process-lane design doc).

Runs as:  blender -b --factory-startup --python turntable.py -- <args>

Imports ONE .glb, centres its world bounding box at the origin, scales it so
its largest dimension is 2.0, and orbits it under a fixed camera at a fixed
elevation, rendering N PNGs with Cycles -- on the PROCESSOR, never the GPU
(owner ruling 2026-09-22: the graphics card stays with whatever
else lives on this box). A three-light studio (key, fill, rim) plus a world
colour that follows the requested background keeps any material readable
without a backdrop.

Output contract (checked by the B2 live gate):
  <out>/f_0001.png ... f_NNNN.png   RGBA, one per orbit step
  <poster>.png                      frame 1 rendered on a TRANSPARENT film
                                    (always keeps its alpha, even for an
                                    opaque background) -- the gate measures
                                    its alpha bounding box to verify framing
  stdout: "PROGRESS i/N" after every frame (runner.py's progress regex),
          then "DONE"

Deterministic: fixed camera, fixed lights, no random seeds in the scene,
no GPU.
"""
import math
import os
import shutil
import sys

import bpy
import mathutils


def parse_args(argv):
    import argparse
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True,
                    help="directory for f_%04d.png frames")
    ap.add_argument("--poster", required=True)
    ap.add_argument("--frames", type=int, default=72)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--samples", type=int, default=32)
    ap.add_argument("--background", default="dark",
                    choices=["dark", "light", "transparent"])
    ap.add_argument("--elevation", type=float, default=20.0)
    return ap.parse_args(argv)


def _aim_at(ob, target):
    c = ob.constraints.new("TRACK_TO")
    c.target = target
    c.track_axis = "TRACK_NEGATIVE_Z"
    c.up_axis = "UP_Y"


def _area_light(scene, pivot, name, location, energy, wsize):
    ld = bpy.data.lights.new(name, "AREA")
    ld.energy = energy
    ld.size = wsize
    ob = bpy.data.objects.new(name, ld)
    scene.collection.objects.link(ob)
    ob.location = location
    _aim_at(ob, pivot)


def _set_world(colour, transparent):
    """World background colour; mid-grey for transparent film."""
    if bpy.context.scene.world is None:
        bpy.context.scene.world = bpy.data.worlds.new("world")
    w = bpy.context.scene.world
    w.use_nodes = True
    bg = w.node_tree.nodes.get("Background")
    if bg is None:
        bg = w.node_tree.nodes.new("ShaderNodeBackground")
    bg.inputs[0].default_value = (colour[0], colour[1], colour[2], 1.0)
    bg.inputs[1].default_value = 1.0
    bpy.context.scene.render.film_transparent = transparent


def main():
    a = parse_args(sys.argv)
    if not 2 <= a.frames <= 1000 or not 64 <= a.size <= 4096:
        print("frame count or size out of range")
        return 1
    if not os.path.isfile(a.src):
        print("cannot find the input file: %s" % a.src)
        return 1

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    # --- Cycles, CPU only --------------------------------------------------
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"  # the GPU on this box is not ours
    try:  # belt and braces: no GPU compute device, period
        bpy.context.preferences.addons["cycles"].preferences.compute_device_type = "NONE"
    except Exception:
        pass
    scene.cycles.samples = max(1, a.samples)
    try:  # OpenImageDenoise when the build has it
        scene.cycles.use_denoising = True
        scene.cycles.denoiser = "OPENIMAGEDENOISE"
    except Exception:
        pass

    # --- import, bound, centre, scale ---------------------------------------
    bpy.ops.import_scene.gltf(filepath=a.src)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        print("ERROR no mesh in file")
        sys.exit(2)

    corners = [mathutils.Vector(c) for c in (
        (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
        (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1))]
    dg = bpy.context.evaluated_depsgraph_get()
    lo = mathutils.Vector((1e18, 1e18, 1e18))
    hi = mathutils.Vector((-1e18, -1e18, -1e18))
    for o in meshes:
        oe = o.evaluated_get(dg)
        m4 = oe.matrix_world
        for c in oe.bound_box:  # 8 corners, local space
            co = m4 @ mathutils.Vector(c)
            lo = mathutils.Vector(min(lo[i], co[i]) for i in range(3))
            hi = mathutils.Vector(max(hi[i], co[i]) for i in range(3))
    center = (lo + hi) / 2
    maxdim = max(hi.x - lo.x, hi.y - lo.y, hi.z - lo.z)
    if maxdim <= 0:
        print("the uploaded object has no size")
        return 1

    # One EMPTY carries the whole model. Parent at the bbox centre with
    # matrix_parent_inverse (world transforms preserved), then move the
    # empty to the origin: the model's bbox centre lands on (0,0,0).
    empty = bpy.data.objects.new("Turntable", None)
    scene.collection.objects.link(empty)
    empty.location = center
    keep_world = mathutils.Matrix.Translation(center).inverted()
    for o in list(bpy.data.objects):
        if o is empty or o.parent is not None:
            continue
        o.parent = empty
        o.matrix_parent_inverse = keep_world
    empty.location = (0.0, 0.0, 0.0)
    s = 2.0 / maxdim  # largest dimension of the bbox is now 2.0
    empty.scale = (s, s, s)

    # --- camera: fixed, on a circle at `elevation`, tracking the origin ----
    pivot = bpy.data.objects.new("pivot", None)  # at the origin by default
    scene.collection.objects.link(pivot)

    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = 50.0  # sensor 36 mm -> hfov = 2*atan(18/50)
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    _aim_at(cam, pivot)
    # Frame the model to fill the image: distance from the real scaled
    # half-diagonal (<= sqrt(3), tight for long boxes). At 0.9 the model's
    # projected corners still sit inside the frame at every rotation angle
    # (a full box never projects wider than 2*sqrt(2) at this elevation),
    # while the model takes up the frame instead of floating in it.
    r = (hi - lo).length / 2.0 * s  # half-diagonal of the scaled bbox
    fov = 2.0 * math.atan(18.0 / 50.0)
    dist = r / math.sin(fov / 2.0) * 0.9
    el = math.radians(a.elevation)
    cam.location = (dist * math.cos(el), 0.0, dist * math.sin(el))

    # --- studio: key (above-front-left), fill (front-right), rim (behind) ---
    _area_light(scene, pivot, "key", (-1.6, -1.8, 2.2), 600.0, 5.0)
    _area_light(scene, pivot, "fill", (2.0, -1.6, 0.8), 150.0, 3.0)
    _area_light(scene, pivot, "rim", (0.4, 2.4, 1.6), 300.0, 2.0)

    # --- background / world --------------------------------------------------
    if a.background == "dark":
        _set_world((0.02, 0.02, 0.025), transparent=False)
    elif a.background == "light":
        _set_world((0.8, 0.8, 0.8), transparent=False)
    else:  # transparent: film alpha; mid-grey world so bounces stay neutral
        _set_world((0.18, 0.18, 0.18), transparent=True)

    scene.render.resolution_x = a.size
    scene.render.resolution_y = a.size
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.frame_set(1)  # any animation the .glb carries stays put at frame 1

    os.makedirs(a.out, exist_ok=True)
    for i in range(a.frames):
        # The MODEL turns; the camera stays put.
        empty.rotation_euler = (0.0, 0.0, 2.0 * math.pi * i / a.frames)
        frame = os.path.join(a.out, "f_%04d.png" % (i + 1))
        scene.render.filepath = frame
        bpy.ops.render.render(write_still=True)
        print("PROGRESS %d/%d" % (i + 1, a.frames), flush=True)

    # Poster = frame 1 on a TRANSPARENT film, so it keeps its alpha even
    # when the job's background was opaque (the gate measures it).
    if a.background == "transparent":
        shutil.copyfile(os.path.join(a.out, "f_0001.png"), a.poster)
    else:
        empty.rotation_euler = (0.0, 0.0, 0.0)
        scene.render.film_transparent = True
        scene.render.filepath = a.poster
        bpy.ops.render.render(write_still=True)
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())

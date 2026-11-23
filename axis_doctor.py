"""axis_doctor.py -- bake the PSX identity-root pattern into an FBX.

Takes one static mesh object out of an FBX and writes a standalone FBX in
the PSX Cold War Lab end state:

  * root Model Lcl Rotation (0, 0, 0), Lcl Scaling (1, 1, 1)
  * geometry stored in a Y-up frame, facing +Z, units in metres
    (UnitScaleFactor 100)
  * zero-area / sliver triangles removed surgically -- nothing else moves
  * optional floor pivot: the mesh's lowest point sits at y = 0

No Blender exporter setting produces that end state (the exporter always
writes its own axis conversion into the root node), so the pipeline is:

  1. bake the object's full evaluated world transform into its mesh data
  2. delete only the polygons whose every fan triangle is a sliver
  3. rotate the data into the target frame, (x, y, z) -> (x, z, -y)
  4. export with exporter defaults, then fold the root transform away at
     file level (fbx_fold.py) -- the bake Unity's "Bake Axis Conversion"
     checkbox does, performed once here instead of at every import

Geometry preservation is proven, not assumed: the surviving triangle set
is hashed before and after the bake (exact match), or -- when float32
re-storage at large bake scales jitters coordinates -- compared as a
sorted vertex cloud with a max-deviation figure. Both numbers land in
the JSON report.

Scope: ONE static mesh object per run. Modifiers on the object
(including an armature modifier) are dropped after the evaluated bake;
armature / animation data is NOT carried into the output. See the
README's Limitations.

Usage (Blender 4.2+):

    blender --background --python axis_doctor.py -- \
        --input in.fbx --output out.fbx \
        [--object Name] [--floor] [--sliver-eps 1e-6] \
        [--report report.json] [--keep-raw raw.fbx]

--object may be omitted when the file holds exactly one mesh object.
"""
import argparse
import hashlib
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bpy  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Matrix  # noqa: E402

import fbx_fold  # noqa: E402


# ---------------------------------------------------------------------------
# mesh measurement helpers (all sliver tests on evaluated world geometry --
# Blender's importer leaves matrix_world stale headless; see README)
# ---------------------------------------------------------------------------

def tris_and_verts(me, Mnp=None):
    me.calc_loop_triangles()
    nv = len(me.vertices)
    nt = len(me.loop_triangles)
    tris = np.empty(nt * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(-1, 3)
    verts = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    if Mnp is not None:
        verts = verts @ Mnp[:3, :3].T + Mnp[:3, 3]
    return tris, verts


def sliver_mask(tris, verts, eps):
    a = verts[tris[:, 1]] - verts[tris[:, 0]]
    b = verts[tris[:, 2]] - verts[tris[:, 0]]
    return np.sqrt((np.cross(a, b) ** 2).sum(axis=1)) < eps


def tri_hash(tris, verts):
    tv = np.round(verts[tris], 5)
    keys = sorted(tuple(sorted(tuple(v) for v in tri)) for tri in tv)
    h = hashlib.sha256()
    for k in keys:
        h.update(repr(k).encode())
    return h.hexdigest()


def cloud(tris, verts):
    return verts[tris].reshape(-1, 3)


def sorted_cloud(c):
    order = np.lexsort((c[:, 2], c[:, 1], c[:, 0]))
    return c[order]


def preservation_check(ref_hash, ref_cloud, tris, verts, keep_mask):
    """Compare a surviving triangle set against the world-frame reference.
    Exact hash when float storage allows it, else sorted-cloud deviation."""
    h = tri_hash(tris[keep_mask], verts)
    if h == ref_hash:
        return {"method": "exact-hash", "value": 0.0}
    a = sorted_cloud(ref_cloud)
    b = sorted_cloud(cloud(tris[keep_mask], verts))
    if a.shape != b.shape:
        return {"method": "shape-mismatch", "value": None,
                "ref_points": int(len(a)), "points": int(len(b))}
    return {"method": "max-deviation", "value": float(np.abs(a - b).max())}


# ---------------------------------------------------------------------------
# main pipeline
# ---------------------------------------------------------------------------

def process(args):
    rep = {"tool": "axis-doctor", "input": args.input, "floor": args.floor,
           "sliver_eps": args.sliver_eps}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=args.input)

    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if args.object:
        ob = bpy.data.objects.get(args.object)
        if ob is None or ob.type != 'MESH':
            raise SystemExit(f"object {args.object!r} not found as a mesh; "
                             f"meshes present: {[o.name for o in meshes]}")
    elif len(meshes) == 1:
        ob = meshes[0]
    else:
        raise SystemExit(f"--object required: file holds {len(meshes)} meshes: "
                         f"{[o.name for o in meshes]}")
    rep["object"] = ob.name
    rep["parent"] = ob.parent.name if ob.parent else None
    rep["object_before"] = {
        "rot_deg": [round(math.degrees(a), 2) for a in ob.rotation_euler],
        "scale": [round(float(s), 4) for s in ob.scale],
        "loc": [round(float(v), 3) for v in ob.location],
    }
    rep["modifiers_dropped"] = [f"{m.name} ({m.type})" for m in ob.modifiers]

    # evaluated world transform -- the ONLY trustworthy matrix headless
    dg = bpy.context.evaluated_depsgraph_get()
    M = ob.evaluated_get(dg).matrix_world.copy()
    Mnp = np.array(M)

    tris, wverts = tris_and_verts(ob.data, Mnp)
    mask = sliver_mask(tris, wverts, args.sliver_eps)
    rep["before"] = {
        "verts": int(len(wverts)), "tris": int(len(tris)),
        "slivers": int(mask.sum()),
        "world_bounds_min": [round(float(v), 4) for v in wverts.min(axis=0)],
        "world_bounds_max": [round(float(v), 4) for v in wverts.max(axis=0)],
    }
    ref_hash = tri_hash(tris[~mask], wverts)
    ref_cloud = cloud(tris[~mask], wverts)

    # STEP 1: bake the full evaluated world transform into the data
    ob.data.transform(M)
    ob.parent = None
    ob.matrix_basis = Matrix.Identity(4)
    ob.location = (0, 0, 0)
    ob.rotation_euler = (0, 0, 0)
    ob.scale = (1, 1, 1)
    for o in list(bpy.context.scene.objects):
        if o != ob:
            bpy.data.objects.remove(o, do_unlink=True)
    ob.modifiers.clear()
    tris_b, verts_b = tris_and_verts(ob.data)
    mb = sliver_mask(tris_b, verts_b, args.sliver_eps)
    rep["bake_check"] = preservation_check(ref_hash, ref_cloud, tris_b, verts_b, ~mb)

    # STEP 2: surgical sliver removal -- only polygons whose EVERY fan
    # triangle is a sliver; surviving geometry is never touched
    me = ob.data
    me.calc_loop_triangles()
    poly_bad, poly_seen = {}, {}
    for i, lt in enumerate(me.loop_triangles):
        poly_seen[lt.polygon_index] = poly_seen.get(lt.polygon_index, 0) + 1
        if mb[i]:
            poly_bad[lt.polygon_index] = poly_bad.get(lt.polygon_index, 0) + 1
    doomed = sorted(p for p, n in poly_bad.items() if n == poly_seen[p])
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in doomed], context='FACES')
    bm.to_mesh(me)
    bm.free()
    me.update()
    tris_d, verts_d = tris_and_verts(me)
    rep["cleanup"] = {
        "polygons_deleted": len(doomed),
        "tris_after": int(len(tris_d)),
        "slivers_after": int(sliver_mask(tris_d, verts_d, args.sliver_eps).sum()),
        "surviving_check": preservation_check(
            ref_hash, ref_cloud, tris_d, verts_d,
            np.ones(len(tris_d), dtype=bool)),
    }

    # STEP 3: rotate data into the target frame (x,y,z) -> (x,z,-y);
    # optional floor pivot so the lowest point sits at y = 0
    me.transform(Matrix.Rotation(math.radians(-90), 4, 'X'))
    me.update()
    tris_e, verts_e = tris_and_verts(me)
    floor_off = None
    if args.floor:
        floor_off = float(verts_e[:, 1].min())
        me.transform(Matrix.Translation((0, -floor_off, 0)))
        me.update()
        tris_e, verts_e = tris_and_verts(me)
    rep["unity_frame"] = {
        "floor_offset_applied": (round(floor_off, 4) if floor_off is not None else None),
        "bounds_min": [round(float(v), 4) for v in verts_e.min(axis=0)],
        "bounds_max": [round(float(v), 4) for v in verts_e.max(axis=0)],
        "tris": int(len(tris_e)),
    }

    # STEP 4: export with defaults, then fold the root away at file level
    bpy.ops.object.select_all(action='DESELECT')
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    if args.keep_raw:
        raw_path = args.keep_raw
    else:
        import tempfile
        raw_path = os.path.join(tempfile.mkdtemp(prefix="axis-doctor-"), "raw.fbx")
    bpy.ops.export_scene.fbx(filepath=raw_path, use_selection=True)

    raw_info = fbx_fold.inspect(raw_path)
    actions = fbx_fold.fold_models(raw_path, args.output, [ob.name])
    fin_info = fbx_fold.inspect(args.output)
    rep["fold"] = {
        "actions": actions,
        "raw_root": raw_info["models"].get(ob.name),
        "final_root": fin_info["models"].get(ob.name),
        "final_globals": {k: (v.item() if hasattr(v, "item") else v)
                          for k, v in fin_info["globals"].items()},
        "geometry_identical_raw_vs_final": (
            raw_info["geometry"] == fin_info["geometry"]),
    }
    rep["output"] = args.output
    return rep


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(description="Bake the PSX identity-root pattern into an FBX.")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--object", default=None)
    ap.add_argument("--floor", action="store_true",
                    help="Translate the data so its lowest point sits at y = 0.")
    ap.add_argument("--sliver-eps", type=float, default=1e-6,
                    help="|cross| threshold for a sliver (default 1e-6 = "
                         "face area < 0.5 mm^2, world space).")
    ap.add_argument("--report", default=None, help="Write the JSON report here.")
    ap.add_argument("--keep-raw", default=None,
                    help="Also keep the unfolded exporter output at this path.")
    args = ap.parse_args(argv)

    rep = process(args)
    text = json.dumps(rep, indent=1)
    if args.report:
        with open(args.report, "w") as f:
            f.write(text)
    b, c = rep["before"], rep["cleanup"]
    print(f"BEFORE:  {b['verts']} verts | {b['tris']} tris | {b['slivers']} slivers")
    print(f"CLEANUP: {c['polygons_deleted']} polygons deleted | {c['tris_after']} tris | "
          f"{c['slivers_after']} slivers | bake check: {rep['bake_check']}")
    print(f"FOLD:    root -> {rep['fold']['final_root']} | "
          f"geometry raw==final: {rep['fold']['geometry_identical_raw_vs_final']}")
    print(f"DONE -> {rep['output']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""axis-doctor test suite.

Run with a system Python 3 (needs numpy):

    python3 tests/run_tests.py

Environment:
    BLENDER              path to the Blender binary (default: 4.2.3)
    AXIS_DOCTOR_AVATAR_FBX
                         path to the avatar test FBX (default: the
                         local fixture copy described below)

The avatar tests run on COPIES in a temp dir; the source file is only
ever read to be copied. The test asset is a female avatar with a
Christmas dress (two extractable meshes: a generated gown and a face);
its expected numbers are the independently measured values from the
pattern evaluation this tool was built from.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import fbx_fold  # noqa: E402

BLENDER = os.environ.get(
    "BLENDER",
    os.path.expanduser("~/workspace/tools/blender-4.2.3-linux-x64/blender"))
BLENDER_HOME = os.path.dirname(BLENDER)
AVATAR_FBX = os.environ.get(
    "AXIS_DOCTOR_AVATAR_FBX",
    os.path.expanduser("~/workspace/test-fixtures/avatar-christmas-dress.fbx"))

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=900, **kw)


def blender_script(script_text, workdir):
    path = os.path.join(workdir, "inline_script.py")
    with open(path, "w") as f:
        f.write(script_text)
    return run([BLENDER, "--background", "--python", path])


def run_axis_doctor(src, out, workdir, extra=()):
    report = os.path.join(workdir, "report.json")
    r = run([BLENDER, "--background", "--python",
             os.path.join(ROOT, "axis_doctor.py"), "--",
             "--input", src, "--output", out, "--report", report, *extra])
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise RuntimeError("axis_doctor failed")
    with open(report) as f:
        return json.load(f)


def approx(a, b, tol=1e-4):
    return a is not None and abs(float(a) - float(b)) <= tol


def root_is_identity(info, name):
    m = info["models"].get(name)
    if not m:
        return False
    rot, scl = m["Lcl Rotation"], m["Lcl Scaling"]
    rot_ok = rot is None or all(approx(x, 0.0, 1e-6) for x in rot)
    scl_ok = scl is None or all(approx(x, 1.0, 1e-6) for x in scl)
    return rot_ok and scl_ok


def globals_ok(info):
    g = info["globals"]
    return (g.get("UpAxis") == 1 and g.get("FrontAxis") == 2
            and g.get("FrontAxisSign") == 1
            and approx(g.get("UnitScaleFactor"), 100.0, 1e-9))


def main():
    workdir = tempfile.mkdtemp(prefix="axis-doctor-tests-")
    print(f"workdir: {workdir}")
    print(f"blender: {BLENDER}")

    # ------------------------------------------------------------------
    # T1: fold machinery roundtrip on a synthetic exporter file
    # ------------------------------------------------------------------
    fixture = os.path.join(workdir, "fixture.fbx")
    r = blender_script(f'''
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1.2, location=(0, 0, 0.6))
cube = bpy.context.active_object
cube.rotation_euler = (0.3, 0.0, 0.7)
cube.scale = (2.0, 1.0, 1.0)
bpy.ops.mesh.primitive_cone_add(radius1=0.4, depth=0.9, location=(1.5, 0, 0.45))
bpy.ops.export_scene.fbx(filepath={fixture!r})
''', workdir)
    check("T1 fixture exported", r.returncode == 0 and os.path.exists(fixture))
    n, bad = fbx_fold.roundtrip_check(fixture, blender_home=BLENDER_HOME)
    check("T1 roundtrip value-identical", not bad and n > 100,
          f"elements={n} mismatches={len(bad)} {bad[:3]}")

    # ------------------------------------------------------------------
    # T2/T3: female avatar with Christmas dress, end-to-end (on a copy)
    # ------------------------------------------------------------------
    src = os.path.join(workdir, "avatar_source.fbx")
    shutil.copy2(AVATAR_FBX, src)

    # --- GenGown: axis-bake case, floored ---
    gout = os.path.join(workdir, "gengown_baked.fbx")
    rep = run_axis_doctor(src, gout, workdir,
                          extra=("--object", "GenGown", "--floor"))
    b, c = rep["before"], rep["cleanup"]
    check("T2 GenGown before", (b["verts"], b["tris"], b["slivers"]) == (17716, 34663, 3),
          f"{b}")
    check("T2 GenGown cleanup", (c["polygons_deleted"], c["tris_after"], c["slivers_after"]) == (3, 34660, 0),
          f"{c}")
    bc = rep["bake_check"]
    check("T2 GenGown bake preservation", bc["value"] is not None and bc["value"] < 1e-5,
          f"{bc} (evaluation measured 5.5e-08)")
    uf = rep["unity_frame"]
    check("T2 GenGown floored at y=0", approx(uf["bounds_min"][1], 0.0, 1e-6)
          and approx(uf["bounds_max"][1], 1.3251), f"{uf['bounds_min']}..{uf['bounds_max']}")
    info = fbx_fold.inspect(gout, blender_home=BLENDER_HOME)
    check("T2 GenGown root identity", root_is_identity(info, "GenGown"),
          f"{info['models'].get('GenGown')}")
    check("T2 GenGown globals", globals_ok(info), f"{info['globals']}")
    check("T2 GenGown fold geometry identical", rep["fold"]["geometry_identical_raw_vs_final"])
    geo = info["geometry"][0]
    check("T2 GenGown file geometry", (geo["tris"], geo["slivers"]) == (34660, 0), f"{geo}")

    # --- Face: sliver case, exact hash, no floor ---
    fout = os.path.join(workdir, "face_baked.fbx")
    rep = run_axis_doctor(src, fout, workdir, extra=("--object", "Face"))
    b, c = rep["before"], rep["cleanup"]
    check("T3 Face before", (b["verts"], b["tris"], b["slivers"]) == (1163, 1958, 124),
          f"{b}")
    check("T3 Face cleanup", (c["polygons_deleted"], c["tris_after"], c["slivers_after"]) == (124, 1834, 0),
          f"{c}")
    check("T3 Face bake exact-hash", rep["bake_check"]["method"] == "exact-hash",
          f"{rep['bake_check']}")
    check("T3 Face surviving exact-hash", c["surviving_check"]["method"] == "exact-hash",
          f"{c['surviving_check']}")
    info_f = fbx_fold.inspect(fout, blender_home=BLENDER_HOME)
    check("T3 Face root identity", root_is_identity(info_f, "Face"),
          f"{info_f['models'].get('Face')}")
    check("T3 Face globals", globals_ok(info_f), f"{info_f['globals']}")
    geo_f = info_f["geometry"][0]
    check("T3 Face file geometry", (geo_f["verts"], geo_f["tris"], geo_f["slivers"]) == (1044, 1834, 0),
          f"{geo_f} (119 sliver-only verts dropped by the exporter)")

    # ------------------------------------------------------------------
    # T4: folded files re-import cleanly, scale exactly 1, no slivers
    # ------------------------------------------------------------------
    reimp = os.path.join(workdir, "reimport.json")
    r = blender_script(f'''
import bpy, json
import numpy as np
out = {{}}
for label, path in (("gengown", {gout!r}), ("face", {fout!r})):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=path)
    ob = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    dg = bpy.context.evaluated_depsgraph_get()
    M = np.array(ob.evaluated_get(dg).matrix_world)
    me = ob.data
    me.calc_loop_triangles()
    tris = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tris); tris = tris.reshape(-1, 3)
    verts = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", verts); verts = verts.reshape(-1, 3)
    verts = verts @ M[:3, :3].T + M[:3, 3]
    a = verts[tris[:, 1]] - verts[tris[:, 0]]; b = verts[tris[:, 2]] - verts[tris[:, 0]]
    mag = np.sqrt((np.cross(a, b) ** 2).sum(axis=1))
    out[label] = {{"scale": [float(s) for s in ob.scale], "slivers": int((mag < 1e-6).sum())}}
with open({reimp!r}, "w") as f: json.dump(out, f)
print(json.dumps(out))
''', workdir)
    ok = r.returncode == 0 and os.path.exists(reimp)
    data = {}
    if ok:
        with open(reimp) as f:
            data = json.load(f)
    for label in ("gengown", "face"):
        d = data.get(label, {})
        check(f"T4 reimport {label}",
              bool(d) and all(approx(s, 1.0, 1e-6) for s in d.get("scale", []))
              and d.get("slivers") == 0, f"{d}")

    # ------------------------------------------------------------------
    # T5: no-op control -- an already-baked file passes through unchanged
    # ------------------------------------------------------------------
    nout = os.path.join(workdir, "face_rebaked.fbx")
    rep = run_axis_doctor(fout, nout, workdir)
    check("T5 no-op slivers stay 0", rep["before"]["slivers"] == 0
          and rep["cleanup"]["slivers_after"] == 0, f"{rep['cleanup']}")
    sc = rep["cleanup"]["surviving_check"]
    check("T5 no-op geometry unchanged", sc["value"] is not None and sc["value"] < 1e-5,
          f"{sc}")
    info_n = fbx_fold.inspect(nout, blender_home=BLENDER_HOME)
    gn, gf = info_n["geometry"][0], geo_f
    check("T5 no-op file geometry matches",
          (gn["verts"], gn["tris"], gn["slivers"]) == (gf["verts"], gf["tris"], gf["slivers"])
          and all(approx(x, y) for x, y in zip(gn["bounds_min"], gf["bounds_min"]))
          and all(approx(x, y) for x, y in zip(gn["bounds_max"], gf["bounds_max"])),
          f"{gn} vs {gf}")
    check("T5 no-op root identity", root_is_identity(info_n, "Face"))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} PASS")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

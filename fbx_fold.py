"""fbx_fold.py -- file-level FBX root folding + inspection for axis-doctor.

Blender's FBX exporter always encodes its axis conversion in the file's
root Model node (Lcl Rotation (-90, 0, 0), Lcl Scaling (100, 100, 100),
geometry stored in Blender's frame). No exporter setting produces an
identity root. The PSX Cold War Lab pattern -- root rotation (0,0,0),
scale 1, geometry already standing in a Y-up / +Z-front frame -- therefore
needs one extra step after export: fold the root transform away at file
level. Zero the root Model's Lcl Rotation / Lcl Scaling and set
UnitScaleFactor to 100 so the stored metre units stay metres with the
root scale gone. That is the same bake Unity's "Bake Axis Conversion"
import checkbox performs, done once at export time instead of at every
import forever.

The parse -> convert -> re-encode machinery uses Blender's own
io_scene_fbx modules, so this file runs either inside Blender or under a
system Python that can find a Blender install (see _ensure_io_scene_fbx).

ONLY fold files whose geometry has already been baked into the target
frame (e.g. the raw exports axis_doctor.py produces). Folding an ordinary
FBX whose data still lives in Blender's frame would corrupt its pose:
the fold removes the conversion without applying it.
"""
import os
import sys

_io_scene_fbx_ready = False


def _find_addons_core(blender_home=None):
    """Locate a Blender ``addons_core`` folder (the one holding io_scene_fbx).

    blender_home may point at a Blender install root (the folder that
    contains a version folder such as ``4.2/``) or directly at an
    ``addons_core`` folder. Without it, BLENDER_HOME and a few common
    install locations are searched.
    """
    import glob
    candidates = []
    if blender_home:
        candidates.append(blender_home)
    if os.environ.get("BLENDER_HOME"):
        candidates.append(os.environ["BLENDER_HOME"])
    try:
        import bpy
        # .../blender-x.y.z-linux-x64/blender -> .../blender-x.y.z-linux-x64
        candidates.append(os.path.dirname(os.path.abspath(bpy.app.binary_path)))
    except Exception:
        pass
    candidates += sorted(glob.glob(os.path.expanduser("~/workspace/tools/blender-*-linux-x64")))
    candidates += sorted(glob.glob("/opt/blender*"))
    candidates += sorted(glob.glob("/usr/share/blender*"))
    candidates += sorted(glob.glob("/snap/blender/current"))
    for cand in candidates:
        if not cand:
            continue
        paths = [cand]
        if os.path.basename(cand) != "addons_core":
            paths += sorted(glob.glob(os.path.join(cand, "*", "scripts", "addons_core")))
            paths.append(os.path.join(cand, "scripts", "addons_core"))
        for p in paths:
            if os.path.isfile(os.path.join(p, "io_scene_fbx", "parse_fbx.py")):
                return p
    return None


def _ensure_io_scene_fbx(blender_home=None):
    """Make the bpy-free io_scene_fbx submodules importable.

    parse_fbx / encode_bin / data_types never import bpy, but the addon
    package's __init__.py does -- so outside Blender the package is
    registered as a bare namespace stub pointing at the real folder and
    the submodules load without executing __init__.py. Inside Blender
    the plain import succeeds first and this is all skipped.
    """
    global _io_scene_fbx_ready
    if _io_scene_fbx_ready:
        return
    try:
        import io_scene_fbx.parse_fbx  # noqa: F401  (inside Blender)
        _io_scene_fbx_ready = True
        return
    except ImportError:
        pass
    addons_core = _find_addons_core(blender_home)
    if addons_core is None:
        raise RuntimeError(
            "io_scene_fbx not found. Run inside Blender, or pass --blender-home "
            "pointing at a Blender install (or set BLENDER_HOME)."
        )
    import types
    pkg = types.ModuleType("io_scene_fbx")
    pkg.__path__ = [os.path.join(addons_core, "io_scene_fbx")]
    pkg.__package__ = "io_scene_fbx"
    sys.modules["io_scene_fbx"] = pkg
    import io_scene_fbx.parse_fbx  # noqa: F401
    _io_scene_fbx_ready = True


def _imports(blender_home=None):
    _ensure_io_scene_fbx(blender_home)
    from io_scene_fbx.parse_fbx import parse
    from io_scene_fbx import encode_bin, data_types as dt
    return parse, encode_bin, dt


# Element finders over the parse tree (direct children only) -- local
# equivalents of the two helpers io_scene_fbx.import_fbx provides, so the
# bpy-dependent importer module never has to load.

def elem_find_first(elem, id_search, default=None):
    for fbx_item in elem.elems:
        if fbx_item.id == id_search:
            return fbx_item
    return default


def elem_find_iter(elem, id_search):
    for fbx_item in elem.elems:
        if fbx_item.id == id_search:
            yield fbx_item


# ---------------------------------------------------------------------------
# parse tree -> encode tree conversion (generic; dispatched on props_type)
# ---------------------------------------------------------------------------

def _add_map(dt):
    return {
        dt.BOOL: "add_bool", dt.CHAR: "add_char", dt.INT8: "add_int8",
        dt.INT16: "add_int16", dt.INT32: "add_int32", dt.INT64: "add_int64",
        dt.FLOAT32: "add_float32", dt.FLOAT64: "add_float64",
        dt.BYTES: "add_bytes", dt.STRING: "add_string",
        dt.INT32_ARRAY: "add_int32_array", dt.INT64_ARRAY: "add_int64_array",
        dt.FLOAT32_ARRAY: "add_float32_array", dt.FLOAT64_ARRAY: "add_float64_array",
        dt.BOOL_ARRAY: "add_bool_array", dt.BYTE_ARRAY: "add_byte_array",
    }


def convert(elem, encode_bin, dt):
    """Rebuild a parse_fbx element tree as an encode_bin element tree."""
    add = _add_map(dt)
    out = encode_bin.FBXElem(elem.id)
    for value, tcode in zip(elem.props, elem.props_type):
        getattr(out, add[tcode])(value)
    for child in elem.elems:
        out.elems.append(convert(child, encode_bin, dt))
    return out


def props70_map(elem):
    """name -> P elem for an element's Properties70 block (parse tree)."""
    out = {}
    if elem is None:
        return out
    p70 = elem_find_first(elem, b'Properties70')
    if p70 is None:
        return out
    for p in p70.elems:
        if p.id == b'P':
            out[p.props[0].decode()] = p
    return out


# ---------------------------------------------------------------------------
# the fold
# ---------------------------------------------------------------------------

def fold_models(src, dst, model_names=None, blender_home=None):
    """Zero Lcl Rotation/Scaling on the named Models (or all Models when
    model_names is None), set UnitScaleFactor to 100, write to dst.

    Returns the list of actions taken. The geometry elements are never
    touched -- the fold only rewrites transform properties.
    """
    parse, encode_bin, dt = _imports(blender_home)
    root, ver = parse(src)
    objs = elem_find_first(root, b'Objects')
    hit = []
    for m in elem_find_iter(objs, b'Model'):
        name_props = [p for p in m.props if isinstance(p, bytes)]
        decoded = [p.split(b'\x00\x01')[0].decode(errors="replace") for p in name_props]
        if model_names is not None and not any(n in decoded for n in model_names):
            continue
        label = decoded[0] if decoded else "?"
        pm = props70_map(m)
        for key, val in (("Lcl Rotation", [0.0, 0.0, 0.0]),
                         ("Lcl Scaling", [1.0, 1.0, 1.0])):
            p = pm.get(key)
            if p is not None:
                p.props[4:7] = val
                hit.append(f"{label}: {key} -> {val}")
            else:
                hit.append(f"{label}: {key} (absent)")
    gs = props70_map(elem_find_first(root, b'GlobalSettings'))
    if "UnitScaleFactor" in gs:
        gs["UnitScaleFactor"].props[4] = 100.0
        hit.append("UnitScaleFactor=100")
    enc_root = convert(root, encode_bin, dt)
    encode_bin.write(dst, enc_root, ver)
    return hit


# ---------------------------------------------------------------------------
# roundtrip self-check: parse -> convert -> write -> parse, compare everything
# ---------------------------------------------------------------------------

def _norm(value):
    import array
    if isinstance(value, array.array):
        return list(value)
    return value


def _compare_elems(a, b, path, mismatches):
    if a.id != b.id:
        mismatches.append(f"{path}: id {a.id!r} != {b.id!r}")
        return 1
    count = 1
    if len(a.props) != len(b.props):
        mismatches.append(f"{path}/{a.id}: prop count {len(a.props)} != {len(b.props)}")
    else:
        for i, (va, vb) in enumerate(zip(a.props, b.props)):
            if _norm(va) != _norm(vb):
                mismatches.append(f"{path}/{a.id}: prop {i} differs")
    if len(a.elems) != len(b.elems):
        mismatches.append(f"{path}/{a.id}: child count {len(a.elems)} != {len(b.elems)}")
        return count
    for ca, cb in zip(a.elems, b.elems):
        count += _compare_elems(ca, cb, f"{path}/{a.id.decode(errors='replace')}", mismatches)
    return count


def roundtrip_check(path, blender_home=None):
    """Validate the parse->re-encode machinery on a real file: every
    element of the re-encoded file must reproduce the original's values
    exactly. Returns (element_count, mismatches)."""
    import tempfile
    parse, encode_bin, dt = _imports(blender_home)
    root_a, ver = parse(path)
    enc = convert(root_a, encode_bin, dt)
    fd, tmp = tempfile.mkstemp(suffix=".fbx")
    os.close(fd)
    try:
        encode_bin.write(tmp, enc, ver)
        root_b, _ = parse(tmp)
    finally:
        os.unlink(tmp)
    mismatches = []
    count = _compare_elems(root_a, root_b, "", mismatches)
    return count, mismatches


# ---------------------------------------------------------------------------
# inspection helpers (file-level evidence; no Blender scene involved)
# ---------------------------------------------------------------------------

def model_info(root, name):
    objs = elem_find_first(root, b'Objects')
    for m in elem_find_iter(objs, b'Model'):
        names = [p for p in m.props if isinstance(p, bytes)]
        if any(name.encode() in p for p in names):
            pm = props70_map(m)
            return {k: ([float(x) for x in pm[k].props[4:7]] if k in pm else None)
                    for k in ("Lcl Translation", "Lcl Rotation", "Lcl Scaling")}
    return None


def list_models(root):
    objs = elem_find_first(root, b'Objects')
    out = {}
    for m in elem_find_iter(objs, b'Model'):
        names = [p.split(b'\x00\x01')[0].decode(errors="replace")
                 for p in m.props if isinstance(p, bytes)]
        if not names:
            continue
        pm = props70_map(m)
        out[names[0]] = {k: ([float(x) for x in pm[k].props[4:7]] if k in pm else None)
                         for k in ("Lcl Translation", "Lcl Rotation", "Lcl Scaling")}
    return out


def global_settings(root):
    gs = props70_map(elem_find_first(root, b'GlobalSettings'))
    keys = ("UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign",
            "CoordAxis", "CoordAxisSign", "UnitScaleFactor")
    return {k: gs[k].props[4] for k in keys if k in gs}


def geometry_stats(root, eps=1e-6):
    """Per-Geometry stats from the stored data: verts, fan tris, slivers
    (|cross| < eps, i.e. area < eps/2 in stored units), bounds, and a
    hash of the sorted triangle vertex set (exact geometry identity)."""
    import hashlib
    import numpy as np
    objs = elem_find_first(root, b'Objects')
    out = []
    for g in elem_find_iter(objs, b'Geometry'):
        v = elem_find_first(g, b'Vertices')
        pvi = elem_find_first(g, b'PolygonVertexIndex')
        if v is None or pvi is None:
            continue
        name = ""
        for p in g.props:
            if isinstance(p, bytes):
                name = p.split(b'\x00\x01')[0].decode(errors="replace")
                break
        verts = np.array(v.props[0], dtype=np.float64).reshape(-1, 3)
        raw = np.array(pvi.props[0], dtype=np.int64)
        polys, cur = [], []
        for r in raw:
            if r < 0:
                cur.append(-r - 1)
                polys.append(cur)
                cur = []
            else:
                cur.append(r)
        tris = []
        for p in polys:
            for i in range(1, len(p) - 1):
                tris.append((p[0], p[i], p[i + 1]))
        tris = np.array(tris)
        a = verts[tris[:, 1]] - verts[tris[:, 0]]
        b = verts[tris[:, 2]] - verts[tris[:, 0]]
        mag = np.sqrt((np.cross(a, b) ** 2).sum(axis=1))
        tv = np.round(verts[tris], 5)
        keys = sorted(tuple(sorted(tuple(vtx) for vtx in tri)) for tri in tv)
        h = hashlib.sha256()
        for k in keys:
            h.update(repr(k).encode())
        out.append({
            "name": name,
            "verts": int(len(verts)),
            "tris": int(len(tris)),
            "slivers": int((mag < eps).sum()),
            "bounds_min": [round(float(x), 4) for x in verts.min(axis=0)],
            "bounds_max": [round(float(x), 4) for x in verts.max(axis=0)],
            "tri_hash": h.hexdigest(),
        })
    return out


def inspect(path, blender_home=None, eps=1e-6):
    parse, _, _ = _imports(blender_home)
    root, _ = parse(path)
    return {
        "globals": global_settings(root),
        "models": list_models(root),
        "geometry": geometry_stats(root, eps=eps),
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Fold FBX root transforms to identity at file level.")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--model", action="append", dest="models",
                    help="Model name to fold (repeatable). Default: all Models.")
    ap.add_argument("--blender-home", default=None)
    ap.add_argument("--roundtrip-only", action="store_true",
                    help="Only run the parse->re-encode value-identity check on src.")
    args = ap.parse_args()
    if args.roundtrip_only:
        n, bad = roundtrip_check(args.src, args.blender_home)
        print(json.dumps({"elements": n, "mismatches": bad}, indent=1))
        sys.exit(0 if not bad else 1)
    print("folded:", fold_models(args.src, args.dst, args.models, args.blender_home))

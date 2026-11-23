"""verify_fbx.py -- file-level FBX inspector for axis-doctor.

Parses an FBX with Blender's own io_scene_fbx parser and prints what is
actually stored in the file: GlobalSettings, every Model's Lcl
Translation/Rotation/Scaling, and per-Geometry stats (verts, fan tris,
sliver count, bounds, triangle-set hash). No Blender scene is involved,
so nothing is reinterpreted by an importer -- this is the same class of
evidence engine devlogs cite ("every FBX root has rotation (0,0,0)").

Usage:
    python3 verify_fbx.py file.fbx [--blender-home PATH] [--sliver-eps 1e-6]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fbx_fold


def _san(o):
    if isinstance(o, bytes):
        return o.decode(errors="replace")
    if hasattr(o, "item"):
        return o.item()
    return str(o)


def main():
    ap = argparse.ArgumentParser(description="Inspect an FBX at file level.")
    ap.add_argument("path")
    ap.add_argument("--blender-home", default=None,
                    help="Blender install root (for io_scene_fbx). "
                         "Default: BLENDER_HOME or auto-detect.")
    ap.add_argument("--sliver-eps", type=float, default=1e-6,
                    help="|cross| threshold for a sliver (default 1e-6, "
                         "i.e. face area < 0.5 mm^2 in metre files).")
    args = ap.parse_args()
    info = fbx_fold.inspect(args.path, blender_home=args.blender_home,
                            eps=args.sliver_eps)
    print(json.dumps(info, indent=1, default=_san))
    return 0


if __name__ == "__main__":
    sys.exit(main())

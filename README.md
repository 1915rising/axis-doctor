# axis-doctor

Bake the axis conversion into your FBX meshes — identity root, Y-up,
facing +Z, slivers removed — so no importer ever has to untangle your
files again.

## The problem

Export an FBX from Blender with default settings and parse the file:
the root Model node carries **Lcl Rotation (−90°, 0, 0)** and **Lcl
Scaling (100, 100, 100)**, with the geometry stored in Blender's frame.
The conversion doesn't live in the mesh — it lives in a transform every
importer must apply, and engines paper over it with import options
(Unity's *Bake Axis Conversion* checkbox exists for exactly this). Tick
the wrong box, or open the file somewhere without one, and your model
arrives rotated, scaled, or both.

Measured on a real project file (a female avatar with Christmas
dress, exported as FBX): the main mesh's root carried Lcl Rotation
(−90°, 0, 0) and a child node carried an armature-parent scale of
0.0198. Roots are not identity; every consumer has to untangle them.

No Blender exporter setting fixes this. We tested four recipes
(defaults; data pre-rotated; exporter set to Blender convention;
explicit `axis_forward`/`axis_up`) — Blender always encodes its
conversion in the root node. The fix has to happen partly *after*
export, at file level.

## The pattern

PSX Cold War Lab 1.0.1 (SemaYT) shipped 132 props in the end state this
tool produces: root rotation (0, 0, 0), scale 1, standing in a Y-up
frame facing +Z, zero-area triangles removed — "no import option to
tick." axis-doctor turns that pattern into a pipeline:

1. **Bake** the object's full evaluated world transform into its mesh
   data (evaluated depsgraph matrix — see Limitations for why).
2. **Delete slivers surgically** — only polygons whose every fan
   triangle is zero-area. Surviving geometry is never touched: no
   remesh, no decimation. (ai-mesh-cleanup can only remove zero-area
   triangles as a side effect of a full voxel remesh, which rebuilds
   everything and strips materials/UVs.)
3. **Rotate** the data into the target frame, (x, y, z) → (x, z, −y),
   and optionally **floor the pivot** so the lowest point sits at y = 0.
4. **Export** with exporter defaults, then **fold** the root transform
   away at file level: zero the root Model's Lcl Rotation/Scaling and
   set UnitScaleFactor to 100 so the stored metre units stay metres.
   That is the same bake the Unity checkbox performs — done once at
   export time instead of at every import, forever.

Preservation is proven, not assumed: the surviving triangle set is
hashed before and after the bake. Where float32 re-storage at large
bake scales jitters coordinates, the report falls back to a sorted
vertex-cloud comparison with a max-deviation figure (5.5e-08 m on the
50×-scale test case below). The fold machinery itself is validated by a
parse → re-encode roundtrip check that must reproduce every element of
a test file value-identically before it is trusted.

## What you get

Measured on our own worst export (a female avatar with Christmas
dress; two meshes extracted one at a time):

| Check | GenGown | Face |
|---|---|---|
| Tris before → after | 34,663 → 34,660 | 1,958 → 1,834 |
| Slivers (area < 0.5 mm²) before → after | 3 → 0 | 124 → 0 |
| Surviving geometry unchanged | max deviation 5.5e-08 m | exact hash match |
| Final root Lcl Rotation / Scaling | (0, 0, 0) / (1, 1, 1) | (0, 0, 0) / (1, 1, 1) |
| Final globals | UpAxis Y, FrontAxis +Z, UnitScaleFactor 100 | same |
| Stored bounds after | base at y = 0.0000, height 1.3251 m | head height preserved |

Full test output: [examples/test-output.txt](examples/test-output.txt)
(23/23 checks, Blender 4.2.3 headless).

## Usage

Main pipeline (runs inside Blender):

```bash
blender --background --python axis_doctor.py -- \
    --input in.fbx --output out.fbx \
    --object GenGown --floor --report report.json
```

| Argument | Default | Meaning |
|---|---|---|
| `--input` | *(required)* | Source FBX |
| `--output` | *(required)* | Destination FBX (identity root, Y-up) |
| `--object` | the only mesh | Which mesh object to extract; required when the file holds several |
| `--floor` | off | Translate the data so its lowest point sits at y = 0 |
| `--sliver-eps` | `1e-6` | \|cross\| threshold for a sliver, world space (1e-6 = face area < 0.5 mm²) |
| `--report` | — | Also write the full JSON report to this path |
| `--keep-raw` | — | Also keep the unfolded exporter output at this path |

File-level inspector (system Python, no Blender scene — nothing is
reinterpreted by an importer):

```bash
python3 verify_fbx.py out.fbx            # roots, globals, geometry stats, tri hash
```

Fold an already-baked file, or validate the fold machinery on its own:

```bash
python3 fbx_fold.py raw.fbx folded.fbx --model GenGown
python3 fbx_fold.py --roundtrip-only any.fbx
```

`verify_fbx.py` / `fbx_fold.py` need Blender's `io_scene_fbx` modules
(they use the parser/encoder only, never bpy). They find a Blender
install via `--blender-home`, `$BLENDER_HOME`, or common install
locations; inside Blender they just work.

## Requirements

- **Blender 4.2+** — developed and tested on **4.2.3 LTS** (bundled
  Python + NumPy; no extra packages for the Blender side).
- The standalone tools need a system Python 3 with NumPy, plus any
  Blender install to borrow `io_scene_fbx` from.

## Limitations — read before you rely on it

- **One static mesh object per run.** The tool extracts a single named
  object into a standalone file. Multi-object scenes are processed one
  object at a time.
- **Armature and animation data are NOT preserved.** Modifiers on the
  object (including an armature modifier) are dropped after the
  evaluated bake; the output is a static mesh. Folding roots of
  animated/armature files (per-node treatment, animation curves) is
  untested and out of scope — the report lists every modifier dropped
  so nothing vanishes silently.
- **The end state is verified at file level, not inside Unity.** No
  Unity runs on the test machine; the claim rests on parsing the
  output files (root transforms, globals, stored geometry) — the same
  measurements the original devlog reports from the Unity side — plus
  clean re-import into Blender at scale exactly 1.
- **Sliver threshold is in world-space metres.** The default
  (area < 0.5 mm²) suits metre-scale character/prop files. Files stored
  in other units need `--sliver-eps` adjusted.
- **Measurement trap, documented so you don't repeat it:** Blender's
  FBX importer leaves `matrix_world` stale in headless sessions
  (parent chains and scales missing; `view_layer.update()` does not
  reliably fix it). Sliver counts measured against the stale matrix
  were wrong in both directions during development. This tool reads
  evaluated depsgraph matrices only.
- **The fold is only valid on pre-baked data.** `fbx_fold.py` removes
  the root conversion without applying it; run it on files whose
  geometry already lives in the target frame (axis-doctor's raw
  exports), never on an ordinary Blender export you still need
  converted.

## Origin

The pattern comes from the PSX Cold War Lab 1.0.1 devlog (SemaYT,
itch.io) — pattern only; no pack assets were bought or used. It is the
Blender-side baker core of bridge B7 in the 1915rising tool collection
(a declarative Unity import spec), sibling to ripdoctor, stl-doctor,
ai-mesh-cleanup, viseme-doctor, orient-doctor, and fit-doctor.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 1915rising.

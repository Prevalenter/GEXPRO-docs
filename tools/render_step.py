#!/usr/bin/env python3
"""Render the STEP files from the GEXPRO download archives to PNG previews.

The documentation site is plain static HTML, so the previews are produced
offline and committed as ordinary PNG files.  Each B-rep solid is tessellated
with the OpenCascade kernel (the ``cadquery-ocp`` wheel) and rasterised with a
small painter's-algorithm renderer built on numpy and Pillow, so no GPU,
OpenGL context or display is needed.

Environment
-----------
    python3.11 -m venv --system-site-packages .venv
    .venv/bin/pip install cadquery-ocp      # provides the OCP module

Usage
-----
    python tools/render_step.py                     # render both archives
    python tools/render_step.py --only gx16         # just the hand
    python tools/render_step.py --parts Palm1       # single part, for tuning
    python tools/render_step.py --html              # also print the HTML fragment

Outputs
-------
    _static/models/<device>/parts/<slug>.png        full-size render (and .thumb.png)
    tools/preview-manifest.json                     geometry facts used on the page

The archives have no assembly transform: every part is modelled about its own
origin, so the previews are deliberately per part.  Overlaying the files would
produce a pile of parts rather than an assembly, and the site already carries a
rendered view of the assembled hand.

The renders are a visual index of the archives only: sizes are bounding boxes
measured on the models themselves, and no material or manufacturing claims are
encoded in the shading.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
OUT_ROOT = REPO / "_static" / "models"
MANIFEST = Path(__file__).resolve().parent / "preview-manifest.json"

FULL_SIZE = (1400, 1050)
THUMB_SIZE = (320, 240)
SSAA = 2                       # supersampling factor for the full render
AZIMUTH = 38.0                 # camera azimuth in degrees (Z-up world)
ELEVATION = 20.0               # camera elevation in degrees
MAX_TRIANGLES = 160_000        # tessellation budget per part
OUTLINE_RADIUS = 3             # silhouette line width in supersampled pixels
OUTLINE_TONE = 0.30            # sRGB tone of that line

# Linear-space albedos.  Structural parts share one tone; the silicone
# fingertip parts use the pink of the existing site render.
GREY = (0.690, 0.712, 0.729)
PINK = (0.855, 0.545, 0.553)

# Camera-space lights: one key light from the upper left, one fill from the
# right, plus the highlight that makes machined surfaces read as metal.
KEY = np.array([-0.40, 0.48, 0.78])
FILL = np.array([0.80, -0.20, 0.56])
HALF = KEY + np.array([0.0, 0.0, 1.0])
for _vector in (KEY, FILL, HALF):
    _vector /= np.linalg.norm(_vector)


@dataclass
class Part:
    """One STEP file: where it lives and how it should be grouped on the page."""

    path: Path
    group: str


@dataclass
class Device:
    key: str
    title: str
    root: Path
    parts: list[Part] = field(default_factory=list)


def collect(device: Device) -> None:
    """List the STEP files of one archive, skipping extracted duplicates."""
    for path in sorted(device.root.rglob("*.STEP")) + sorted(device.root.rglob("*.step")):
        if path.is_file():
            device.parts.append(Part(path=path, group=group_for(device.key, path)))


def group_for(device_key: str, path: Path) -> str:
    """Group a part by its file-name family (the page says so explicitly)."""
    stem = path.stem.lower()
    if device_key == "gx16":
        if stem.startswith("palm"):
            return "Palm and palm joints"
        if stem.startswith("thumb"):
            return "Thumb"
        if "silicone" in str(path).lower() or stem in {"fingertips", "ring_fingertip"}:
            return "Silicone fingertips and moulds"
        if "tactile" in stem:
            return "Tactile-fingertip connectors"
        if stem.startswith("control_box"):
            return "Control-box shells"
        if stem in {"motor_connect", "flange"}:
            return "Motor mount and flange"
        return "Fingers and finger links"
    if stem == "base":
        return "Glove base"
    for module in ("index", "middle", "ring", "thumb"):
        if stem.startswith(module + "_"):
            return f"{module.capitalize()} module"
    return "Glove base"


def slugify(name: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return slug or "part"


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


def read_shape(path: Path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape.IsNull():
        raise RuntimeError(f"empty shape in {path}")
    return shape


def bbox(shape) -> tuple[np.ndarray, np.ndarray]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    lo, hi = box.CornerMin(), box.CornerMax()
    return np.array([lo.X(), lo.Y(), lo.Z()]), np.array([hi.X(), hi.Y(), hi.Z()])


def triangulate(shape, max_triangles: int = MAX_TRIANGLES):
    """Tessellate a shape into (triangles, face normals) arrays in world space."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    lo, hi = bbox(shape)
    diagonal = float(np.linalg.norm(hi - lo))
    deflection = max(diagonal / 900.0, 0.015)

    triangles = None
    for _ in range(6):
        BRepMesh_IncrementalMesh(shape, deflection, False, 0.18, True)
        triangles = extract_triangles(shape, BRep_Tool, TopoDS, TopExp_Explorer, TopLoc_Location,
                                      TopAbs_FACE, TopAbs_REVERSED)
        if len(triangles[0]) <= max_triangles:
            break
        deflection *= 1.8
    return triangles


def extract_triangles(shape, BRep_Tool, TopoDS, TopExp_Explorer, TopLoc_Location,
                      TopAbs_FACE, TopAbs_REVERSED):
    """Return (triangles, vertex normals) with one entry per triangle corner."""
    faces: list[np.ndarray] = []
    face_normals: list[np.ndarray] = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face(explorer.Current())
        location = TopLoc_Location()
        mesh = BRep_Tool.Triangulation_s(face, location)
        if mesh is not None and mesh.NbTriangles() > 0:
            transform = location.Transformation()
            nodes = np.empty((mesh.NbNodes(), 3), dtype=np.float64)
            for i in range(1, mesh.NbNodes() + 1):
                point = mesh.Node(i)
                nodes[i - 1] = (point.X(), point.Y(), point.Z())
            if not location.IsIdentity():
                for i in range(len(nodes)):
                    x, y, z = nodes[i]
                    point = mesh.Node(i + 1).Transformed(transform)
                    nodes[i] = (point.X(), point.Y(), point.Z())
            index = np.empty((mesh.NbTriangles(), 3), dtype=np.int64)
            for i in range(1, mesh.NbTriangles() + 1):
                index[i - 1] = mesh.Triangle(i).Get()
            index -= 1
            if face.Orientation() == TopAbs_REVERSED:
                # OpenCascade winds the triangles in the face's own parametric
                # direction, so reversed faces need their winding flipped to
                # keep every normal pointing out of the solid.
                index = index[:, ::-1]
            corners = nodes[index]
            faces.append(corners)
            face_normals.append(smooth_normals(nodes, index, corners))
        explorer.Next()

    if not faces:
        raise RuntimeError("tessellation produced no triangles")

    triangles = np.concatenate(faces, axis=0)
    normals = np.concatenate(face_normals, axis=0)
    return triangles, normals


def smooth_normals(nodes: np.ndarray, index: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Per-corner normals averaged over each vertex inside one CAD face.

    Smoothing inside a face rounds off tessellated cylinders and fillets, while
    the area-weighted raw normals keep the edges between faces sharp.  The
    result has the same shape as the triangle corners, so the renderer can
    interpolate it per pixel.
    """
    raw = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    accum = np.zeros_like(nodes)
    for corner in range(3):
        np.add.at(accum, index[:, corner], raw)
    lengths = np.linalg.norm(accum, axis=1, keepdims=True)
    accum = accum / np.where(lengths == 0.0, 1.0, lengths)
    return accum[index]


def read_unit(path: Path) -> str:
    """Report the length unit declared in the STEP header."""
    try:
        text = path.open("r", errors="ignore").read(20000)
    except OSError:
        return "unknown"
    if re.search(r"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)", text):
        return "mm"
    if re.search(r"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", text, re.IGNORECASE):
        return "inch"
    if re.search(r"SI_UNIT\s*\(\s*\.\s*,\s*\.METRE\.\s*\)", text):
        return "m"
    return "unknown"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def srgb_to_linear(value: np.ndarray | float):
    return np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(value: np.ndarray):
    value = np.clip(value, 0.0, 1.0)
    return np.where(value <= 0.0031308, value * 12.92, 1.055 * value ** (1 / 2.4) - 0.055)


def camera_basis(azimuth: float, elevation: float):
    az, el = math.radians(azimuth), math.radians(elevation)
    forward = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])
    right = np.cross(np.array([0.0, 0.0, 1.0]), forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    return forward, right, up


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Grow a coverage mask by ``radius`` pixels (4-connected)."""
    grown = mask.copy()
    for _ in range(radius):
        previous = grown.copy()
        grown[1:, :] |= previous[:-1, :]
        grown[:-1, :] |= previous[1:, :]
        grown[:, 1:] |= previous[:, :-1]
        grown[:, :-1] |= previous[:, 1:]
    return grown


def shade_pixels(normals: np.ndarray, albedo_linear: np.ndarray, basis) -> np.ndarray:
    """Shade world-space normals in camera space; returns floats in 0..1."""
    forward, right, up = basis
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    unit = normals / np.where(lengths == 0.0, 1.0, lengths)
    camera = np.stack([unit @ right, unit @ up, unit @ forward], axis=1)

    intensity = (
        0.50
        + 0.58 * np.clip(camera @ KEY, 0.0, None)
        + 0.22 * np.clip(camera @ FILL, 0.0, None)
    )
    specular = 0.22 * np.clip(camera @ HALF, 0.0, None) ** 60
    linear = albedo_linear[None, :] * intensity[:, None] + specular[:, None]
    return linear_to_srgb(linear)


def projected_spans(triangles: np.ndarray, basis) -> tuple[float, float]:
    """Width and height of the silhouette in the given camera frame."""
    forward, right, up = basis
    flat = triangles.reshape(-1, 3)
    x = flat @ right
    y = flat @ up
    return float(x.max() - x.min()), float(y.max() - y.min())


def choose_basis(triangles: np.ndarray, basis):
    """Keep the default view unless it would show a part almost edge-on."""
    width, height = projected_spans(triangles, basis)
    if min(width, height) / max(width, height) >= 0.30:
        return basis
    best = basis
    best_area = width * height
    for azimuth in range(0, 360, 45):
        for elevation in (-20.0, 20.0, 55.0):
            candidate = camera_basis(azimuth, elevation)
            span_x, span_y = projected_spans(triangles, candidate)
            if span_x * span_y > best_area * 1.05:
                best_area = span_x * span_y
                best = candidate
    return best


def render(triangles: np.ndarray, vertex_normals: np.ndarray, albedo, size=FULL_SIZE,
           supersample: int = SSAA, fill: float = 0.90, basis=None) -> Image.Image:
    """Z-buffered orthographic render on a transparent background.

    Depth is tested per pixel rather than by sorting whole triangles, so
    overlapping and interpenetrating faces cannot bleed through one another,
    and the corner normals are interpolated per pixel so that tessellated
    cylinders and fillets shade smoothly instead of showing facets.
    """
    width, height = size
    s = int(supersample)
    canvas_w, canvas_h = width * s, height * s
    basis = basis if basis is not None else camera_basis(AZIMUTH, ELEVATION)
    forward, right, up = basis

    flat = triangles.reshape(-1, 3)
    x = (flat @ right).reshape(-1, 3)
    y = (flat @ up).reshape(-1, 3)
    z = (flat @ forward).reshape(-1, 3)

    span_x = max(x.max() - x.min(), 1e-6)
    span_y = max(y.max() - y.min(), 1e-6)
    scale = min(canvas_w * fill / span_x, canvas_h * fill / span_y)
    center_x = (x.min() + x.max()) / 2
    center_y = (y.min() + y.max()) / 2

    screen_x = canvas_w / 2 + (x - center_x) * scale
    screen_y = canvas_h / 2 - (y - center_y) * scale
    screen_z = z * scale

    depth = np.full((canvas_h, canvas_w), -np.inf, dtype=np.float64)
    color = np.zeros((canvas_h, canvas_w, 3), dtype=np.float32)
    albedo_linear = srgb_to_linear(np.array(albedo, dtype=np.float64))

    # Closed solids only ever show their outward-facing triangles, so the
    # inward half of the mesh can be dropped before rasterising.
    geometric = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    order = np.flatnonzero(geometric @ forward > 0.0)

    for index in order:
        triangle_x = screen_x[index]
        triangle_y = screen_y[index]
        left = max(int(math.floor(triangle_x.min())), 0)
        right_px = min(int(math.ceil(triangle_x.max())), canvas_w - 1)
        top = max(int(math.floor(triangle_y.min())), 0)
        bottom = min(int(math.ceil(triangle_y.max())), canvas_h - 1)
        if left > right_px or top > bottom:
            continue

        ax, ay, az = triangle_x[0], triangle_y[0], screen_z[index][0]
        bx, by, bz = triangle_x[1], triangle_y[1], screen_z[index][1]
        cx, cy, cz = triangle_x[2], triangle_y[2], screen_z[index][2]
        area2 = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(area2) < 0.5:            # sub-pixel triangles cannot be seen
            continue

        columns = np.arange(left, right_px + 1, dtype=np.float64) + 0.5
        rows = np.arange(top, bottom + 1, dtype=np.float64)[:, None] + 0.5
        weight_a = ((by - cy) * (columns - cx) + (cx - bx) * (rows - cy)) / area2
        weight_b = ((cy - ay) * (columns - cx) + (ax - cx) * (rows - cy)) / area2
        weight_c = 1.0 - weight_a - weight_b
        inside = (weight_a >= -1e-9) & (weight_b >= -1e-9) & (weight_c >= -1e-9)
        if not inside.any():
            continue

        pixel_z = weight_a * az + weight_b * bz + weight_c * cz
        window = depth[top:bottom + 1, left:right_px + 1]
        visible = inside & (pixel_z > window)
        if not visible.any():
            continue
        window[visible] = pixel_z[visible]

        wa = weight_a[visible][:, None]
        wb = weight_b[visible][:, None]
        wc = weight_c[visible][:, None]
        interpolated = (
            wa * vertex_normals[index, 0]
            + wb * vertex_normals[index, 1]
            + wc * vertex_normals[index, 2]
        )
        color[top:bottom + 1, left:right_px + 1][visible] = shade_pixels(
            interpolated, albedo_linear, basis
        )

    covered = np.isfinite(depth)
    if OUTLINE_RADIUS > 0:
        outline = dilate(covered, OUTLINE_RADIUS) & ~covered
        color[outline] = OUTLINE_TONE
        covered = covered | outline
    rgba = np.dstack([
        np.clip(color * 255.0, 0, 255).astype(np.uint8),
        np.where(covered, 255, 0).astype(np.uint8),
    ])
    image = Image.fromarray(rgba, "RGBA")
    if s > 1:
        image = image.resize((width, height), Image.LANCZOS)
    return image


def has_silicone_tone(stem: str) -> bool:
    return "fingertip" in stem.lower() and "mold" not in stem.lower()


def save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    thumb = image.copy()
    thumb.thumbnail(THUMB_SIZE, Image.LANCZOS)
    thumb.save(path.with_suffix(".thumb.png"), optimize=True)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def build_devices() -> list[Device]:
    gx16 = Device("gx16", "GX16 dexterous hand", REPO / "hardware" / "GX16 STEP file")
    ex16 = Device("ex16", "EX16 exoskeleton glove", REPO / "hardware" / "EX16 STEP file")
    devices = [gx16, ex16]
    for device in devices:
        collect(device)
    return devices


def render_one(path: Path, albedo, out: Path, facts_out: dict, rel: str) -> dict:
    shape = read_shape(path)
    lo, hi = bbox(shape)
    size = np.round(hi - lo, 1)
    triangles, normals = triangulate(shape)
    basis = choose_basis(triangles, camera_basis(AZIMUTH, ELEVATION))
    image = render(triangles, normals, albedo, basis=basis)
    save(image, out)
    facts = {
        "file": path.name,
        "folder": path.parent.name,
        "rel": rel,
        "size_mm": [float(v) for v in size],
        "unit": read_unit(path),
        "triangles": int(len(triangles)),
    }
    facts_out[rel] = facts
    print(f"  {path.name:<34} {size[0]:6.1f} x {size[1]:6.1f} x {size[2]:6.1f} mm"
          f"  {len(triangles):>7} tris  -> {out.relative_to(REPO)}")
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=["gx16", "ex16"], help="render one archive only")
    parser.add_argument("--parts", nargs="*", help="only render parts whose name contains one of these")
    parser.add_argument("--html", action="store_true", help="print the gallery HTML fragment")
    parser.add_argument("--html-only", action="store_true",
                        help="print the gallery fragment from the existing manifest")
    args = parser.parse_args()

    devices = build_devices()
    if args.html_only:
        manifest = json.loads(MANIFEST.read_text())
        print_html(manifest, devices)
        return 0

    selected = [d for d in devices if not args.only or d.key == args.only]
    manifest: dict[str, dict] = {}

    for device in selected:
        parts = [p for p in device.parts
                 if not args.parts or any(a.lower() in p.path.stem.lower() for a in args.parts)]
        print(f"{device.title}: {len(parts)} STEP files")
        for part in parts:
            rel = f"{device.key}/parts/{slugify(part.path.stem)}.png"
            render_one(part.path, PINK if has_silicone_tone(part.path.stem) else GREY,
                       OUT_ROOT / rel, manifest, rel)

    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {MANIFEST.relative_to(REPO)} ({len(manifest)} entries)")
    if args.html:
        print_html(manifest, devices)
    return 0


def print_html(manifest: dict[str, dict], devices: list[Device]) -> None:
    """Emit the figure markup for mechanical-design.html."""
    order = {
        "gx16": ["Palm and palm joints", "Thumb", "Fingers and finger links",
                 "Motor mount and flange", "Control-box shells",
                 "Silicone fingertips and moulds", "Tactile-fingertip connectors"],
        "ex16": ["Glove base", "Index module", "Middle module", "Ring module", "Thumb module"],
    }

    def figure(key: str, caption: str | None = None) -> str:
        facts = manifest[key]
        name = caption or facts["file"]
        size = " × ".join(f"{v:g}" for v in facts["size_mm"])
        return (
            f'<figure class="part"><a href="../_static/models/{facts["rel"]}">'
            f'<img src="../_static/models/{facts["rel"][:-4]}.thumb.png" '
            f'alt="{name} rendered from the STEP file" loading="lazy"></a>'
            f'<figcaption><span class="part-name">{name}</span>'
            f'<span class="part-size">{size} mm</span></figcaption></figure>'
        )

    for device in devices:
        groups: dict[str, list[Part]] = {}
        for part in device.parts:
            groups.setdefault(part.group, []).append(part)
        preferred = order.get(device.key, [])
        ranking = sorted(groups, key=lambda g: preferred.index(g) if g in preferred else len(preferred))

        print(f"\n<!-- {device.title} -->")
        print(f'<h3>{device.title}</h3>')
        for group in ranking:
            print(f'<h4>{group}</h4>')
            print('<div class="part-grid">')
            for part in groups[group]:
                key = f"{device.key}/parts/{slugify(part.path.stem)}.png"
                if key in manifest:
                    print(figure(key, part.path.name))
            print("</div>")


if __name__ == "__main__":
    sys.exit(main())

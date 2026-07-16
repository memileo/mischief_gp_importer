from __future__ import annotations

import json
import os
import subprocess
import tempfile
import gzip
# import numpy as np
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import bpy
from bpy_extras.io_utils import ImportHelper
from bpy.props import BoolProperty, FloatProperty, StringProperty, IntProperty
from bpy.types import Operator

from .materials import create_gp_material, ensure_material_slots

MIN_SUPPORTED = (4, 3, 0)
ACTIVE_JOB = None


# ---------------------------------------------------------------------------
# Data specs
# ---------------------------------------------------------------------------


@dataclass
class PointSpec:
    x: float
    y: float
    radius: float
    opacity: float


@dataclass
class StrokeSpec:
    color: tuple[float, float, float, float]
    is_eraser: bool
    hardness: float
    rnote_textured: bool
    points: list[PointSpec]


@dataclass
class LayerSpec:
    name: str
    opacity: float
    visible: bool
    strokes: list[StrokeSpec]


@dataclass
class ImportJob:
    context: Any
    filepath: str
    data: dict[str, Any]
    object: bpy.types.Object
    gp_data: bpy.types.GreasePencil
    layer_specs: list[LayerSpec] = field(default_factory=list)
    canvas_width: float = 0.0
    canvas_height: float = 0.0
    frame_number: int = 0
    total_strokes: int = 0
    processed_strokes: int = 0
    current_layer_index: int = 0
    cancel_requested: bool = False
    progress_text: str = ""
    timer: Any = None
    catmull_rom: bool = False


def _srgb_to_linear(c: float) -> float:
    """Convert a single sRGB channel value (0-1) to linear space."""
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4

# ---------------------------------------------------------------------------
# Validation / parsing
# ---------------------------------------------------------------------------


def _require_supported_version() -> None:
    if bpy.app.version < MIN_SUPPORTED:
        v = bpy.app.version
        raise RuntimeError(
            f"Blender {v[0]}.{v[1]}.{v[2]} is not supported. "
            f"This importer requires Blender {MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]}.{MIN_SUPPORTED[2]}+ "
            f"(Grease Pencil v3)."
        )


def _parse_point(raw: dict[str, Any]) -> PointSpec:
    return PointSpec(
        x=float(raw["x"]),
        y=float(raw["y"]),
        radius=float(raw.get("radius", 0.0)),
        opacity=float(raw.get("opacity", 1.0)),
    )


def _parse_stroke(raw: dict[str, Any]) -> StrokeSpec:
    color = raw.get("color", [0.0, 0.0, 0.0, 1.0])
    if len(color) != 4:
        raise ValueError("Stroke color must be RGBA with 4 components")
    points = [_parse_point(p) for p in raw.get("points", [])]
    # Duplicate single point strokes to prevent dissappearing when drawing in GP
    if len(points) == 1:
        points.append(points[0]) 
    return StrokeSpec(
        color=(float(color[0]), float(color[1]), float(color[2]), float(color[3])),
        is_eraser=bool(raw.get("is_eraser", False)),
        hardness=float(raw.get("hardness", 1.0)),
        rnote_textured=bool(raw.get("rnote_textured", False)),
        points=points,
    )


def _parse_layer(raw: dict[str, Any]) -> LayerSpec:
    return LayerSpec(
        name=str(raw.get("name", "Layer")),
        opacity=float(raw.get("opacity", 1.0)),
        visible=bool(raw.get("visible", True)),
        strokes=[_parse_stroke(s) for s in raw.get("strokes", [])],
    )


def load_json_file(filepath: str) -> dict[str, Any]:
    with open(filepath, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if not isinstance(data, dict):
        raise ValueError("Top-level JSON must be an object")
    if int(data.get("version", 0)) != 1:
        raise ValueError("Unsupported JSON version; expected version 1")
    if "canvas" not in data or "layers" not in data:
        raise ValueError("JSON is missing required canvas or layers fields")

    return data


# ---------------------------------------------------------------------------
# art2png conversion
# ---------------------------------------------------------------------------


def _run_art2png(
    art_path: str,
    output_dir: str,
    radius_scale: float,
    resample_step: float,
    art2png_path: str,
    catmull_rom: bool = False,
) -> str:
    """Run the external art2png tool and return the path to the generated JSON."""
    stem = Path(art_path).stem
    output_json = os.path.join(output_dir, f"{stem}.json")

    cmd = [
        art2png_path,
        "--export-gp", output_json,
        "--gp-radius-scale", str(radius_scale),
        "--resample-step", str(resample_step),
    ]

    if catmull_rom:
        cmd.append("--catmull-rom")

    cmd.append(art_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise RuntimeError(f"art2png executable not found: {art2png_path}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("art2png conversion timed out")

    if result.returncode != 0:
        raise RuntimeError(
            f"art2png failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    if not os.path.isfile(output_json):
        raise RuntimeError(f"art2png did not produce expected output: {output_json}")

    return output_json


# ---------------------------------------------------------------------------
# Grease Pencil object helpers
# ---------------------------------------------------------------------------


def _safe_link_object_to_scene(
    context: bpy.types.Context, obj: bpy.types.Object
) -> None:
    """Link *obj* into the scene collection if it is not already there."""
    collection = (
        context.collection
        if getattr(context, "collection", None)
        else context.scene.collection
    )
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    obj.location = context.scene.cursor.location.copy()


def _create_gp_object(
    context: bpy.types.Context, name: str
) -> tuple[bpy.types.GreasePencil, bpy.types.Object]:
    if context.mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass

    try:
        bpy.ops.object.select_all(action='DESELECT')
    except Exception:
        pass

    bpy.ops.object.grease_pencil_add()
    obj = context.object or context.view_layer.objects.active
    if obj is None:
        raise RuntimeError("Failed to create Grease Pencil object")

    gp_data = obj.data
    if gp_data is None:
        raise RuntimeError("Created Grease Pencil object has no data-block")

    # Remove the default layer so imported data doesn't get a stray empty one
    while len(gp_data.layers) > 0:
        gp_data.layers.remove(gp_data.layers[0])

    obj.name = name
    try:
        gp_data.name = name
    except Exception:
        pass

    _safe_link_object_to_scene(context, obj)
    try:
        obj.select_set(True)
    except Exception:
        pass
    context.view_layer.objects.active = obj
    return gp_data, obj


def _new_layer(
    gp_data: bpy.types.GreasePencil, name: str
) -> bpy.types.GreasePencilLayer:
    layer = gp_data.layers.new(name, set_active=True)
    return layer


def _new_frame(layer: bpy.types.GreasePencilLayer, frame_number: int):
    try:
        return layer.frames.new(frame_number=frame_number)
    except TypeError:
        return layer.frames.new(frame_number)

def _get_prefs(context):
    """Return the addon preferences (where art2png_path is stored)."""
    from . import MISCHIEFIMPORTER_ADDON_PACKAGE
    return context.preferences.addons[MISCHIEFIMPORTER_ADDON_PACKAGE].preferences

# ---------------------------------------------------------------------------
# Attribute helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-point property setters (fall back to attribute API when the
# point-level property doesn't exist, e.g. GPv3)
# ---------------------------------------------------------------------------

def _set_point_position(point, xyz) -> None:
    if hasattr(point, "position"):
        point.position = xyz
    else:
        point.co = xyz


def _set_point_radius(point, value: float) -> None:
    for attr in ("radius", "pressure"):
        if hasattr(point, attr):
            try:
                setattr(point, attr, value)
                return
            except Exception:
                continue


def _set_point_opacity(point, value: float) -> None:
    for attr in ("opacity", "strength"):
        if hasattr(point, attr):
            try:
                setattr(point, attr, value)
                return
            except Exception:
                continue


def _set_stroke_softness_or_hardness(stroke, hardness: float) -> None:
    softness = max(0.0, min(1.0, 1.0 - float(hardness)))
    if hasattr(stroke, "softness"):
        stroke.softness = softness
    elif hasattr(stroke, "hardness"):
        stroke.hardness = float(hardness)


# ---------------------------------------------------------------------------
# Coordinate conversion  (FIX: XZ front-view, centred origin)
# ---------------------------------------------------------------------------


def _pixel_to_local(
    x_px: float,
    y_px: float,
    canvas_width: float,
    canvas_height: float,
    scale: float,
) -> tuple[float, float, float]:
    """Convert pixel coords to 3-D local coords on the XZ (front-view)
    plane with the origin at the centre of the canvas.

    Y in pixel space grows downward; Z in Blender grows upward, so the
    vertical component is flipped.
    """
    x = (x_px - canvas_width * 0.5) * scale
    y = 0.0
    z = (canvas_height * 0.5 - y_px) * scale
    return (x, y, z)


def _sanitize_object_name(filepath: str) -> str:
    stem = Path(filepath).stem.strip()
    return stem or "MischiefImport"


# ---------------------------------------------------------------------------
# Material creation  (4 materials: Color, Eraser, Pencil Color, Pencil Eraser)
# ---------------------------------------------------------------------------


def _create_materials_for_object(obj: bpy.types.Object):
    color_mat = create_gp_material(
        "Color", color=(1.0, 0.0, 1.0, 1.0), holdout=False, is_pencil=False, rnote_textured=False
    )
    eraser_mat = create_gp_material(
        "Eraser", color=(0.0, 0.0, 0.0, 1.0), holdout=True, is_pencil=False, rnote_textured=False
    )
    pencil_color_mat = create_gp_material(
        "Pencil Color", color=(1.0, 0.0, 1.0, 1.0), holdout=False, is_pencil=True, rnote_textured=False
    )
    pencil_eraser_mat = create_gp_material(
        "Pencil Eraser", color=(0.0, 0.0, 0.0, 1.0), holdout=True, is_pencil=True, rnote_textured=False
    )
    rnote_textured_mat = create_gp_material(
        "Rnote Textured", color=(1.0, 0.0, 1.0, 1.0), holdout=False, is_pencil=False, rnote_textured=True
    )
    ensure_material_slots(obj, color_mat, eraser_mat, pencil_color_mat, pencil_eraser_mat, rnote_textured_mat)
    return color_mat, eraser_mat, pencil_color_mat, pencil_eraser_mat, rnote_textured_mat


# ---------------------------------------------------------------------------
# Drawing population  (single batch per drawing)
# ---------------------------------------------------------------------------


def _populate_drawing(
    drawing,
    layer_spec: LayerSpec,
    canvas_width: float,
    canvas_height: float,
    radius_scale: float,
    catmull_rom: bool = False,
) -> None:
    """Add all strokes and their point data to a drawing in one batch.

    Uses ``foreach_set`` on the built-in drawing attributes (position,
    radius) and a vertex-colour attribute so that per-point radius and
    colour variation are correctly rendered in Grease Pencil 3.

    When *catmull_rom* is True the ``set_types`` method is called on
    every stroke so the viewport/Cycles renderer interpolates a smooth
    curve through the (fewer) control points instead of treating them
    as a polyline.
    """
    stroke_specs = layer_spec.strokes
    if not stroke_specs:
        return

    # Allocate all strokes at once
    sizes = [len(s.points) for s in stroke_specs]
    drawing.add_strokes(sizes)

    # ---- Per-stroke metadata (material index, softness) ----
    for stroke_idx, stroke_spec in enumerate(stroke_specs):
        stroke = drawing.strokes[stroke_idx]

        #   0 = Color          (hardness == 1, not eraser)
        #   1 = Eraser         (hardness == 1, eraser)
        #   2 = Pencil Color   (hardness <  1, not eraser)
        #   3 = Pencil Eraser  (hardness <  1, eraser)
        #   4 = Rnote Textured
        if hasattr(stroke, "material_index"):
            if stroke_spec.rnote_textured:
                stroke.material_index = 4
            elif stroke_spec.is_eraser:
                stroke.material_index = 3 if stroke_spec.hardness < 1.0 else 1
            else:
                stroke.material_index = 2 if stroke_spec.hardness < 1.0 else 0

        _set_stroke_softness_or_hardness(stroke, stroke_spec.hardness)

    # ---- Set curve type to CATMULL_ROM when requested ----
    if catmull_rom:
        try:
            drawing.set_types(
                type='CATMULL_ROM',
                indices=tuple(range(len(stroke_specs)))
            )
        except Exception:
            pass

    # ---- Collect per-point data into flat arrays ----
    all_positions: list[float] = []
    all_radii:   list[float] = []
    all_opacities: list[float] = []
    all_colors:  list[float] = []

    for stroke_spec in stroke_specs:
        rgba = stroke_spec.color
        for pt_spec in stroke_spec.points:
            x, y, z = _pixel_to_local(
                pt_spec.x, pt_spec.y,
                canvas_width, canvas_height,
                radius_scale,
            )
            all_positions.extend((x, y, z))
            all_radii.append(pt_spec.radius * radius_scale)
            all_opacities.append(pt_spec.opacity)
            # Convert RGB from sRGB to linear; alpha stays linear
            all_colors.extend((
                _srgb_to_linear(rgba[0]),
                _srgb_to_linear(rgba[1]),
                _srgb_to_linear(rgba[2]),
                1.0 # vertex_color alpha should be 1 or it will mix with material color
            ))

    # ---- Set built-in "position" attribute (FLOAT_VECTOR, POINT) ----
    pos_attr = drawing.attributes.get("position")
    if pos_attr is not None:
        try:
            pos_attr.data.foreach_set("vector", all_positions)
        except Exception:
            pass

    # ---- Set built-in "radius" attribute (FLOAT, POINT) ----
    radius_attr = drawing.attributes.get("radius")
    if radius_attr is None:
        try:
            radius_attr = drawing.attributes.new(
                name="radius", type='FLOAT', domain='POINT'
            )
        except TypeError:
            radius_attr = drawing.attributes.new("radius", 'FLOAT', 'POINT')
    if radius_attr is not None:
        try:
            radius_attr.data.foreach_set("value", all_radii)
        except Exception:
            pass

    # ---- Set built-in "opacity" attribute (FLOAT, POINT) if it exists ----
    opacity_attr = drawing.attributes.get("opacity")
    if opacity_attr is None:
        try:
            opacity_attr = drawing.attributes.new(
                name="opacity", type='FLOAT', domain='POINT'
            )
        except TypeError:
            opacity_attr = drawing.attributes.new("opacity", 'FLOAT', 'POINT')
    if opacity_attr is not None:
        try:
            opacity_attr.data.foreach_set("value", all_opacities)
        except Exception:
            pass

    # ---- Vertex colour attribute (FLOAT_COLOR, POINT) ----
    color_attr = drawing.attributes.get("vertex_color")
    if color_attr is None:
        try:
            color_attr = drawing.attributes.new(
                name="vertex_color", type='FLOAT_COLOR', domain='POINT'
            )
        except TypeError:
            color_attr = drawing.attributes.new("vertex_color", 'FLOAT_COLOR', 'POINT')

    if color_attr is not None:
        # Data must be a flat list of RGBA: [r, g, b, a, r, g, b, a...]
        color_attr.data.foreach_set("color", all_colors)



# ---------------------------------------------------------------------------
# Layer / job processing
# ---------------------------------------------------------------------------


def _process_layer(
    gp_data: bpy.types.GreasePencil,
    frame_number: int,
    layer_spec: LayerSpec,
    canvas_width: float,
    canvas_height: float,
    radius_scale: float,
    catmull_rom: bool = False,
    is_bottom_layer: bool = False,
) -> int:
    """Create a GP layer, its frame, and populate the drawing."""
    layer = _new_layer(gp_data, layer_spec.name)

    # Adjust opacity for fully opaque layers that contain eraser strokes,
    # unless this is the only layer or the bottom-most layer in the stack.
    if (
       layer_spec.opacity == 1.0
       and any(s.is_eraser for s in layer_spec.strokes)
       and not is_bottom_layer
    ):
       layer_spec = LayerSpec(
           name=layer_spec.name,
           opacity=0.99999,
           visible=layer_spec.visible,
           strokes=layer_spec.strokes,
       )

    if hasattr(layer, "opacity"):
        layer.opacity = float(layer_spec.opacity)
    if hasattr(layer, "hide"):
        layer.hide = not bool(layer_spec.visible)

    frame = _new_frame(layer, frame_number)
    drawing = frame.drawing

    _populate_drawing(
        drawing, layer_spec,
        canvas_width, canvas_height, radius_scale,
        catmull_rom=catmull_rom,
    )
    return len(layer_spec.strokes)


def build_job(
    context: bpy.types.Context, filepath: str, data: dict[str, Any],
    catmull_rom: bool = False,
) -> ImportJob:
    _require_supported_version()

    stem = _sanitize_object_name(filepath)
    gp_data, obj = _create_gp_object(context, stem)
    _create_materials_for_object(obj)

    canvas = data.get("canvas", {})
    canvas_width = float(canvas.get("width", 0.0))
    canvas_height = float(canvas.get("height", 0.0))

    layer_specs = []
    total_strokes = 0
    for raw_layer in data.get("layers", []):
        spec = _parse_layer(raw_layer)
        layer_specs.append(spec)
        total_strokes += len(spec.strokes)

    job = ImportJob(
        context=context,
        filepath=filepath,
        data=data,
        object=obj,
        gp_data=gp_data,
        layer_specs=layer_specs,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
        frame_number=context.scene.frame_current,
        total_strokes=total_strokes,
        catmull_rom=catmull_rom,
    )
    job.progress_text = f"Importing 0/{max(total_strokes, 1)} strokes"
    return job


# ---------------------------------------------------------------------------
# Import execution
# ---------------------------------------------------------------------------


def _cleanup_job(
    job: Optional[ImportJob], delete_partial_object: bool = True
) -> None:
    global ACTIVE_JOB
    if job is None:
        ACTIVE_JOB = None
        return

    try:
        if job.timer is not None:
            try:
                job.context.window_manager.event_timer_remove(job.timer)
            except Exception:
                pass
    except Exception:
        pass

    try:
        job.context.workspace.status_text_set(None)
    except Exception:
        pass

    if delete_partial_object and job.object is not None:
        obj = job.object
        gp_data = job.gp_data
        try:
            bpy.data.objects.remove(obj, do_unlink=True)
        except Exception:
            pass
        try:
            if gp_data.users == 0:
                bpy.data.grease_pencils.remove(gp_data)
        except Exception:
            pass

    ACTIVE_JOB = None


def _finalize_job(job: ImportJob, cancelled: bool = False) -> None:
    global ACTIVE_JOB
    try:
        job.context.workspace.status_text_set(None)
    except Exception:
        pass
    try:
        job.context.window_manager.progress_end()
    except Exception:
        pass

    if cancelled:
        _cleanup_job(job, delete_partial_object=True)
    else:
        ACTIVE_JOB = None


def process_job_batch(job: ImportJob, radius_scale: float) -> bool:
    """Process one complete layer per call. Returns True when finished."""
    if job.current_layer_index >= len(job.layer_specs):
        return True

    if job.cancel_requested:
        raise KeyboardInterrupt

    layer_spec = job.layer_specs[job.current_layer_index]

    # Determine if the current layer is the bottom-most layer in the stack.
    # Layers are rendered top-to-bottom, so the last index is the bottom-most layer.
    is_bottom_layer = False
    if len(job.layer_specs) > 1:
        is_bottom_layer = (job.current_layer_index == 0)

    stroke_count = _process_layer(
        job.gp_data,
        job.frame_number,
        layer_spec,
        job.canvas_width,
        job.canvas_height,
        radius_scale,
        catmull_rom=job.catmull_rom,
        is_bottom_layer=is_bottom_layer,
    )

    job.processed_strokes += stroke_count
    job.current_layer_index += 1
    job.progress_text = (
        f"Imported layer {job.current_layer_index}/{len(job.layer_specs)} "
        f"({job.processed_strokes}/{max(job.total_strokes, 1)} strokes)"
    )

    return job.current_layer_index >= len(job.layer_specs)


# ---------------------------------------------------------------------------
# Public operators
# ---------------------------------------------------------------------------


class MISCHIEF_OT_import_json(Operator, ImportHelper):
    bl_idname = "mischief_gp.import_json"
    bl_label = "Import Mischief File"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Select .art file or .json file to import"

    filename_ext = ".json"
    filter_glob: StringProperty(
        default="*.json;*.art", options={'HIDDEN'}
    )
    filepath: StringProperty(subtype='FILE_PATH')

    _job: Optional[ImportJob] = None

    def invoke(self, context, event):
        if self.properties.is_property_set("filepath"):
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global ACTIVE_JOB
        _require_supported_version()

        filepath = self.filepath
        ext = os.path.splitext(filepath)[1].lower()

        # Read the catmull-rom setting once so both art2png and the
        # import job use the same value.
        catmull_rom = bool(context.scene.mischief_catmull_rom)

        # --- .art file: convert via art2png first ----
        if ext == '.art':
            prefs = _get_prefs(context)
            art2png_path = prefs.art2png_path
            if not art2png_path or not os.path.isfile(art2png_path):
                self.report(
                    {'ERROR'},
                    "art2png path is not configured or invalid. "
                    "Set it in Edit → Preferences → Add-ons → Mischief.",
                )
                return {'CANCELLED'}
            try:
                json_path = _run_art2png(
                    art_path=filepath,
                    output_dir=tempfile.gettempdir(),
                    radius_scale=context.scene.mischief_radius_scale,
                    resample_step=context.scene.mischief_resample_step,
                    art2png_path=art2png_path,
                    catmull_rom=catmull_rom,
                )
            except Exception as exc:
                self.report({'ERROR'}, f"art2png conversion failed: {exc}")
                return {'CANCELLED'}
        elif ext == '.json':
            json_path = filepath
        else:
            self.report({'ERROR'}, f"Unsupported file format: {ext}")
            return {'CANCELLED'}

        # --- Load JSON and build import job ----------------------------
        data = load_json_file(json_path)

        # If the JSON was exported with --catmull-rom by art2png, honour
        # that flag so that control-point strokes are always imported as
        # CATMULL_ROM even if the scene property was toggled mid-flow.
        effective_catmull_rom = data.get("catmull_rom", catmull_rom)

        job = build_job(context, json_path, data, catmull_rom=effective_catmull_rom)
        self._job = job
        ACTIVE_JOB = job

        wm = context.window_manager
        job.timer = wm.event_timer_add(0.01, window=context.window)
        wm.progress_begin(0, max(job.total_strokes, 1))
        try:
            context.workspace.status_text_set(job.progress_text)
        except Exception:
            pass

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        job = self._job
        if job is None:
            return {'CANCELLED'}

        if event.type in {'ESC'}:
            job.cancel_requested = True

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        try:
            finished = process_job_batch(
                job, context.scene.mischief_radius_scale
            )
        except KeyboardInterrupt:
            _finalize_job(job, cancelled=True)
            self.report({'INFO'}, "Import cancelled")
            self._job = None
            return {'CANCELLED'}
        except Exception as exc:
            _cleanup_job(job, delete_partial_object=True)
            self.report({'ERROR'}, f"Import failed: {exc}")
            self._job = None
            return {'CANCELLED'}

        try:
            context.workspace.status_text_set(job.progress_text)
        except Exception:
            pass
        context.window_manager.progress_update(
            min(job.processed_strokes, max(job.total_strokes, 1))
        )

        if finished:
            _finalize_job(job, cancelled=False)
            self.report({'INFO'}, f"Imported {job.total_strokes} strokes")
            self._job = None
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        job = self._job
        if job is not None:
            job.cancel_requested = True
            _cleanup_job(job, delete_partial_object=True)
            self._job = None


class RNOTE_OT_import_json(Operator, ImportHelper):
    bl_idname = "rnote_gp.import_json"
    bl_label = "Import Rnote File"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Select .rnote file or .json file to import"

    filename_ext = ".json"
    filter_glob: StringProperty(
        default="*.json;*.rnote", options={'HIDDEN'}
    )
    filepath: StringProperty(subtype='FILE_PATH')

    _job: Optional['ImportJob'] = None

    def invoke(self, context, event):
        if self.properties.is_property_set("filepath"):
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global ACTIVE_JOB
        _require_supported_version()

        filepath = self.filepath
        ext = os.path.splitext(filepath)[1].lower()

        # Read the catmull-rom setting, reuse existing mischief logic
        catmull_rom = bool(context.scene.mischief_catmull_rom)

        data = None
        json_path = filepath # Keep path for reference/debugging if needed

        # --- Parse Rnote File (Gzipped JSON or Plain JSON) ---
        try:
            if ext == '.rnote':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    rnote_json = json.load(f)
            elif ext == '.json':
                with open(filepath, 'r', encoding='utf-8') as f:
                    rnote_json = json.load(f)
            else:
                self.report({'ERROR'}, f"Unsupported file format: {ext}")
                return {'CANCELLED'}

            # --- Convert Rnote JSON to GPExportData Structure ---

            engine_snapshot = rnote_json.get('data', {}).get('engine_snapshot', {})
            stroke_components = engine_snapshot.get('stroke_components', [])
            chrono_components = engine_snapshot.get('chrono_components', [])

            # Canvas Data
            doc_config = engine_snapshot.get('document', {}).get('config', {})
            format_info = doc_config.get('format', {})

            canvas_width = int(format_info.get('width', 1920))
            canvas_height = int(format_info.get('height', 1080))

            # Layers Dictionary to aggregate strokes
            layers_dict = {}

            # Iterate through chrono components to determine order and layer assignment
            for i, chrono in enumerate(chrono_components):
                val = chrono.get('value')
                if not val:
                    continue

                # 1. Use the loop index 'i' to access stroke_components
                # This handles the compacted array structure
                if i >= len(stroke_components):
                    continue

                stroke_comp = stroke_components[i]

                # 2. Handle bare "null" entries (Python None)
                if stroke_comp is None:
                    continue

                # 3. Check if 'value' key exists and is not null
                stroke_val = stroke_comp.get('value')
                if stroke_val is None:
                    continue

                # 4. Get brushstroke data
                brushstroke = stroke_val.get('brushstroke')
                if not brushstroke:
                    continue

                # 5. EXTRACT 't' VALUE
                # 't' represents the Z-order/Chronological key.
                # We do NOT use it to access the array, but we MUST save it for sorting.
                t_value = val.get('t')

                # 6. Determine Layer Name
                layer_info = val.get('layer')
                if isinstance(layer_info, dict):
                    layer_name = f"user_layer_{layer_info.get('user_layer', 0)}"
                elif isinstance(layer_info, str):
                    layer_name = layer_info
                else:
                    layer_name = "default_layer"

                # Check if this is a highlighter layer
                # is_highlighter = 'highlighter' in layer_name.lower()

                # 7. Extract Style (Handle both 'smooth' and 'textured')
                path = brushstroke.get('path')
                style_root = brushstroke.get('style', {})

                # Determine style type and extract data
                is_textured = False
                if 'textured' in style_root and style_root.get('textured') is not None:
                    style_data = style_root.get('textured')
                    is_textured = True
                else:
                    # Try 'smooth', fallback to empty dict
                    style_data = style_root.get('smooth') or {}

                # 8. Extract Style Properties
                stroke_color = style_data.get('stroke_color', {'r': 0, 'g': 0, 'b': 0, 'a': 1})
                color_rgba = [stroke_color.get('r', 0), stroke_color.get('g', 0),
                              stroke_color.get('b', 0), stroke_color.get('a', 1)]
                
                # Default width to 5.0 if not found
                stroke_width = style_data.get('stroke_width', 5.0) / 2 # Radius

                # --- PRESSURE CURVES ---
                # Get the curve type from JSON, default to 'linear'
                pressure_curve_type = style_data.get('pressure_curve', 'linear')

                # Force constant pressure for highlighters if needed
                # Probably not needed
                # if is_highlighter:
                #     pressure_curve_type = 'const'

                # Build Points List
                points_data = []

                # --- 1. START POINT (Inline Logic) ---
                start_node = path.get('start', {})
                start_pos = start_node.get('pos', [0, 0])
                p = start_node.get('pressure', 1.0)
                
                if pressure_curve_type == 'const':
                    final_start_pressure = 1.0
                elif pressure_curve_type == 'linear':
                    final_start_pressure = p
                elif pressure_curve_type == 'sqrt':
                    final_start_pressure = p ** 0.5
                elif pressure_curve_type == 'cbrt':
                    final_start_pressure = p ** (1/3)
                elif pressure_curve_type == 'pow2':
                    final_start_pressure = p * p
                elif pressure_curve_type == 'pow3':
                    final_start_pressure = p * p * p
                else:
                    final_start_pressure = p

                points_data.append({
                    'x': start_pos[0],
                    'y': start_pos[1],
                    'radius': stroke_width * final_start_pressure,
                    'opacity': 1.0
                })

                # --- 2. SEGMENTS (Loop with Inline Logic) ---
                segments = path.get('segments', [])
                for seg in segments:
                    seg_type = 'lineto' if 'lineto' in seg else 'cubbezto'
                    seg_content = seg.get(seg_type, {})
                    end_node = seg_content.get('end', {})

                    end_pos = end_node.get('pos', [0, 0])
                    
                    p = end_node.get('pressure', 1.0)

                    if pressure_curve_type == 'const':
                        final_end_pressure = 1.0
                    elif pressure_curve_type == 'linear':
                        final_end_pressure = p
                    elif pressure_curve_type == 'sqrt':
                        final_end_pressure = p ** 0.5
                    elif pressure_curve_type == 'cbrt':
                        final_end_pressure = p ** (1/3)
                    elif pressure_curve_type == 'pow2':
                        final_end_pressure = p * p
                    elif pressure_curve_type == 'pow3':
                        final_end_pressure = p * p * p
                    else:
                        final_end_pressure = p

                    points_data.append({
                        'x': end_pos[0],
                        'y': end_pos[1],
                        'radius': stroke_width * final_end_pressure,
                        'opacity': 1.0
                    })

                # Construct GPExportStroke
                gp_stroke = {
                    'color': color_rgba,
                    'is_eraser': False,
                    'hardness': 1.0,
                    'rnote_textured': is_textured,
                    'points': points_data
                }

                # Add to Layer
                if layer_name not in layers_dict:
                    layers_dict[layer_name] = {
                        'name': layer_name,
                        'opacity': 1.0,
                        'visible': True,
                        'strokes': [] 
                    }

                # Store t value alongside the stroke
                layers_dict[layer_name]['strokes'].append({
                    't': t_value,
                    'data': gp_stroke
                })

            # --- POST-PROCESSING: SORT STROKES ---

            # Now that we have all strokes, we sort them by 't' within each layer.
            # Lower 't' = Drawn first (Bottom)
            # Higher 't' = Drawn last (Top)
            for layer_name in layers_dict:
                # Sort the list of dictionaries based on the 't' key
                layers_dict[layer_name]['strokes'].sort(key=lambda x: x['t'])

                # Extract the pure stroke data, discarding the 't' wrapper
                layers_dict[layer_name]['strokes'] = [item['data'] for item in layers_dict[layer_name]['strokes']]

            # Final Data Structure matching GPExportData
            # 1. Extract layers from the dictionary
            layers_list = list(layers_dict.values())

            # 2. Separate "highlighter" layers from standard layers
            highlighter_layers = []
            standard_layers = []

            for layer in layers_list:
                if 'highlighter' in layer['name'].lower():
                    highlighter_layers.append(layer)
                else:
                    standard_layers.append(layer)

            # 3. Combine: Highlighters at the bottom (start of list), others on top.
            sorted_layers = highlighter_layers + standard_layers

            data = {
                'version': 1,
                'canvas': {
                    'width': canvas_width,
                    'height': canvas_height
                },
                'radius_scale': 1.0,
                'catmull_rom': catmull_rom,
                'layers': sorted_layers
            }

        # background_layers = [l for l in layers_list if 'background' in l['name'].lower()]
        # highlighter_layers = [l for l in layers_list if 'highlighter' in l['name'].lower()]
        # standard_layers = [l for l in layers_list if 'highlighter' not in l['name'].lower() and 'background' not in l['name'].lower()]
        #
        # # Order: Background (Bottom) -> Highlighter -> Ink (Top)
        # sorted_layers = background_layers + highlighter_layers + standard_layers

        except Exception as exc:
            self.report({'ERROR'}, f"Failed to parse Rnote file: {exc}")
            import traceback
            print(traceback.format_exc())
            return {'CANCELLED'}

        # --- Load JSON and build import job ----------------------------
        # Note: We already parsed 'data' above, so we skip load_json_file(json_path)
        # data = load_json_file(json_path)

        job = build_job(context, json_path, data, catmull_rom=catmull_rom)
        self._job = job
        ACTIVE_JOB = job

        wm = context.window_manager
        job.timer = wm.event_timer_add(0.01, window=context.window)
        wm.progress_begin(0, max(job.total_strokes, 1))
        try:
            context.workspace.status_text_set(job.progress_text)
        except Exception:
            pass

        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        job = self._job
        if job is None:
            return {'CANCELLED'}

        if event.type in {'ESC'}:
            job.cancel_requested = True

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        try:
            finished = process_job_batch(
                job, context.scene.mischief_radius_scale
            )
        except KeyboardInterrupt:
            _finalize_job(job, cancelled=True)
            self.report({'INFO'}, "Import cancelled")
            self._job = None
            return {'CANCELLED'}
        except Exception as exc:
            _cleanup_job(job, delete_partial_object=True)
            self.report({'ERROR'}, f"Import failed: {exc}")
            self._job = None
            return {'CANCELLED'}

        try:
            context.workspace.status_text_set(job.progress_text)
        except Exception:
            pass
        context.window_manager.progress_update(
            min(job.processed_strokes, max(job.total_strokes, 1))
        )

        if finished:
            _finalize_job(job, cancelled=False)
            self.report({'INFO'}, f"Imported {job.total_strokes} strokes")
            self._job = None
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def cancel(self, context):
        job = self._job
        if job is not None:
            job.cancel_requested = True
            _cleanup_job(job, delete_partial_object=True)
            self._job = None


class MISCHIEF_OT_cancel_import(Operator):
    bl_idname = "mischief_gp.cancel_import"
    bl_label = "Cancel Mischief Import"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        global ACTIVE_JOB
        if ACTIVE_JOB is None:
            self.report({'WARNING'}, "No active import to cancel")
            return {'CANCELLED'}
        ACTIVE_JOB.cancel_requested = True
        self.report({'INFO'}, "Cancel requested")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Self-test helper
# ---------------------------------------------------------------------------


def create_sample_json_file(path: str) -> str:
    sample = {
      "canvas" : {
        "height" : 1080,
        "width" : 1920
      },
      "catmull_rom" : False,
      "layers" : [
      {
          "name" : "Layer A",
          "opacity" : 1,
          "strokes" : [
            {
              "color" : [
                0.027450980618596077,
                0.27843138575553894,
                0.45882353186607361,
                1
              ],
              "hardness" : 0.94599997997283936,
              "is_eraser" : False,
              "points" : [
                {
                  "opacity" : 0.66700851917266846,
                  "radius" : 2.0012607574462891,
                  "x" : 1032.00390625,
                  "y" : 429
                },
                {
                  "opacity" : 0.76388156414031982,
                  "radius" : 2.7128660678863525,
                  "x" : 1045.0809326171875,
                  "y" : 446.92578125
                },
                {
                  "opacity" : 0.78340107202529907,
                  "radius" : 2.8562519550323486,
                  "x" : 1049.1114501953125,
                  "y" : 468.9486083984375
                },
                {
                  "opacity" : 0.78329229354858398,
                  "radius" : 2.8554525375366211,
                  "x" : 1047.43603515625,
                  "y" : 491.35540771484375
                },
                {
                  "opacity" : 0.78455042839050293,
                  "radius" : 2.8646948337554932,
                  "x" : 1043.18994140625,
                  "y" : 513.447998046875
                },
                {
                  "opacity" : 0.78825384378433228,
                  "radius" : 2.8918991088867188,
                  "x" : 1038.37744140625,
                  "y" : 535.42724609375
                },
                {
                  "opacity" : 0.79474234580993652,
                  "radius" : 2.9395623207092285,
                  "x" : 1034.0264892578125,
                  "y" : 557.5015869140625
                },
                {
                  "opacity" : 0.80200141668319702,
                  "radius" : 2.9928853511810303,
                  "x" : 1030.7486572265625,
                  "y" : 579.75714111328125
                },
                {
                  "opacity" : 0.80668431520462036,
                  "radius" : 3.027285099029541,
                  "x" : 1029.2467041015625,
                  "y" : 602.1973876953125
                },
                {
                  "opacity" : 0.80073577165603638,
                  "radius" : 2.9835882186889648,
                  "x" : 1030.3966064453125,
                  "y" : 624.6490478515625
                },
                {
                  "opacity" : 0.76662158966064453,
                  "radius" : 2.7329936027526855,
                  "x" : 1035.212890625,
                  "y" : 646.598388671875
                },
                {
                  "opacity" : 0.70987176895141602,
                  "radius" : 2.3161232471466064,
                  "x" : 1043.430419921875,
                  "y" : 667.441650390625
                },
                {
                  "opacity" : 0.70639312267303467,
                  "radius" : 2.2905704975128174,
                  "x" : 1045.16015625,
                  "y" : 669.6875
                }
              ]
            }
          ],
          "visible" : True
        },
        {
          "name" : "Layer B",
          "opacity" : 1,
          "strokes" : [
            {
              "color" : [
                0.92941176891326904,
                0.82745099067687988,
                0.22745098173618317,
                1
              ],
              "hardness" : 1,
              "is_eraser" : False,
              "points" : [
                {
                  "opacity" : 1,
                  "radius" : 3.8137996196746826,
                  "x" : 897.01953125,
                  "y" : 608
                },
                {
                  "opacity" : 1,
                  "radius" : 5.892493724822998,
                  "x" : 926.754638671875,
                  "y" : 604.6705322265625
                },
                {
                  "opacity" : 1,
                  "radius" : 7.3489499092102051,
                  "x" : 954.37689208984375,
                  "y" : 593.20233154296875
                },
                {
                  "opacity" : 1,
                  "radius" : 9.3897066116333008,
                  "x" : 979.16571044921875,
                  "y" : 576.3424072265625
                },
                {
                  "opacity" : 1,
                  "radius" : 11.676648139953613,
                  "x" : 1002.69921875,
                  "y" : 557.73760986328125
                },
                {
                  "opacity" : 1,
                  "radius" : 14.196652412414551,
                  "x" : 1026.312744140625,
                  "y" : 539.23541259765625
                },
                {
                  "opacity" : 1,
                  "radius" : 17.023775100708008,
                  "x" : 1050.720947265625,
                  "y" : 521.80145263671875
                },
                {
                  "opacity" : 1,
                  "radius" : 19.754560470581055,
                  "x" : 1076.4140625,
                  "y" : 506.33526611328125
                },
                {
                  "opacity" : 1,
                  "radius" : 22.204998016357422,
                  "x" : 1103.666015625,
                  "y" : 493.84808349609375
                },
                {
                  "opacity" : 1,
                  "radius" : 24.04461669921875,
                  "x" : 1132.56298828125,
                  "y" : 485.95556640625
                },
                {
                  "opacity" : 1,
                  "radius" : 24.316179275512695,
                  "x" : 1162.450927734375,
                  "y" : 484.731689453125
                },
                {
                  "opacity" : 1,
                  "radius" : 18.647190093994141,
                  "x" : 1191.0821533203125,
                  "y" : 493.13079833984375
                },
                {
                  "opacity" : 1,
                  "radius" : 15.876124382019043,
                  "x" : 1198.48828125,
                  "y" : 498
                }
              ]
            },
            {
              "color" : [
                0.14509804546833038,
                0.51764708757400513,
                0.78431373834609985,
                1
              ],
              "hardness" : 1,
              "is_eraser" : True,
              "points" : [
                {
                  "opacity" : 1,
                  "radius" : 1.0507797002792358,
                  "x" : 938,
                  "y" : 517
                }
              ]
            },
            {
              "color" : [
                0.14509804546833038,
                0.51764708757400513,
                0.78431373834609985,
                1
              ],
              "hardness" : 1,
              "is_eraser" : True,
              "points" : [
                {
                  "opacity" : 1,
                  "radius" : 11.85765552520752,
                  "x" : 1067,
                  "y" : 443
                },
                {
                  "opacity" : 1,
                  "radius" : 16.701040267944336,
                  "x" : 1062.45556640625,
                  "y" : 472.6221923828125
                },
                {
                  "opacity" : 1,
                  "radius" : 17.5,
                  "x" : 1054.3619384765625,
                  "y" : 501.49749755859375
                },
                {
                  "opacity" : 1,
                  "radius" : 17.5,
                  "x" : 1043.9930419921875,
                  "y" : 529.64324951171875
                },
                {
                  "opacity" : 1,
                  "radius" : 17.5,
                  "x" : 1032.388916015625,
                  "y" : 557.30731201171875
                },
                {
                  "opacity" : 1,
                  "radius" : 17.5,
                  "x" : 1020.6407470703125,
                  "y" : 584.9111328125
                },
                {
                  "opacity" : 1,
                  "radius" : 17.5,
                  "x" : 1009.2140502929688,
                  "y" : 612.6495361328125
                },
                {
                  "opacity" : 1,
                  "radius" : 14.766080856323242,
                  "x" : 1001.5,
                  "y" : 624.875
                }
              ]
            }
          ],
          "visible" : True
        }
      ],
      "radius_scale" : 0.0099999997764825821,
      "version" : 1
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(sample, fh, indent=2)
    return path


class MISCHIEF_OT_import_sample_json(Operator):
    bl_idname = "mischief_gp.import_sample_json"
    bl_label = "Import Sample Mischief JSON"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "Import from test data string"

    def execute(self, context):
        tmpdir = tempfile.gettempdir()
        path = os.path.join(tmpdir, "mischief_gp_sample.json")
        create_sample_json_file(path)
        data = load_json_file(path)
        catmull_rom = bool(context.scene.mischief_catmull_rom)

        job = None
        try:
            job = build_job(context, path, data, catmull_rom=catmull_rom)
            radius_scale = context.scene.mischief_radius_scale
            while not process_job_batch(job, radius_scale):
                pass
        except Exception as exc:
            _cleanup_job(job, delete_partial_object=True)
            self.report({'ERROR'}, f"Sample import failed: {exc}")
            return {'CANCELLED'}

        _finalize_job(job, cancelled=False)
        self.report({'INFO'}, f"Sample import completed: {job.total_strokes} strokes")
        return {'FINISHED'}

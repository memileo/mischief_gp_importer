from __future__ import annotations

import bpy


def _ensure_gp_material_data(material: bpy.types.Material) -> None:
    if getattr(material, "grease_pencil", None) is None:
        create = getattr(bpy.data.materials, "create_gpencil_data", None)
        if create is not None:
            create(material)


def _set_if_has(obj, attr: str, value) -> bool:
    if hasattr(obj, attr):
        try:
            setattr(obj, attr, value)
            return True
        except Exception:
            return False
    return False


def create_gp_material(
    name: str,
    color=(1.0, 1.0, 1.0, 1.0),
    holdout: bool = False,
    is_pencil: bool = False,
    rnote_textured: bool = False,
) -> bpy.types.Material:
    material = bpy.data.materials.get(name)
    if material is None:
        material = bpy.data.materials.new(name=name)

    _ensure_gp_material_data(material)
    gp = material.grease_pencil

    # Base stroke settings
    _set_if_has(gp, "mode", 'LINE')
    _set_if_has(gp, "stroke_style", 'SOLID')
    _set_if_has(gp, "show_stroke", True)
    _set_if_has(gp, "show_fill", False)
    _set_if_has(gp, "color", color)
    _set_if_has(gp, "mix_factor", 1.0)
    _set_if_has(gp, "mix_color", (1.0, 1.0, 1.0, 1.0))

    # Enable vertex colour so the per-point Color attribute is rendered
    _set_if_has(gp, "use_vertex_color_stroke", True)
    for val in ('VERTEX', 'VERTEXCOLOR'):
        if _set_if_has(gp, "color_type", val):
            break

    # Pencil-specific (soft strokes benefit from overlap visibility)
    # if is_pencil:
    #     _set_if_has(gp, "use_overlap_strokes", True)

    _set_if_has(gp, "use_overlap_strokes", True) # Use Self Overlap for all materials as it matches Mischief closer

    # Holdout — the correct GPv3 property is use_stroke_holdout
    if holdout:
        _set_if_has(gp, "use_stroke_holdout", True)
        # Fallbacks for older / different builds
        for prop_name in ("holdout", "use_holdout", "show_holdout"):
            _set_if_has(gp, prop_name, True)
        _set_if_has(material, "use_holdout", True)

    # Viewport colour hint
    if hasattr(material, "diffuse_color"):
        try:
            material.diffuse_color = color
        except Exception:
            pass

    return material


def ensure_material_slots(
    obj: bpy.types.Object,
    color_material: bpy.types.Material,
    eraser_material: bpy.types.Material,
    pencil_color_material: bpy.types.Material,
    pencil_eraser_material: bpy.types.Material,
    rnote_textured_material: bpy.types.Material,
) -> None:
    """Ensure slot layout: 0=Color, 1=Eraser, 2=Pencil Color, 3=Pencil Eraser."""
    mats = obj.data.materials
    expected = [
        color_material,
        eraser_material,
        pencil_color_material,
        pencil_eraser_material,
        rnote_textured_material,
    ]

    for i, mat in enumerate(expected):
        if len(mats) <= i:
            mats.append(mat)
        else:
            mats[i] = mat

    # Remove leftover slots
    while len(mats) > len(expected):
        mats.pop(index=len(mats) - 1)

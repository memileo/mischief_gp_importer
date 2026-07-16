bl_info = {
    "name": "Mischief Grease Pencil Importer",
    "author": "Ajaj",
    "version": (1, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Mischief",
    "description": "Import Mischief JSON into Grease Pencil objects",
    "category": "Import-Export",
}

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy.types import Panel

from bpy_extras.io_utils import (
    ImportHelper,
    poll_file_object_drop,
)

from . import importer as _importer

# Exported so importer.py can look up addon preferences
MISCHIEFIMPORTER_ADDON_PACKAGE = __package__


# ---------------------------------------------------------------------------
# Addon Preferences (persist across sessions & scenes)
# ---------------------------------------------------------------------------


class MISCHIEF_Preferences(bpy.types.AddonPreferences):
    bl_idname = MISCHIEFIMPORTER_ADDON_PACKAGE

    art2png_path: StringProperty(
        name="art2png Path",
        subtype='FILE_PATH',
        default="",
        description="Path to the art2png executable for converting .art files",
    )

    def draw(self, context):
        self.layout.prop(self, "art2png_path")


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class MISCHIEF_PT_importer(Panel):
    bl_label = "Mischief Import"
    bl_idname = "MISCHIEF_PT_importer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GP Importer'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Graceful guard if properties aren't initialised yet
        if not hasattr(scene, "mischief_radius_scale"):
            layout.label(text="Re-enable addon to initialise properties")
            return

        try:
            self._draw_content(context, layout, scene)
        except Exception as exc:
            layout.label(text=f"Panel error: {exc}")

    def _draw_content(self, context, layout, scene):
        layout.prop(scene, "mischief_radius_scale")
        layout.prop(scene, "mischief_resample_step")
        layout.prop(scene, "mischief_catmull_rom")

        # art2png path from addon preferences (persists across scenes)
        prefs = context.preferences.addons[MISCHIEFIMPORTER_ADDON_PACKAGE].preferences
        layout.prop(prefs, "art2png_path")

        # layout.operator(
        #     _importer.MISCHIEF_OT_import_sample_json.bl_idname,
        #     text="Test Import",
        #     icon='EXPERIMENTAL', # CHAR_REPLACEMENT COLOR EXPERIMENTAL MESH_DATA QUESTION
        # )

        # layout.separator(type='LINE')
        layout.separator()

        big_row = layout.row()
        big_row.scale_y = 1.5

        big_row.operator(
            _importer.MISCHIEF_OT_import_json.bl_idname,
            text="Import...",
            icon='FILE_FOLDER',
        )

        active_job = _importer.ACTIVE_JOB
        if active_job is not None:
            box = layout.box()
            box.label(text=active_job.progress_text or "Importing...")
            box.operator(
                _importer.MISCHIEF_OT_cancel_import.bl_idname,
                text="Cancel",
                icon='CANCEL',
            )
        else:
            layout.label(text="No active import")


class MISCHIEF_PT_importer_dev(Panel):
    # Optional: Set to False to completely hide this sub-panel from users
    DRAW_EPHEMERAL = True

    bl_label = " "
    bl_icon = 'EXPERIMENTAL'
    bl_idname = "MISCHIEF_PT_importer_dev"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GP Importer'
    bl_parent_id = MISCHIEF_PT_importer.bl_idname  # Links it as a collapsible sub-panel
    bl_options = {'DEFAULT_CLOSED'}  # Starts collapsed

    @classmethod
    def poll(cls, context):
        return cls.DRAW_EPHEMERAL

    def draw(self, context):
        layout = self.layout
        layout.operator(
            _importer.MISCHIEF_OT_import_sample_json.bl_idname,
            text="Test Import",
            icon='EXPERIMENTAL',
        )


class RNOTE_PT_importer(Panel):
    bl_label = "Rnote Import"
    bl_idname = "RNOTE_PT_importer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'GP Importer'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Graceful guard if properties aren't initialised yet
        if not hasattr(scene, "mischief_radius_scale"):
            layout.label(text="Re-enable addon to initialise properties")
            return

        try:
            self._draw_content(context, layout, scene)
        except Exception as exc:
            layout.label(text=f"Panel error: {exc}")

    def _draw_content(self, context, layout, scene):
        layout.prop(scene, "mischief_radius_scale")
        layout.prop(scene, "mischief_catmull_rom")
        # layout.separator(type='LINE')
        layout.separator()

        big_row = layout.row()
        big_row.scale_y = 1.5

        big_row.operator(
            _importer.RNOTE_OT_import_json.bl_idname,
            text="Import...",
            icon='FILE_FOLDER',
        )

        active_job = _importer.ACTIVE_JOB
        if active_job is not None:
            box = layout.box()
            box.label(text=active_job.progress_text or "Importing...")
            box.operator(
                _importer.MISCHIEF_OT_cancel_import.bl_idname,
                text="Cancel",
                icon='CANCEL',
            )
        else:
            layout.label(text="No active import")

# ---------------------------------------------------------------------------
# Import menu
# ---------------------------------------------------------------------------

def menu_func_import(self, context):
    self.layout.operator(
        _importer.MISCHIEF_OT_import_json.bl_idname,
        text="Mischief (.art)",
    )
    self.layout.operator(
        _importer.RNOTE_OT_import_json.bl_idname,
        text="Rnote (.rnote)",
    )

# ---------------------------------------------------------------------------
# File handlers
# ---------------------------------------------------------------------------

class RNOTE_FH_rnote(bpy.types.FileHandler):
    bl_idname = "RNOTE_FH_rnote"
    bl_label = "Rnote"
    bl_import_operator = "rnote_gp.import_json"
    bl_file_extensions = ".rnote"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


class MISCHIEF_FH_art(bpy.types.FileHandler):
    bl_idname = "MISCHIEF_FH_art"
    bl_label = "Mischief"
    bl_import_operator = "mischief_gp.import_json"
    bl_file_extensions = ".art"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


classes = (
    MISCHIEF_Preferences,
    _importer.MISCHIEF_OT_import_json,
    _importer.RNOTE_OT_import_json,
    _importer.MISCHIEF_OT_cancel_import,
    _importer.MISCHIEF_OT_import_sample_json,
    MISCHIEF_PT_importer,
    RNOTE_PT_importer,
    MISCHIEF_PT_importer_dev,
)


def register():
    # Scene properties BEFORE classes (panel's first draw must find them)
    if not hasattr(bpy.types.Scene, "mischief_radius_scale"):
        bpy.types.Scene.mischief_radius_scale = FloatProperty(
            name="Radius Scale",
            default=0.0045,
            min=0.0001,
            max=1.0,
            step=0.001,
            description="Meters per pixel (0.01 = 1 cm)",
        )
    if not hasattr(bpy.types.Scene, "mischief_resample_step"):
        bpy.types.Scene.mischief_resample_step = FloatProperty(
            name="Resample Step",
            default=4.0,
            min=0.1,
            soft_max=100.0,
            step=1.0,
            description="Resampling density / step size passed to art2png",
        )
    if not hasattr(bpy.types.Scene, "mischief_catmull_rom"):
        bpy.types.Scene.mischief_catmull_rom = BoolProperty(
            name="Catmull-Rom Strokes",
            default=False,
            description=(
                "Import strokes as Catmull-Rom curves instead of polylines. \n"
                "Skips resampling in art2png and uses raw control points"
            ),
        )

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

    bpy.utils.register_class(RNOTE_FH_rnote)
    bpy.utils.register_class(MISCHIEF_FH_art)


def unregister():
    bpy.utils.unregister_class(RNOTE_FH_rnote)
    bpy.utils.unregister_class(MISCHIEF_FH_art)

    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for prop_name in ("mischief_radius_scale", "mischief_resample_step", "mischief_catmull_rom"):
        try:
            if hasattr(bpy.types.Scene, prop_name):
                delattr(bpy.types.Scene, prop_name)
        except Exception:
            pass

if __name__ == "__main__":
    register()

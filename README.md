#### AI Disclaimer: In large part copy-paste coded with various LLMs.
<picture>
<img width="256" height="256" alt="mischief_gp_importer logo" src="https://github.com/user-attachments/assets/997a21b8-9b56-4709-bfdb-81510a3521ef" />
</picture>

# mischief_gp_importer

Blender Grease Pencil importer for Mischief (.art) files and Rnote (.rnote) files.
## Requirements
- Blender 4.3+ (GPv3)
- Mischief import requires [art2png binary](https://github.com/memileo/mischief-re-swift) (Select path to the binary in the N-panel or Preferences.)

## Usage:
**File → Import → Mischief/Rnote:** Imports with current settings.

**Drag and drop:** Imports with current settings.

**N-panel GP Importer category:** Set import settings and import.

## Notes:
Can import brush strokes, layers. Pencil strokes have their own material so you can apply noise texture via compositing, noise modifier or bitmap brush texture.

Layers containing eraser brush strokes relies on what might be a bug: and are set to 0.99999 opacity for correct compositing. When set to 1.0 it erase/hold-out blends all layers below instead of only content on the layer itself.

All imported objects share the same materials and are overwritten with default values when importing another object. Rename the materials if you need to keep them separate.

### Issues/missing:
Polygon lines, rectangle, ellipses and embedded images are not supported.
Marker/calligraphy type brush strokes imports as regular strokes.
Copy data, rectangle selection transforms and merged layers is not implemented.
The pressure isn't read 100% correctly: Some strokes may have the wrong thickness (mostly short/dot-type strokes).
Tapering of strokes can appear jagged.

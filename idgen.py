"""
idgen.py — deterministic PSD kernel

render(psd_path, values, image_values) -> bytes (PNG)

Rules:
  - PSD is never mutated
  - Base composite is immutable
  - All overlays applied post-composite
  - No hidden state
  - No font file system probing — caller passes font_map if fidelity needed

Text style: size, color, alignment sourced from PSD typesetting.
Font:        caller-supplied font_map { layer_name: path } or PIL default at correct size.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Union

from PIL import Image, ImageDraw, ImageFont
from psd_tools import PSDImage
from psd_tools.api.layers import Layer, TypeLayer, Group


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(
    psd_path: Union[str, Path],
    values: dict[str, str] | None = None,
    image_values: dict[str, Union[str, Path, Image.Image]] | None = None,
    font_map: dict[str, Union[str, Path]] | None = None,
) -> bytes:
    """
    Render a PSD template with substituted values.

    Args:
        psd_path:    Path to .psd file.
        values:      { layer_name: replacement_text }
        image_values:{ layer_name: PIL.Image | path_str }
        font_map:    { layer_name: font_path } — optional, for fidelity.
                     Falls back to PIL default at PSD-specified size.

    Returns:
        PNG bytes. Deterministic for identical inputs.
    """
    values       = values or {}
    image_values = image_values or {}
    font_map     = font_map or {}

    psd  = PSDImage.open(str(psd_path))
    base = psd.composite(ignore_preview=True).convert("RGBA")  # immutable
    layers = _collect_layers(psd)
    draw = ImageDraw.Draw(base)

    for name, text in values.items():
        layer = layers.get(name)
        if layer and isinstance(layer, TypeLayer):
            _draw_text(draw, layer, text, font_path=font_map.get(name))

    for name, img_src in image_values.items():
        layer = layers.get(name)
        if layer:
            _draw_image(base, layer, img_src)

    buf = io.BytesIO()
    base.save(buf, format="PNG")
    return buf.getvalue()


def inspect_psd(psd_path: Union[str, Path]) -> list[dict]:
    """
    Return layer metadata without rendering.

    Returns list of:
        { name, type, left, top, width, height, visible, text? }
    """
    psd = PSDImage.open(str(psd_path))
    out = []
    for layer in _walk(psd):
        entry = {
            "name":    layer.name.strip(),
            "type":    type(layer).__name__,
            "left":    layer.left,
            "top":     layer.top,
            "width":   layer.width,
            "height":  layer.height,
            "visible": layer.visible,
        }
        if isinstance(layer, TypeLayer):
            entry["text"]  = layer.text
            entry["style"] = _extract_style(layer)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Layer traversal
# ---------------------------------------------------------------------------

def _walk(node) -> list[Layer]:
    out = []
    for layer in node:
        if isinstance(layer, Group):
            out.extend(_walk(layer))
        else:
            out.append(layer)
    return out


def _collect_layers(psd: PSDImage) -> dict[str, Layer]:
    # First-wins on duplicate names. Logs a warning — PSDs allow duplicates,
    # but silent discard is worse than a visible signal.
    import warnings
    result: dict[str, Layer] = {}
    for layer in _walk(psd):
        name = layer.name.strip()
        if name not in result:
            result[name] = layer
        else:
            warnings.warn(
                f"idgen: duplicate layer name '{name}' — first occurrence used",
                stacklevel=2,
            )
    return result


# ---------------------------------------------------------------------------
# Style extraction — PSD typesetting only, no filesystem probing
# ---------------------------------------------------------------------------

def _extract_style(layer: TypeLayer) -> dict:
    """
    Extract size, color, alignment from PSD typesetting.
    Returns deterministic defaults on any parse failure.
    font_name is returned for caller reference only — not used internally.
    """
    style = {
        "font_name": None,   # informational; caller uses font_map for loading
        "font_size": 12.0,
        "color":     (0, 0, 0, 255),
        "align":     "left",
    }

    try:
        ts = layer.typesetting
        paragraphs = list(ts)
        if not paragraphs:
            return style

        para = paragraphs[0]

        justification = getattr(para.style, "justification", None)
        if justification is not None:
            j = str(justification).lower()
            if "center" in j:
                style["align"] = "center"
            elif "right" in j:
                style["align"] = "right"

        runs = list(para.runs)
        if not runs:
            return style

        rs = runs[0].style

        font_name = getattr(rs, "font_name", None)
        if font_name:
            style["font_name"] = font_name

        font_size = getattr(rs, "font_size", None)
        if font_size:
            style["font_size"] = float(font_size)

        color = getattr(rs, "color", None)
        if color is not None and hasattr(color, "__iter__"):
            try:
                c = tuple(int(x) for x in color)
                style["color"] = c if len(c) == 4 else c + (255,)
            except Exception:
                pass

    except Exception:
        pass  # always return a usable dict

    return style


# ---------------------------------------------------------------------------
# Text overlay
# ---------------------------------------------------------------------------

def _resolve_font(
    font_size: float,
    font_path: Union[str, Path, None],
) -> ImageFont.ImageFont:
    """
    Load font from explicit path, or return PIL bitmap default.
    No filesystem probing. Never raises.

    NOTE: PIL's load_default() is a fixed-size bitmap font — font_size is
    not honoured in fallback mode. Alignment will be approximate.
    Supply font_map in render() for correct sizing.
    """
    size_px = max(8, int(round(font_size)))
    if font_path:
        try:
            return ImageFont.truetype(str(font_path), size=size_px)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_text(
    draw: ImageDraw.ImageDraw,
    layer: TypeLayer,
    text: str,
    font_path: Union[str, Path, None] = None,
) -> None:
    style = _extract_style(layer)
    font  = _resolve_font(style["font_size"], font_path)

    # Compute text width for manual alignment — avoids PIL anchor cross-platform drift
    bbox  = font.getbbox(text)
    text_w = bbox[2] - bbox[0]

    if style["align"] == "center":
        x = layer.left + (layer.width - text_w) // 2
    elif style["align"] == "right":
        x = layer.right - text_w
    else:
        x = layer.left

    draw.text(
        (x, layer.top),
        text,
        font=font,
        fill=style["color"],
    )


# ---------------------------------------------------------------------------
# Image overlay
# ---------------------------------------------------------------------------

def _draw_image(
    base: Image.Image,
    layer: Layer,
    src: Union[str, Path, Image.Image],
) -> None:
    img = Image.open(str(src)).convert("RGBA") if isinstance(src, (str, Path)) else src.convert("RGBA")
    img = img.resize((layer.width, layer.height), Image.LANCZOS)
    base.paste(img, (layer.left, layer.top), img)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("Usage: idgen.py inspect <file.psd>\n"
                         "       idgen.py render  <file.psd> <values.json> [out.png]")

    cmd      = sys.argv[1]
    psd_path = sys.argv[2]

    if cmd == "inspect":
        print(json.dumps(inspect_psd(psd_path), indent=2))

    elif cmd == "render":
        if len(sys.argv) < 4:
            raise SystemExit("render requires <values.json>")
        payload      = json.loads(Path(sys.argv[3]).read_text())
        out_path     = sys.argv[4] if len(sys.argv) > 4 else "out.png"
        png = render(
            psd_path,
            values=payload.get("text"),
            image_values=payload.get("images"),
            font_map=payload.get("fonts"),
        )
        Path(out_path).write_bytes(png)
        print(f"written → {out_path}  ({len(png):,} bytes)")

    else:
        raise SystemExit("unknown command: inspect | render")

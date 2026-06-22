#!/usr/bin/env python3
"""
app.py — tkinter GUI for PSD template rendering

Two modes:
  1. New template: open PSD → configure fields → save as JSON
  2. Open template: load JSON → fill values → export PNG

Field model (the only state that matters):
  label    — display name
  key      — snake_case identifier
  value    — current text / image path
  editable — shown in the fill form
  locked   — prevents accidental edits in the editor
"""

import json
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk, ImageDraw
from io import BytesIO

from idgen import inspect_psd, render


# ---------------------------------------------------------------------------
# Field model
# ---------------------------------------------------------------------------

def _to_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "field"


class Field(dict):
    """Field with enforced lock semantics at the setter boundary."""
    def __setitem__(self, key, val):
        # locked=true blocks any value mutation (anywhere: UI, CLI, batch)
        if self.get("locked") and key == "value":
            raise ValueError(f"Field '{self.get('label')}' is locked")
        super().__setitem__(key, val)

    def to_json(self) -> dict:
        """Export as plain dict for JSON serialization."""
        return dict(self)


def make_field(layer: dict) -> Field:
    return Field({
        "label":    layer["name"],
        "key":      _to_key(layer["name"]),
        "value":    layer.get("text", ""),
        "editable": layer["type"] == "TypeLayer",
        "locked":   False,
    })


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

class _SimpleEntry(tk.Toplevel):
    """Single-line input dialog. result is None on cancel."""
    def __init__(self, parent, title, label, value):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None
        ttk.Label(self, text=label).pack(padx=20, pady=(15, 4))
        self.var = tk.StringVar(value=value)
        e = ttk.Entry(self, textvariable=self.var, width=38)
        e.pack(padx=20)
        e.select_range(0, tk.END)
        e.focus_set()
        f = ttk.Frame(self)
        f.pack(pady=12)
        ttk.Button(f, text="OK",     command=self._ok).pack(side=tk.LEFT, padx=5)
        ttk.Button(f, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=5)
        self.bind("<Return>", lambda _: self._ok())
        self.bind("<Escape>", lambda _: self.destroy())
        self.transient(parent)
        self.grab_set()
        parent.wait_window(self)

    def _ok(self):
        self.result = self.var.get()
        self.destroy()


# ---------------------------------------------------------------------------
# Mode selector
# ---------------------------------------------------------------------------

class ModeSelector(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("idgen — PSD Template Manager")
        self.geometry("400x200")
        self.resizable(False, False)

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="Select mode", font=("Arial", 14, "bold")).pack(pady=20)
        ttk.Button(frame, text="New Template",  command=self.new_template).pack(pady=10, fill=tk.X)
        ttk.Button(frame, text="Open Template", command=self.open_template).pack(pady=10, fill=tk.X)

    def new_template(self):
        self.destroy()
        TemplateEditor().mainloop()

    def open_template(self):
        self.destroy()
        TemplateLoader().mainloop()


# ---------------------------------------------------------------------------
# Template editor (new template)
# ---------------------------------------------------------------------------

class TemplateEditor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("idgen — New Template")
        self.geometry("1200x700")

        self.psd_path       = None
        self.layers         = []   # raw inspect_psd output, ordered
        self.layer_index    = {}   # name → layer dict
        self.fields         = {}   # name → field dict (THE model)
        self.field_vars     = {}   # name → {"editable": BooleanVar, "locked": BooleanVar}
        self.selected_layer = None
        self.preview_tk     = None
        self._base_img      = None  # cached composite; re-set only on open_psd
        self._last_img      = None  # last blitted frame; re-blitted on canvas resize

        self._build_ui()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)
        ttk.Button(top, text="Open PSD", command=self.open_psd).pack(side=tk.LEFT)
        self.psd_label = ttk.Label(top, text="No PSD loaded")
        self.psd_label.pack(side=tk.LEFT, padx=10)

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = ttk.LabelFrame(main, text="Layers")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        self.layer_canvas = tk.Canvas(left)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.layer_canvas.yview)
        self.layer_frame = ttk.Frame(self.layer_canvas)
        self.layer_frame.bind(
            "<Configure>",
            lambda e: self.layer_canvas.configure(scrollregion=self.layer_canvas.bbox("all"))
        )
        self.layer_canvas.create_window((0, 0), window=self.layer_frame, anchor="nw")
        self.layer_canvas.configure(yscrollcommand=sb.set)
        self.layer_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = ttk.LabelFrame(main, text="Preview")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        self.preview_canvas = tk.Canvas(right, bg="gray20")
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda e: self._blit_cached())

        bottom = ttk.Frame(self)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        ttk.Button(bottom, text="Save Template", command=self.save_template).pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    # PSD loading
    # ------------------------------------------------------------------

    def open_psd(self):
        path = filedialog.askopenfilename(filetypes=[("PSD files", "*.psd"), ("All files", "*")])
        if not path:
            return

        self.psd_path    = Path(path)
        self.layers      = inspect_psd(str(self.psd_path))
        self.layer_index = {l["name"]: l for l in self.layers}
        self.fields      = {l["name"]: make_field(l) for l in self.layers}
        self.selected_layer = None

        self.psd_label.config(text=f"Loaded: {self.psd_path.name}")

        self._base_img = None
        self._last_img = None
        try:
            self._base_img = Image.open(BytesIO(render(str(self.psd_path)))).convert("RGBA")
        except Exception as e:
            messagebox.showerror("Render error", f"Could not composite PSD:\n{e}")

        self._populate_layer_list()
        self._update_preview()

    def invalidate_base_cache(self):
        """Call explicitly when PSD-level state changes (DPI, font-map, etc.)."""
        self._base_img = None
        self._last_img = None
        if self.psd_path:
            try:
                self._base_img = Image.open(BytesIO(render(str(self.psd_path)))).convert("RGBA")
            except Exception as e:
                messagebox.showerror("Render error", str(e))
        self._update_preview()

    # ------------------------------------------------------------------
    # Layer list
    # ------------------------------------------------------------------

    def _populate_layer_list(self):
        for w in self.layer_frame.winfo_children():
            w.destroy()
        self.field_vars = {}

        for layer in self.layers:
            name  = layer["name"]
            field = self.fields[name]

            row = ttk.Frame(self.layer_frame)
            row.pack(fill=tk.X, padx=4, pady=2)

            sel_text = "●" if name == self.selected_layer else "○"
            sel_btn = ttk.Button(row, text=sel_text, width=2,
                                 command=lambda n=name: self._select_layer(n))
            sel_btn.pack(side=tk.LEFT)

            lbl = ttk.Label(row, text=field["label"], width=22, anchor="w")
            lbl.pack(side=tk.LEFT, padx=5)
            lbl.bind("<Double-1>", lambda e, n=name: self._rename_field(n))

            for widget in (row, sel_btn, lbl):
                widget.bind("<Button-2>", lambda e, n=name: self._show_context_menu(e, n))
                widget.bind("<Button-3>", lambda e, n=name: self._show_context_menu(e, n))

            ev = tk.BooleanVar(value=field["editable"])
            lv = tk.BooleanVar(value=field["locked"])
            self.field_vars[name] = {"editable": ev, "locked": lv}

            ev.trace_add("write", lambda *a, n=name: self._on_editable_toggle(n))
            lv.trace_add("write", lambda *a, n=name: self._on_lock_toggle(n))

            chk = ttk.Frame(row)
            chk.pack(side=tk.LEFT)
            ttk.Checkbutton(
                chk, text="Edit", variable=ev,
                state="disabled" if field["locked"] else "normal"
            ).pack(side=tk.LEFT)
            ttk.Checkbutton(chk, text="Lock", variable=lv).pack(side=tk.LEFT)

    def _on_editable_toggle(self, name):
        field = self.fields[name]
        if field["locked"]:
            # silently revert — locked fields block editable changes
            self.field_vars[name]["editable"].set(field["editable"])
            return
        field["editable"] = self.field_vars[name]["editable"].get()
        self._update_preview()

    def _on_lock_toggle(self, name):
        self.fields[name]["locked"] = self.field_vars[name]["locked"].get()
        self._populate_layer_list()  # rebuild to enable/disable Edit checkbox

    def _select_layer(self, name):
        self.selected_layer = name
        self._populate_layer_list()
        self._update_preview()

    # ------------------------------------------------------------------
    # Context menu (3 actions only)
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, name):
        field = self.fields[name]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Rename",     command=lambda: self._rename_field(name))
        menu.add_command(label="Edit Value", command=lambda: self._edit_value(name))
        menu.add_command(
            label="Unlock" if field["locked"] else "Lock",
            command=lambda: self._toggle_lock(name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _rename_field(self, name):
        dlg = _SimpleEntry(self, "Rename Field", "Label:", self.fields[name]["label"])
        if dlg.result and dlg.result.strip():
            self.fields[name]["label"] = dlg.result.strip()
            self._populate_layer_list()

    def _edit_value(self, name):
        field = self.fields[name]
        dlg = _SimpleEntry(self, f"Edit Value — {field['label']}", "Value:", field["value"])
        if dlg.result is not None:
            field["value"] = dlg.result
            self._update_preview()

    def _toggle_lock(self, name):
        new_val = not self.fields[name]["locked"]
        self.fields[name]["locked"] = new_val
        self.field_vars[name]["locked"].set(new_val)
        # trace on locked var triggers _on_lock_toggle → _populate_layer_list

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------

    def _update_preview(self):
        if not self._base_img:
            self.preview_canvas.delete("all")
            cw = max(self.preview_canvas.winfo_width(), 10)
            ch = max(self.preview_canvas.winfo_height(), 10)
            self.preview_canvas.create_text(
                cw // 2, ch // 2,
                text="No preview — render failed",
                fill="gray50", font=("Arial", 13))
            return

        img  = self._base_img.copy()
        draw = ImageDraw.Draw(img)

        for layer in self.layers:
            name  = layer["name"]
            field = self.fields.get(name)
            if not field or not field["editable"]:
                continue
            color = (0, 255, 0) if name == self.selected_layer else (100, 150, 200)
            draw.rectangle(
                [layer["left"],  layer["top"],
                 layer["left"] + layer["width"],
                 layer["top"]  + layer["height"]],
                outline=color, width=2)

        self._blit(img)

    def _blit(self, img: Image.Image = None):
        if img is not None:
            self._last_img = img
        if self._last_img is None:
            return
        cw    = max(self.preview_canvas.winfo_width(),  100)
        ch    = max(self.preview_canvas.winfo_height(), 100)
        frame = self._last_img.copy()
        frame.thumbnail((cw, ch), Image.LANCZOS)
        self.preview_tk = ImageTk.PhotoImage(frame)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2,
                                          image=self.preview_tk, anchor="center")

    def _blit_cached(self):
        if self._last_img is not None:
            self._blit()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_template(self):
        if not self.psd_path:
            messagebox.showwarning("No PSD", "Load a PSD first")
            return

        template = {
            "psd_path": str(self.psd_path),
            "fields": {
                name: field.to_json()
                for name, field in self.fields.items()
                if field["editable"]
            },
        }
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*")])
        if not path:
            return
        Path(path).write_text(json.dumps(template, indent=2))
        messagebox.showinfo("Saved", f"Template saved to {path}")


# ---------------------------------------------------------------------------
# Template loader (fill and export)
# ---------------------------------------------------------------------------

class TemplateLoader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("idgen — Open Template")
        self.geometry("1200x700")

        self.template   = None
        self.psd_path   = None
        self.psd_layers = []
        self._base_img  = None
        self._last_img  = None
        self.form_vars  = {}   # layer_name → StringVar (text fields)
        self.image_vars = {}   # layer_name → StringVar (image path)
        self.preview_tk = None

        self._build_ui()

    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)
        ttk.Button(top, text="Open Template", command=self.open_template).pack(side=tk.LEFT)
        self.template_label = ttk.Label(top, text="No template loaded")
        self.template_label.pack(side=tk.LEFT, padx=10)

        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        left = ttk.LabelFrame(main, text="Editable Fields")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        self.form_canvas = tk.Canvas(left)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.form_canvas.yview)
        self.form_frame = ttk.Frame(self.form_canvas)
        self.form_frame.bind(
            "<Configure>",
            lambda e: self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all"))
        )
        self.form_canvas.create_window((0, 0), window=self.form_frame, anchor="nw")
        self.form_canvas.configure(yscrollcommand=sb.set)
        self.form_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        right = ttk.LabelFrame(main, text="Live Preview")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        self.preview_canvas = tk.Canvas(right, bg="gray20")
        self.preview_canvas.pack(fill=tk.BOTH, expand=True)
        self.preview_canvas.bind("<Configure>", lambda e: self._blit_cached())

        bottom = ttk.Frame(self)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        ttk.Button(bottom, text="Export PNG", command=self.export_png).pack(side=tk.LEFT)

    def open_template(self):
        path = filedialog.askopenfilename(
            filetypes=[("JSON files", "*.json"), ("All files", "*")])
        if not path:
            return

        self.template = json.loads(Path(path).read_text())
        self.psd_path = Path(self.template["psd_path"])

        if not self.psd_path.exists():
            messagebox.showerror("Error", f"PSD not found: {self.psd_path}")
            return

        self.psd_layers = inspect_psd(str(self.psd_path))

        self._base_img = None
        self._last_img = None
        try:
            self._base_img = Image.open(BytesIO(render(str(self.psd_path)))).convert("RGBA")
        except Exception as e:
            messagebox.showerror("Render error", str(e))

        self.template_label.config(text=f"Loaded: {Path(path).name}")
        self._build_form()
        self._update_preview()

    def _build_form(self):
        for w in self.form_frame.winfo_children():
            w.destroy()
        self.form_vars  = {}
        self.image_vars = {}

        layer_index = {l["name"]: l for l in self.psd_layers}
        # Wrap loaded fields in Field class to enforce lock semantics
        fields = {
            name: Field(field_data) if not isinstance(field_data, Field) else field_data
            for name, field_data in self.template.get("fields", {}).items()
        }

        for layer_name, field in fields.items():
            if not field.get("editable", True):
                continue
            layer = layer_index.get(layer_name)
            if not layer:
                continue

            label = field.get("label", layer_name)
            box   = ttk.LabelFrame(self.form_frame, text=label, padding=8)
            box.pack(fill=tk.X, padx=5, pady=5)

            if layer["type"] == "TypeLayer":
                var = tk.StringVar(value=field.get("value", ""))
                var.trace_add("write", lambda *a: self._update_preview())
                self.form_vars[layer_name] = var
                ttk.Label(box, text="Text:").pack(anchor="w")
                ttk.Entry(box, textvariable=var, width=40).pack(fill=tk.X)
            else:
                var = tk.StringVar()
                self.image_vars[layer_name] = var
                ttk.Label(box, text="Image:").pack(anchor="w")
                row = ttk.Frame(box)
                row.pack(fill=tk.X)
                lbl = ttk.Label(row, text="No file selected", relief=tk.SUNKEN)
                lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

                def pick(ln=layer_name, v=var, l=lbl):
                    p = filedialog.askopenfilename(
                        filetypes=[("Images", "*.png *.jpg *.jpeg"), ("All files", "*")])
                    if p:
                        v.set(p)
                        l.config(text=Path(p).name)
                        self._update_preview()

                ttk.Button(row, text="Browse", command=pick).pack(side=tk.LEFT, padx=5)

    def _update_preview(self):
        if not self.psd_path or not self.template:
            return

        values       = {n: v.get() for n, v in self.form_vars.items()}
        image_values = {n: v.get() for n, v in self.image_vars.items() if v.get()}

        try:
            if values or image_values:
                raw = render(str(self.psd_path),
                             values=values or None,
                             image_values=image_values or None)
                img = Image.open(BytesIO(raw)).convert("RGBA")
            elif self._base_img:
                img = self._base_img.copy()
            else:
                return

            draw        = ImageDraw.Draw(img)
            layer_index = {l["name"]: l for l in self.psd_layers}
            for name in self.template.get("fields", {}):
                layer = layer_index.get(name)
                if not layer:
                    continue
                draw.rectangle(
                    [layer["left"],  layer["top"],
                     layer["left"] + layer["width"],
                     layer["top"]  + layer["height"]],
                    outline=(0, 255, 0), width=2)

            self._blit(img)
        except Exception as e:
            print(f"[preview error] {e}", file=sys.stderr)

    def _blit(self, img: Image.Image = None):
        if img is not None:
            self._last_img = img
        if self._last_img is None:
            return
        cw    = max(self.preview_canvas.winfo_width(),  100)
        ch    = max(self.preview_canvas.winfo_height(), 100)
        frame = self._last_img.copy()
        frame.thumbnail((cw, ch), Image.LANCZOS)
        self.preview_tk = ImageTk.PhotoImage(frame)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(cw // 2, ch // 2,
                                          image=self.preview_tk, anchor="center")

    def _blit_cached(self):
        if self._last_img is not None:
            self._blit()

    def export_png(self):
        if not self.psd_path or not self.template:
            messagebox.showwarning("No Template", "Load a template first")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("All files", "*")])
        if not path:
            return

        try:
            values       = {n: v.get() for n, v in self.form_vars.items()}
            image_values = {n: v.get() for n, v in self.image_vars.items() if v.get()}
            Path(path).write_bytes(render(
                str(self.psd_path),
                values=values or None,
                image_values=image_values or None,
            ))
            messagebox.showinfo("Exported", f"PNG saved to {path}")
        except Exception as e:
            messagebox.showerror("Export Error", str(e))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ModeSelector().mainloop()

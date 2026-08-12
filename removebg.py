#!/usr/bin/env python3
"""
BG Remover Pro
==============
A simple, elegant Tkinter desktop app that removes image backgrounds using
the remove.bg API (https://www.remove.bg/api) — full resolution, no
subscription/login wall like the web app imposes on free users (resolution
is whatever your remove.bg API plan allows; the free API tier already gives
much higher resolution than the free *website* preview).

Requirements:
    pip install requests Pillow

Run:
    python bg_remover_pro.py

On first run you'll be asked for your remove.bg API key (get one free at
https://www.remove.bg/api -> Get API Key). It's saved locally so you only
enter it once.

Window / app icon:
    Drop a file named "imageicon.ico" and/or "imageicon.png" in the same
    folder as this script and it's used automatically as the window/taskbar
    icon (see _load_app_icon()). Either file alone is enough; .ico is used
    first when running on Windows.

Packaging as a Windows .exe (PyInstaller):
    pip install pyinstaller
    pyinstaller --onefile --noconsole --icon=imageicon.ico ^
        --add-data "imageicon.ico;." --add-data "imageicon.png;." ^
        bg_remover_pro.py

    --icon sets the .exe file's own icon (what Explorer/taskbar/desktop
    shortcuts show for the exe itself) — this needs imageicon.ico.
    --add-data bundles a copy of both icon files inside the exe so the
    *running app* can also load one as its window icon at startup; without
    it, _load_app_icon() would have nothing to find once packaged.
"""

import collections
import io
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

try:
    import requests
except ImportError:
    print("Missing dependency 'requests'. Install it with:\n    pip install requests")
    sys.exit(1)

try:
    from PIL import Image, ImageTk, ImageOps, ImageDraw
    try:
        from PIL import ImageGrab
        HAS_IMAGEGRAB = True
    except ImportError:
        HAS_IMAGEGRAB = False
except ImportError:
    print("Missing dependency 'Pillow'. Install it with:\n    pip install Pillow")
    sys.exit(1)


# --------------------------------------------------------------------------
# Config (remembers API key + last-used save directory across sessions)
# --------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".bg_remover_pro" / "config.json"
API_URL = "https://api.remove.bg/v1.0/removebg"

DEFAULT_CONFIG = {"api_key": "", "last_dir": str(Path.home())}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass  # non-fatal — app still works, just won't remember settings


# --------------------------------------------------------------------------
# Visual theme
# --------------------------------------------------------------------------

BG = "#1a1a24"
PANEL = "#22222e"
PANEL_BORDER = "#34344a"
ACCENT = "#22c55e"        # green — "go" action (Remove BG)
ACCENT_HOVER = "#16a34a"
SAVE_COLOR = "#3b82f6"    # blue — Save
SAVE_HOVER = "#2563eb"
ERASE_COLOR = "#f97316"   # orange — Magic Erase
ERASE_HOVER = "#ea580c"
RESTORE_COLOR = "#a855f7" # purple — Restore
RESTORE_HOVER = "#9333ea"
DANGER = "#ef4444"
TEXT = "#f5f5f7"
SUBTEXT = "#a1a1ae"
FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"

F_TITLE = (FONT_FAMILY, 18, "bold")
F_LABEL = (FONT_FAMILY, 11, "bold")
F_BUTTON = (FONT_FAMILY, 12, "bold")
F_BUTTON_SM = (FONT_FAMILY, 10, "bold")
F_STATUS = (FONT_FAMILY, 10, "bold")


class BoldButton(tk.Button):
    """A flat, bold, high-contrast button with hover feedback."""

    def __init__(self, master, text, command, bg=ACCENT, hover=ACCENT_HOVER,
                 fg=TEXT, font=F_BUTTON, **kwargs):
        super().__init__(
            master, text=text, command=command, bg=bg, fg=fg, font=font,
            activebackground=hover, activeforeground=fg, relief="flat",
            bd=0, padx=18, pady=10, cursor="hand2",
            disabledforeground="#6b6b78", **kwargs,
        )
        self._bg = bg
        self._hover = hover
        self._active = False
        self.bind("<Enter>", lambda e: self.config(bg=self._hover) if self["state"] != "disabled" else None)
        self.bind("<Leave>", lambda e: self.config(bg=self._bg if not self._active else self._hover)
                   if self["state"] != "disabled" else None)

    def set_active(self, active: bool):
        """Visually mark this as the selected tool (stays highlighted)."""
        self._active = active
        if self["state"] != "disabled":
            self.config(bg=self._hover if active else self._bg)

    def set_enabled(self, enabled: bool):
        """Enable/disable AND visibly dim the button, so it's obvious when a
        control isn't interactive yet (e.g. retouch tools before a removal)."""
        if enabled:
            self.config(state="normal", bg=self._hover if self._active else self._bg,
                        fg=TEXT, cursor="hand2")
        else:
            self.config(state="disabled", bg="#26262f", fg="#5a5a66", cursor="arrow")


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class BGRemoverApp(tk.Tk):
    CANVAS_W, CANVAS_H = 460, 460  # initial size hint only — canvases resize with the window

    def __init__(self):
        super().__init__()
        self.cfg = load_config()

        self.title("BG Remover Pro  (build 2026-08-12j)")
        self.configure(bg=BG)
        self.minsize(1080, 780)  # wide enough that the single-row retouch toolbar always fits
        self._icon_photo = None  # keep a reference so Tk doesn't garbage-collect it
        self._icon_ico_path = None
        self._icon_png_path = None
        self._load_app_icon()

        # ---- state ----
        self.original_path = None
        self.original_image = None      # full-res PIL image (as loaded)
        self.original_aligned = None    # original resized to match result dims, for Restore
        self.result_image = None        # pristine full-res PIL image, RGBA, bg removed
        self.edited_image = None        # working copy — magic erase/restore paint onto this
        self.preview_original_tk = None
        self.preview_result_tk = None
        self.matte_color = None         # None = transparent, else (r,g,b)
        self.processing = False

        # retouch tool state
        self.tool_mode = "none"         # "none" | "erase" | "restore"
        self.brush_var = tk.IntVar(value=18)
        self._last_paint_pt = None
        self.undo_stack = collections.deque(maxlen=20)  # one entry per retouch action
        self._brush_cursor_id = None
        self._result_disp = None        # (scale, offx, offy) mapping canvas<->image coords
        self._checker_cache_key = None  # cached transparency checkerboard (perf)
        self._checker_cache_img = None
        self._preview_refresh_job = None  # throttles preview redraws while dragging a brush stroke

        # scan animation state
        self._scan_active = False
        self._scan_anim_id = None
        self._scan_line = None
        self._scan_glow = None
        self._scan_y = 0

        self._build_ui()
        self._center_window()
        self.bind_all("<Control-z>", self._undo)
        self.bind_all("<Control-Z>", self._undo)
        self.bind_all("<Command-z>", self._undo)  # macOS

        if not self.cfg.get("api_key"):
            self.after(300, self._prompt_for_api_key)

    # ---------------------------------------------------------------- UI --

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=24, pady=(20, 10))

        tk.Label(header, text="BG Remover Pro", font=F_TITLE, bg=BG, fg=TEXT).pack(side="left")

        BoldButton(header, "⚙ API Key", self._prompt_for_api_key,
                   bg=PANEL, hover="#3a3a4e", font=F_BUTTON_SM
                   ).pack(side="right", padx=(8, 0))

        # Toolbar
        toolbar = tk.Frame(self, bg=BG)
        toolbar.pack(fill="x", padx=24, pady=(0, 14))

        BoldButton(toolbar, "📂 Open Image", self.open_image, bg=PANEL, hover="#3a3a4e"
                   ).pack(side="left", padx=(0, 8))
        if HAS_IMAGEGRAB:
            BoldButton(toolbar, "📋 Paste from Clipboard", self.paste_from_clipboard,
                       bg=PANEL, hover="#3a3a4e").pack(side="left", padx=(0, 8))

        self.remove_btn = BoldButton(toolbar, "✂ Remove Background", self.remove_background,
                                      bg=ACCENT, hover=ACCENT_HOVER)
        self.remove_btn.pack(side="left", padx=(0, 8))
        self.remove_btn.set_enabled(False)

        self.reset_btn = BoldButton(toolbar, "↺ Reset", self.reset_all, bg=PANEL, hover="#3a3a4e")
        self.reset_btn.pack(side="left")
        self.reset_btn.set_enabled(False)

        # Image panels — these stretch with the window (including on Maximize),
        # and the previews inside redraw at the new size (see _make_panel).
        panels = tk.Frame(self, bg=BG)
        panels.pack(fill="both", expand=True, padx=24)
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        panels.rowconfigure(0, weight=1)

        self.orig_frame, self.orig_canvas = self._make_panel(panels, "Original", "orig")
        self.orig_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        self.result_frame, self.result_canvas = self._make_panel(panels, "Background Removed", "result")
        self.result_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        # Retouch tool bar (Magic Erase / Restore) — mimics remove.bg's manual edit brush.
        # Single row; the window's minsize is wide enough to always fit it (see minsize below).
        retouch_bar = tk.Frame(self, bg=BG)
        retouch_bar.pack(fill="x", padx=24, pady=(10, 0))

        tk.Label(retouch_bar, text="Retouch:", font=F_LABEL, bg=BG, fg=SUBTEXT).pack(side="left")

        self.erase_btn = BoldButton(retouch_bar, "🪄 Magic Erase", lambda: self._set_tool_mode("erase"),
                                     bg=PANEL, hover=ERASE_HOVER, font=F_BUTTON_SM)
        self.erase_btn.pack(side="left", padx=(10, 6))
        self.erase_btn.set_enabled(False)

        self.restore_btn = BoldButton(retouch_bar, "🩹 Restore", lambda: self._set_tool_mode("restore"),
                                       bg=PANEL, hover=RESTORE_HOVER, font=F_BUTTON_SM)
        self.restore_btn.pack(side="left", padx=(0, 14))
        self.restore_btn.set_enabled(False)

        tk.Label(retouch_bar, text="Brush size", font=F_BUTTON_SM, bg=BG, fg=SUBTEXT).pack(side="left")
        self.brush_scale = tk.Scale(retouch_bar, from_=4, to=60, orient="horizontal",
                                     variable=self.brush_var, length=110, bg=BG, fg=TEXT,
                                     troughcolor=PANEL, highlightthickness=0, bd=0,
                                     font=(FONT_FAMILY, 8), showvalue=True,
                                     activebackground=ACCENT)
        self.brush_scale.pack(side="left", padx=(6, 14))
        self.brush_scale.config(state="disabled")

        self.undo_btn = BoldButton(retouch_bar, "↩ Undo (Ctrl+Z)", self._undo,
                                    bg=PANEL, hover="#3a3a4e", font=F_BUTTON_SM)
        self.undo_btn.pack(side="left", padx=(0, 8))
        self.undo_btn.set_enabled(False)

        self.retouch_reset_btn = BoldButton(retouch_bar, "Reset Retouching", self._reset_retouch,
                                             bg=PANEL, hover="#3a3a4e", font=F_BUTTON_SM)
        self.retouch_reset_btn.pack(side="left")
        self.retouch_reset_btn.set_enabled(False)

        # Background matte options (for the result panel)
        matte_bar = tk.Frame(self, bg=BG)
        matte_bar.pack(fill="x", padx=24, pady=(10, 0))
        tk.Label(matte_bar, text="Result background:", font=F_LABEL, bg=BG, fg=SUBTEXT).pack(side="left")

        self.matte_var = tk.StringVar(value="transparent")
        for label, val in (("Transparent", "transparent"), ("White", "white"), ("Custom…", "custom")):
            tk.Radiobutton(
                matte_bar, text=label, value=val, variable=self.matte_var,
                command=self._on_matte_change, bg=BG, fg=TEXT, selectcolor=PANEL,
                activebackground=BG, activeforeground=TEXT, font=F_BUTTON_SM,
                indicatoron=True, cursor="hand2",
            ).pack(side="left", padx=(10, 0))

        self.custom_swatch = tk.Frame(matte_bar, width=22, height=22, bg=PANEL, highlightthickness=1,
                                       highlightbackground=PANEL_BORDER)
        self.custom_swatch.pack(side="left", padx=(6, 0))

        # Footer: status + progress + save
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=24, pady=18)

        self.status_var = tk.StringVar(value="Open an image to get started.")
        self.status_label = tk.Label(footer, textvariable=self.status_var, font=F_STATUS,
                                      bg=BG, fg=SUBTEXT, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Slim.Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT,
                         bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=160,
                                         style="Slim.Horizontal.TProgressbar")

        self.save_btn = BoldButton(footer, "💾 Save As…", self.save_image, bg=SAVE_COLOR, hover=SAVE_HOVER)
        self.save_btn.pack(side="right")
        self.save_btn.set_enabled(False)

    def _make_panel(self, parent, title, which):
        frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=PANEL_BORDER)
        tk.Label(frame, text=title, font=F_LABEL, bg=PANEL, fg=TEXT).pack(pady=(12, 6))
        canvas = tk.Canvas(frame, width=self.CANVAS_W, height=self.CANVAS_H, bg="#151520",
                            highlightthickness=0)
        canvas.pack(padx=14, pady=(0, 14), fill="both", expand=True)
        self._draw_placeholder(canvas, "No image yet")
        # Redraw whatever this panel is showing whenever it's resized —
        # including when the window is maximized — so the preview scales
        # up/down with the available space instead of staying pinned small.
        canvas.bind("<Configure>", lambda e, w=which: self._on_panel_resize(w))
        return frame, canvas

    def _on_panel_resize(self, which):
        job_attr = "_orig_resize_job" if which == "orig" else "_result_resize_job"
        existing = getattr(self, job_attr, None)
        if existing is not None:
            try:
                self.after_cancel(existing)
            except Exception:
                pass
        setattr(self, job_attr, self.after(80, lambda: self._redraw_panel(which)))

    def _redraw_panel(self, which):
        if which == "orig":
            self._orig_resize_job = None
            if self.original_image is not None:
                self._show_preview(self.orig_canvas, self.original_image, "orig")
            else:
                self._draw_placeholder(self.orig_canvas, "No image yet")
        else:
            self._result_resize_job = None
            if self.edited_image is not None:
                self._refresh_result_preview()
            else:
                placeholder_text = "Not processed yet" if self.original_image is not None else "No image yet"
                self._draw_placeholder(self.result_canvas, placeholder_text)

    def _draw_placeholder(self, canvas, text):
        canvas.delete("all")
        cw = max(canvas.winfo_width(), 50)
        ch = max(canvas.winfo_height(), 50)
        canvas.create_rectangle(0, 0, cw, ch, fill="#151520", outline="")
        canvas.create_text(cw // 2, ch // 2, text=text, fill=SUBTEXT, font=F_LABEL)

    def _center_window(self, w=None, h=None):
        """Size the window to fit everything at launch (so nothing is ever
        hidden on first open), then center it. Pass explicit w/h to force a
        size; otherwise this uses the UI's actual natural size. The window
        stays fully resizable/maximizable, and the image previews scale up
        proportionally to fill any extra space (see _make_panel)."""
        self.update_idletasks()
        req_w = self.winfo_reqwidth() + 20   # small safety margin
        req_h = self.winfo_reqheight() + 20
        w = w or req_w
        h = h or req_h
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # never request a window bigger than the screen (leave a little margin)
        w = min(w, sw - 40)
        h = min(h, sh - 80)
        x, y = (sw - w) // 2, (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _load_app_icon(self):
        """Look for imageicon.ico / imageicon.png next to this script (or,
        once packaged with PyInstaller, inside the bundled data — see
        sys._MEIPASS) and use it as the window/taskbar icon for the main
        window AND every popup (API Key dialog, etc. — see _apply_window_icon)."""
        if getattr(sys, "frozen", False):
            base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        else:
            try:
                base_dir = Path(__file__).resolve().parent
            except NameError:
                base_dir = Path(sys.argv[0]).resolve().parent

        ico_path = base_dir / "imageicon.ico"
        png_path = base_dir / "imageicon.png"
        self._icon_ico_path = ico_path if ico_path.exists() else None
        self._icon_png_path = png_path if png_path.exists() else None

        # Printed to the console so you can confirm exactly where the app is
        # looking and whether it actually found your file, e.g. if the app
        # was launched from a different folder than the script itself.
        print(f"[BG Remover Pro] Looking for a custom icon in: {base_dir}")
        print(f"[BG Remover Pro]   imageicon.ico found: {ico_path.exists()}")
        print(f"[BG Remover Pro]   imageicon.png found: {png_path.exists()}")

        loaded = False
        if sys.platform.startswith("win") and self._icon_ico_path:
            try:
                # default=... (not just bitmap=...) is what makes Windows
                # apply this icon to every future Toplevel too (dialogs like
                # the API Key popup), not just the main window.
                self.iconbitmap(default=str(self._icon_ico_path))
                loaded = True
            except Exception as e:
                print(f"[BG Remover Pro]   iconbitmap(imageicon.ico) failed: {e}")

        if not loaded:
            icon_file = self._icon_png_path or self._icon_ico_path
            try:
                img = Image.open(icon_file)
                self._icon_photo = ImageTk.PhotoImage(img)
                # default=True here (the `True` first arg) is the
                # cross-platform equivalent — applies to future Toplevels too.
                self.iconphoto(True, self._icon_photo)
                loaded = True
            except Exception as e:
                print(f"[BG Remover Pro]   iconphoto({icon_file.name}) failed: {e}")
        elif self._icon_png_path or self._icon_ico_path:
            # ICO already won on the main window — still prep a PhotoImage
            # as a fallback for _apply_window_icon, without re-setting the
            # main window's icon (that's what caused the ICO/PNG mismatch).
            try:
                icon_file = self._icon_png_path or self._icon_ico_path
                self._icon_photo = ImageTk.PhotoImage(Image.open(icon_file))
            except Exception:
                pass

        if not loaded:
            print("[BG Remover Pro]   No custom icon loaded — place imageicon.ico and/or "
                  "imageicon.png in the same folder as this script, next to it exactly "
                  "(not a subfolder), then restart the app.")

    def _apply_window_icon(self, window):
        """Explicitly re-apply the loaded icon to a specific Toplevel
        (dialogs). Belt-and-suspenders on top of the default= propagation
        above, since that isn't 100% reliable across every Tk build/platform."""
        if self._icon_ico_path and sys.platform.startswith("win"):
            try:
                window.iconbitmap(str(self._icon_ico_path))
                return
            except Exception:
                pass
        if self._icon_photo is not None:
            try:
                window.iconphoto(False, self._icon_photo)
            except Exception:
                pass

    # ------------------------------------------------------------- API key --

    def _prompt_for_api_key(self):
        dlg = tk.Toplevel(self, bg=BG)
        dlg.title("remove.bg API Key")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        self._apply_window_icon(dlg)  # explicit — don't rely solely on default= propagation

        tk.Label(dlg, text="Enter your remove.bg API key", font=F_LABEL, bg=BG, fg=TEXT
                 ).pack(padx=24, pady=(22, 4))
        tk.Label(dlg, text="Free keys are available at remove.bg/api",
                 font=(FONT_FAMILY, 9), bg=BG, fg=SUBTEXT).pack(pady=(0, 12))

        entry_var = tk.StringVar(value=self.cfg.get("api_key", ""))
        entry = tk.Entry(dlg, textvariable=entry_var, show="•", font=(FONT_FAMILY, 12),
                          width=36, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat")
        entry.pack(padx=24, ipady=6)
        entry.focus_set()

        show_var = tk.BooleanVar(value=False)
        tk.Checkbutton(dlg, text="Show key", variable=show_var, bg=BG, fg=SUBTEXT,
                        selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
                        font=(FONT_FAMILY, 9),
                        command=lambda: entry.config(show="" if show_var.get() else "•")
                        ).pack(anchor="w", padx=24, pady=(6, 0))

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(pady=18)

        def get_key_page():
            webbrowser.open("https://www.remove.bg/api")

        def do_save():
            key = entry_var.get().strip()
            if not key:
                messagebox.showwarning("API Key Required", "Please enter a valid API key.", parent=dlg)
                return
            self.cfg["api_key"] = key
            save_config(self.cfg)
            dlg.destroy()

        BoldButton(btn_row, "Get a free key ↗", get_key_page, bg=PANEL, hover="#3a3a4e",
                   font=F_BUTTON_SM).pack(side="left", padx=6)
        BoldButton(btn_row, "Save", do_save, bg=ACCENT, hover=ACCENT_HOVER,
                   font=F_BUTTON_SM).pack(side="left", padx=6)

        dlg.bind("<Return>", lambda e: do_save())
        dlg.update_idletasks()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    # ---------------------------------------------------------- image load --

    def open_image(self):
        path = filedialog.askopenfilename(
            title="Choose an image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.tiff"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img = Image.open(path)
            img.load()
        except Exception as e:
            messagebox.showerror("Couldn't open image", str(e))
            return
        self._load_new_image(img, path)
        self.status_var.set(f"Loaded: {os.path.basename(path)}  ({img.width}×{img.height}px)")

    def paste_from_clipboard(self):
        try:
            img = ImageGrab.grabclipboard()
        except Exception as e:
            messagebox.showerror("Clipboard error", str(e))
            return
        if img is None or not isinstance(img, Image.Image):
            messagebox.showinfo("Nothing to paste", "No image found on the clipboard. Copy an image first.")
            return
        self._load_new_image(img, None)
        self.status_var.set(f"Pasted image from clipboard  ({img.width}×{img.height}px)")

    def _load_new_image(self, img, path):
        self.original_path = path
        self.original_image = img
        self.result_image = None
        self.edited_image = None
        self.original_aligned = None
        self.undo_stack.clear()
        self._set_tool_mode("none")
        self._show_preview(self.orig_canvas, img, "orig")
        self._draw_placeholder(self.result_canvas, "Not processed yet")
        self.remove_btn.set_enabled(True)
        self.reset_btn.set_enabled(True)
        self.save_btn.set_enabled(False)
        self._set_retouch_controls_enabled(False)

    def reset_all(self):
        self._stop_scan_animation()
        self.original_path = None
        self.original_image = None
        self.original_aligned = None
        self.result_image = None
        self.edited_image = None
        self.undo_stack.clear()
        self._set_tool_mode("none")
        self._draw_placeholder(self.orig_canvas, "No image yet")
        self._draw_placeholder(self.result_canvas, "No image yet")
        self.remove_btn.set_enabled(False)
        self.reset_btn.set_enabled(False)
        self.save_btn.set_enabled(False)
        self._set_retouch_controls_enabled(False)
        self.status_var.set("Open an image to get started.")

    # --------------------------------------------------------- preview -----

    def _show_preview(self, canvas, pil_img, which):
        cw = max(canvas.winfo_width(), 50)
        ch = max(canvas.winfo_height(), 50)

        display_img = pil_img
        if which == "result" and pil_img.mode == "RGBA" and self.matte_color:
            display_img = self._apply_matte(pil_img, self.matte_color)

        rgba = display_img.convert("RGBA")
        fitted = ImageOps.contain(rgba, (cw, ch))

        if which == "result" and not self.matte_color:
            # .copy() is essential — pasting onto the cached checkerboard
            # itself would permanently bake in every past frame, since a
            # masked paste never overwrites the now-transparent (erased)
            # pixels. That was the exact cause of erased spots not visually
            # clearing until something (like a resize) rebuilt the cache.
            board = self._checkerboard(fitted.size).copy()
            board.paste(fitted, (0, 0), fitted)
            fitted_display = board
        else:
            fitted_display = fitted.convert("RGB")

        tk_img = ImageTk.PhotoImage(fitted_display)
        canvas.delete("all")  # clears the placeholder rect/text too, not just old previews
        if which == "result":
            self._brush_cursor_id = None  # the old canvas item is gone with delete("all")
        offx = (cw - fitted.width) // 2
        offy = (ch - fitted.height) // 2
        canvas.create_image(cw // 2, ch // 2, image=tk_img, tags=("preview_img",))

        if which == "orig":
            self.preview_original_tk = tk_img
        else:
            self.preview_result_tk = tk_img
            scale = fitted.width / display_img.width if display_img.width else 1.0
            self._result_disp = (scale, offx, offy)

    def _checkerboard(self, size, tile=10):
        # Cached + built from filled rectangles instead of per-pixel writes —
        # the old per-pixel version was the main cause of Magic Erase/Restore
        # lag on a large (e.g. maximized) canvas, since it reran on every
        # single mouse-move while painting.
        if self._checker_cache_key == size and self._checker_cache_img is not None:
            return self._checker_cache_img
        w, h = size
        board = Image.new("RGB", (w, h), "#eeeeee")
        draw = ImageDraw.Draw(board)
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                if ((x // tile) + (y // tile)) % 2 == 0:
                    draw.rectangle([x, y, x + tile, y + tile], fill="#dddddd")
        self._checker_cache_key = size
        self._checker_cache_img = board.convert("RGBA")
        return self._checker_cache_img

    @staticmethod
    def _apply_matte(rgba_img, color):
        bg = Image.new("RGB", rgba_img.size, color)
        bg.paste(rgba_img, (0, 0), rgba_img)
        return bg

    def _refresh_result_preview(self):
        if self.edited_image is not None:
            self._show_preview(self.result_canvas, self.edited_image, "result")

    # ---------------------------------------------------------- matte UI --

    def _on_matte_change(self):
        choice = self.matte_var.get()
        if choice == "transparent":
            self.matte_color = None
        elif choice == "white":
            self.matte_color = (255, 255, 255)
        elif choice == "custom":
            rgb, _ = colorchooser.askcolor(title="Choose background color")
            if rgb is None:
                self.matte_var.set("transparent" if self.matte_color is None else
                                    ("white" if self.matte_color == (255, 255, 255) else "custom"))
                return
            self.matte_color = tuple(int(c) for c in rgb)
            hex_color = "#%02x%02x%02x" % self.matte_color
            self.custom_swatch.config(bg=hex_color)
        self._refresh_result_preview()

    # ------------------------------------------------------ remove.bg call --

    def remove_background(self):
        if self.processing or self.original_image is None:
            return
        api_key = self.cfg.get("api_key", "").strip()
        if not api_key:
            self._prompt_for_api_key()
            return

        self.processing = True
        self.remove_btn.set_enabled(False)
        self.save_btn.set_enabled(False)
        self._set_retouch_controls_enabled(False)
        self.status_var.set("Removing background… this can take a few seconds.")
        self.progress.pack(side="right", padx=16)
        self.progress.start(12)
        self._start_scan_animation()

        threading.Thread(target=self._call_remove_bg, args=(api_key,), daemon=True).start()

    def _call_remove_bg(self, api_key):
        try:
            buf = io.BytesIO()
            img_to_send = self.original_image
            fmt = "PNG"
            img_to_send.convert("RGBA" if img_to_send.mode in ("RGBA", "LA") else "RGB").save(buf, format=fmt)
            buf.seek(0)

            response = requests.post(
                API_URL,
                files={"image_file": ("image.png", buf, "image/png")},
                data={"size": "auto"},  # highest resolution your API plan allows
                headers={"X-Api-Key": api_key},
                timeout=90,
            )

            if response.status_code == requests.codes.ok:
                result = Image.open(io.BytesIO(response.content)).convert("RGBA")
                self.after(0, self._on_remove_success, result)
            else:
                message = self._parse_error(response)
                self.after(0, self._on_remove_error, message)
        except requests.exceptions.RequestException as e:
            self.after(0, self._on_remove_error, f"Network error: {e}")
        except Exception as e:
            self.after(0, self._on_remove_error, str(e))

    @staticmethod
    def _parse_error(response):
        try:
            data = response.json()
            errors = data.get("errors", [])
            if errors:
                return errors[0].get("title", "Unknown error")
        except Exception:
            pass
        return f"HTTP {response.status_code}: {response.text[:200]}"

    def _on_remove_success(self, result_img):
        self._stop_scan_animation()
        self.progress.stop()
        self.progress.pack_forget()
        self.result_image = result_img
        self.edited_image = result_img.copy()
        self.undo_stack.clear()

        # Align a copy of the original (pre-removal) photo to the result's
        # dimensions so the Restore brush can paint back real original pixels.
        src = self.original_image.convert("RGB")
        if src.size != result_img.size:
            src = src.resize(result_img.size, Image.LANCZOS)
        self.original_aligned = src

        self._refresh_result_preview()
        self.save_btn.set_enabled(True)
        self.remove_btn.set_enabled(True)
        self._set_retouch_controls_enabled(True)
        self.status_var.set(
            f"Done!  Result: {result_img.width}×{result_img.height}px. "
            f"Use Magic Erase / Restore to touch up, pick a background, then save."
        )
        self.processing = False

    def _on_remove_error(self, message):
        self._stop_scan_animation()
        self.progress.stop()
        self.progress.pack_forget()
        self.remove_btn.set_enabled(True)
        self.status_var.set("Something went wrong — see the popup for details.")
        self.processing = False
        messagebox.showerror("remove.bg error", message)

    # ------------------------------------------------------ scan animation --
    # A lightweight sweeping "detecting edges…" scan-line effect over the
    # Original panel while remove.bg processes the image (purely cosmetic —
    # the API call itself gives no real progress data, so this mirrors the
    # feel of the web app's scanning animation rather than tracking it exactly).

    def _start_scan_animation(self):
        self._scan_active = True
        self._scan_y = 0
        self.orig_canvas.delete("scan")
        cw = max(self.orig_canvas.winfo_width(), 50)
        self._scan_glow = self.orig_canvas.create_line(
            0, 0, cw, 0, fill=ACCENT, width=14, stipple="gray25", tags=("scan",)
        )
        self._scan_line = self.orig_canvas.create_line(
            0, 0, cw, 0, fill=ACCENT, width=2, tags=("scan",)
        )
        self._animate_scan()

    def _animate_scan(self):
        if not self._scan_active:
            return
        cw = max(self.orig_canvas.winfo_width(), 50)
        ch = max(self.orig_canvas.winfo_height(), 50)
        self._scan_y = (self._scan_y + 5) % ch
        self.orig_canvas.coords(self._scan_glow, 0, self._scan_y, cw, self._scan_y)
        self.orig_canvas.coords(self._scan_line, 0, self._scan_y, cw, self._scan_y)
        self._scan_anim_id = self.after(25, self._animate_scan)

    def _stop_scan_animation(self):
        self._scan_active = False
        if self._scan_anim_id is not None:
            try:
                self.after_cancel(self._scan_anim_id)
            except Exception:
                pass
            self._scan_anim_id = None
        self.orig_canvas.delete("scan")

    # --------------------------------------------------- magic erase/restore --
    # Both tools paint on self.edited_image (a working copy of the API result).
    # Erase makes the brushed area transparent; Restore pastes back the
    # matching pixels from the original (pre-removal) photo.

    def _set_retouch_controls_enabled(self, enabled: bool):
        self.erase_btn.set_enabled(enabled)
        self.restore_btn.set_enabled(enabled)
        self.brush_scale.config(state="normal" if enabled else "disabled")
        self.retouch_reset_btn.set_enabled(enabled)
        self.undo_btn.set_enabled(enabled and bool(self.undo_stack))
        if not enabled:
            self._set_tool_mode("none")

    def _set_tool_mode(self, mode):
        if mode == self.tool_mode:
            mode = "none"  # clicking the active tool again turns it off
        self.tool_mode = mode
        self.erase_btn.set_active(mode == "erase")
        self.restore_btn.set_active(mode == "restore")
        cursor = "crosshair" if mode in ("erase", "restore") else "arrow"
        self.result_canvas.config(cursor=cursor)
        if mode == "none":
            self._clear_brush_cursor()

    def _reset_retouch(self):
        if self.result_image is None:
            return
        self._push_undo()  # so Undo can bring back your retouching after a reset, too
        self.edited_image = self.result_image.copy()
        self._refresh_result_preview()
        self.status_var.set("Retouching reset to the original removal result.")

    def _push_undo(self):
        """Snapshot the current edited_image so one Undo/Ctrl+Z press can
        revert exactly this one action (one brush stroke, or one reset)."""
        if self.edited_image is not None:
            self.undo_stack.append(self.edited_image.copy())
            self._update_undo_button()

    def _undo(self, event=None):
        if not self.undo_stack:
            return
        self.edited_image = self.undo_stack.pop()
        self._refresh_result_preview()
        self._update_undo_button()
        self.status_var.set("Undid the last retouch action.")

    def _update_undo_button(self):
        self.undo_btn.set_enabled(bool(self.undo_stack) and self.result_image is not None)

    def _canvas_to_image_xy(self, cx, cy):
        if not self._result_disp:
            return None
        scale, offx, offy = self._result_disp
        if scale <= 0:
            return None
        ix = (cx - offx) / scale
        iy = (cy - offy) / scale
        return ix, iy

    def _bind_result_canvas_events(self):
        self.result_canvas.bind("<ButtonPress-1>", self._on_paint_start)
        self.result_canvas.bind("<B1-Motion>", self._on_paint_drag)
        self.result_canvas.bind("<ButtonRelease-1>", self._on_paint_end)
        self.result_canvas.bind("<Motion>", self._on_canvas_motion)
        self.result_canvas.bind("<Leave>", lambda e: self._clear_brush_cursor())

    def _on_canvas_motion(self, event):
        self._update_brush_cursor(event.x, event.y)

    def _update_brush_cursor(self, cx, cy):
        if self.tool_mode not in ("erase", "restore"):
            self._clear_brush_cursor()
            return
        r = max(3, int(self.brush_var.get() / 2))
        color = ERASE_COLOR if self.tool_mode == "erase" else RESTORE_COLOR
        if self._brush_cursor_id is None:
            self._brush_cursor_id = self.result_canvas.create_oval(
                cx - r, cy - r, cx + r, cy + r, outline=color, width=2, tags=("brush_cursor",)
            )
        else:
            self.result_canvas.coords(self._brush_cursor_id, cx - r, cy - r, cx + r, cy + r)
            self.result_canvas.itemconfig(self._brush_cursor_id, outline=color)
        self.result_canvas.tag_raise("brush_cursor")

    def _clear_brush_cursor(self):
        if self._brush_cursor_id is not None:
            self.result_canvas.delete(self._brush_cursor_id)
            self._brush_cursor_id = None

    def _on_paint_start(self, event):
        if self.tool_mode not in ("erase", "restore") or self.edited_image is None:
            return
        pt = self._canvas_to_image_xy(event.x, event.y)
        if pt is None:
            return
        self._push_undo()  # snapshot before this stroke — one undo step per stroke
        self._last_paint_pt = pt
        self._paint_at(*pt)
        self._refresh_result_preview()
        self._update_brush_cursor(event.x, event.y)

    def _on_paint_drag(self, event):
        if self.tool_mode not in ("erase", "restore") or self.edited_image is None:
            return
        pt = self._canvas_to_image_xy(event.x, event.y)
        if pt is None:
            return
        if self._last_paint_pt is not None:
            self._paint_line(self._last_paint_pt, pt)
        else:
            self._paint_at(*pt)
        self._last_paint_pt = pt
        self._schedule_preview_refresh()  # throttled — see method for why
        self._update_brush_cursor(event.x, event.y)

    def _schedule_preview_refresh(self):
        """Rescaling the full-resolution edited image for the on-screen
        preview is the expensive part of a brush stroke, especially on a
        large/maximized canvas. Painting itself stays instant; this just
        caps how often the *visible* preview redraws while dragging, so
        the brush doesn't visibly lag behind the mouse."""
        if self._preview_refresh_job is not None:
            return  # a refresh is already queued — no need to stack more
        self._preview_refresh_job = self.after(40, self._do_preview_refresh)

    def _do_preview_refresh(self):
        self._preview_refresh_job = None
        self._refresh_result_preview()

    def _on_paint_end(self, event):
        self._last_paint_pt = None
        if self._preview_refresh_job is not None:
            try:
                self.after_cancel(self._preview_refresh_job)
            except Exception:
                pass
            self._preview_refresh_job = None
        self._refresh_result_preview()  # make sure the final stroke state is shown immediately

    def _paint_line(self, p0, p1):
        r_img = max(1, int(self.brush_var.get() / 2 / max(self._result_disp[0], 0.0001)))
        step = max(1, r_img // 2)
        x0, y0 = p0
        x1, y1 = p1
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        steps = max(1, int(dist / step))
        for i in range(steps + 1):
            t = i / steps
            self._paint_at(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)

    def _paint_at(self, img_x, img_y):
        if self.edited_image is None or not self._result_disp:
            return
        scale = self._result_disp[0]
        r = max(1, int(self.brush_var.get() / 2 / max(scale, 0.0001)))

        left, top = int(img_x - r), int(img_y - r)
        size = r * 2
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)

        if self.tool_mode == "erase":
            transparent = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            self.edited_image.paste(transparent, (left, top), mask)
        elif self.tool_mode == "restore" and self.original_aligned is not None:
            crop_box = (left, top, left + size, top + size)
            source_crop = self.original_aligned.crop(crop_box).convert("RGBA")
            self.edited_image.paste(source_crop, (left, top), mask)

    # -------------------------------------------------------------- save ---

    def save_image(self):
        if self.edited_image is None:
            return

        out_img = self.edited_image
        default_ext = ".png"
        filetypes = [("PNG (keeps transparency)", "*.png"), ("JPEG", "*.jpg *.jpeg")]

        if self.matte_color is not None:
            out_img = self._apply_matte(self.edited_image, self.matte_color)

        base_name = "image"
        if self.original_path:
            base_name = os.path.splitext(os.path.basename(self.original_path))[0]
        suggested = f"{base_name}_nobg{default_ext}"

        path = filedialog.asksaveasfilename(
            title="Save image as…",
            initialdir=self.cfg.get("last_dir", str(Path.home())),
            initialfile=suggested,
            defaultextension=default_ext,
            filetypes=filetypes,
        )
        if not path:
            return

        try:
            if path.lower().endswith((".jpg", ".jpeg")) and out_img.mode == "RGBA":
                out_img = out_img.convert("RGB")
            out_img.save(path)
        except Exception as e:
            messagebox.showerror("Couldn't save image", str(e))
            return

        self.cfg["last_dir"] = os.path.dirname(path)
        save_config(self.cfg)
        self.status_var.set(f"Saved to {path}")

        if messagebox.askyesno("Saved", f"Saved to:\n{path}\n\nOpen the containing folder?"):
            self._open_in_explorer(os.path.dirname(path))

    @staticmethod
    def _open_in_explorer(folder):
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:
            pass


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        # Declare DPI awareness *before* any Tk window is created. Without
        # this, Windows display scaling (125%/150%/etc, common on laptops
        # and high-res monitors) can throw off Tkinter's own geometry math,
        # which can visibly misplace or crop widgets near window edges.
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    app = BGRemoverApp()
    app._bind_result_canvas_events()
    app.mainloop()
"""KC Scout public live camera viewer. Requires Pillow and OpenCV."""
import json
import math
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
    import cv2
except (ImportError, OSError) as error:
    script = Path(__file__).resolve()
    environment = (script.parent if getattr(sys, "frozen", False) else script.parents[2]) / ".venv"
    interpreter = environment / "Scripts" / "pythonw.exe"
    launch_error = None
    if not getattr(sys, "frozen", False) and interpreter.is_file() and Path(sys.prefix).resolve() != environment.resolve():
        try:
            subprocess.Popen([str(interpreter), str(script), *sys.argv[1:]], cwd=str(script.parent))
        except OSError as problem:
            launch_error = str(problem)
        else:
            sys.exit(0)
    startup = tk.Tk()
    startup.withdraw()
    messagebox.showerror(
        "KC Scout - Missing Video Dependencies",
        "The selected Python installation cannot load Pillow or OpenCV.\n\n"
        f"Python: {sys.executable}\nError: {error}\n\n"
        "Install requirements_kc_scout.txt in this Python environment, or run "
        "the app with the workspace .venv interpreter."
        + (f"\n\nEnvironment launch failed: {launch_error}" if launch_error else ""),
        parent=startup)
    startup.destroy()
    sys.exit(1)

BASE_URL = "https://www.kcscout.net/"
REFRESH_SECONDS = 3
FOCUSED_RETRY_SECONDS = 3
STREAM_STALL_SECONDS = 20
CONNECTION_STALL_SECONDS = 45
CITY_CAMERAS = {
    "Shawnee": (
        "K035SBC-08",
    ),
    "Lenexa": (
        "K035SBC-04",
        "K035SBC-03", "K035SBC-03A", "K035SBIPC-05A", "K035SBC-02",
        "K035SBIPC-02A", "K035SBIPC-01",
    ),
    "Olathe": (
        "K035NBIPC-01", "K035NBC-01B", "K035NBC-01C", "K035SBIPC-77",
        "K035NBIPC-78",
    ),
    "Merriam": (
        "K035NBC-06", "K035SBC-07", "K035SBC-05", "K035NBC-04T",
        "K035NBC-05T", "K035SBC-13",
    ),
}


def request_bytes(url, data=None):
    headers = {"User-Agent": "KCScoutCameraViewer/1.0 (personal use)", "Referer": BASE_URL}
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read()


def load_camera_catalog():
    payload = json.loads(request_bytes(BASE_URL + "DataProvider.asmx/LoadEntities", b"{}"))
    entities = payload.get("d") or {}
    if isinstance(entities, str):
        entities = json.loads(entities)
    cameras = entities.get("Camera")
    if not isinstance(cameras, list):
        raise ValueError("KC Scout did not return a camera catalog.")
    by_id = {str(camera["ID"]): camera for camera in cameras if camera.get("ID")}
    grouped = {}
    for city, identifiers in CITY_CAMERAS.items():
        grouped[city] = sorted(
            [dict(by_id[identifier], city=city) for identifier in identifiers if identifier in by_id],
            key=lambda camera: camera.get("OnStreetName", ""))
    return grouped


def load_video_url(camera):
    video = urllib.parse.urlsplit(camera.get("VideoUrl") or "")
    parts = video.path.strip("/").split("/")
    if len(parts) < 2 or not video.hostname:
        raise ValueError("This camera does not publish a video stream.")
    stream = "/".join(parts[1:])
    response = json.loads(request_bytes(BASE_URL + "DataProvider.asmx/GetVideoParams",
                                       json.dumps({"file": stream}).encode("utf-8")))
    token = response.get("d")
    if not isinstance(token, str) or not token:
        raise ValueError("KC Scout did not issue a public video link.")
    return f"https://{video.hostname}/{parts[0]}/{stream}/playlist.m3u8?{token}"


def system_dark_theme():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


class CameraApp:
    def __init__(self, root):
        self.root = root
        self.results = queue.Queue()
        self.catalog = {city: [] for city in CITY_CAMERAS}
        self.selected = None
        self.generation = 0
        self.fetching_generation = None
        self.frames = queue.Queue(maxsize=1)
        self.stream_stop = threading.Event()
        self.playing = False
        self.last_frame_time = None
        self.connection_started = None
        self.grid_generation = 0
        self.grid_streams = {}
        self.grid_mode = True
        self.image = None
        self.photo = None
        self.next_refresh = None
        self.dark = None
        self.closed = False
        root.title("KC Scout - I-35: Olathe to Shawnee Mission Parkway")
        root.geometry("1180x760")
        root.minsize(960, 620)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.city = tk.StringVar(value="Shawnee")
        self.search = tk.StringVar()

        header = ttk.Frame(root, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="KC Scout: I-35 Corridor", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Button(header, text="KC Scout Website", command=lambda: webbrowser.open(BASE_URL)).pack(side="right")
        controls = ttk.Frame(root, padding=(12, 0, 12, 10))
        controls.pack(fill="x")
        ttk.Label(controls, text="City area:").pack(side="left")
        city_box = ttk.Combobox(controls, textvariable=self.city, values=list(CITY_CAMERAS),
                                state="readonly", width=14)
        city_box.pack(side="left", padx=(6, 16))
        city_box.bind("<<ComboboxSelected>>", lambda _event: self.filter_cameras())
        ttk.Label(controls, text="Road / location:").pack(side="left")
        ttk.Entry(controls, textvariable=self.search, width=22).pack(side="left", padx=6)
        self.search.trace_add("write", lambda *_args: self.filter_cameras())
        ttk.Button(controls, text="Reload Cameras", command=self.reload_catalog).pack(side="right")
        self.status = ttk.Label(root, text="Loading KC Scout camera locations...",
                                style="Muted.TLabel", padding=(12, 0, 12, 8), wraplength=900)
        self.status.pack(fill="x")

        body = ttk.Frame(root, padding=(12, 0, 12, 0))
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, width=310)
        self.sidebar = sidebar
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)
        self.count_label = ttk.Label(sidebar, text="", style="Muted.TLabel")
        self.count_label.pack(anchor="w", pady=(0, 6))
        self.tree = ttk.Treeview(sidebar, show="tree", selectmode="browse")
        scroll = ttk.Scrollbar(sidebar, command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.config(yscrollcommand=scroll.set)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_camera)
        viewer = ttk.Frame(body)
        self.viewer = viewer
        viewer.pack(side="left", fill="both", expand=True)
        self.title = ttk.Label(viewer, text="Choose a camera", font=("Segoe UI", 13, "bold"),
                               wraplength=600)
        self.title.pack(anchor="w")
        self.location = ttk.Label(viewer, text="", style="Muted.TLabel", wraplength=600)
        self.location.pack(anchor="w", pady=(4, 8))
        self.canvas = tk.Canvas(viewer, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.draw_image())
        actions = ttk.Frame(viewer, padding=(0, 10, 0, 8))
        actions.pack(fill="x")
        ttk.Button(actions, text="City Grid", command=self.show_city_grid).pack(side="left", padx=(0, 6))
        self.refresh_button = ttk.Button(actions, text="Reconnect", command=self.start_stream)
        self.refresh_button.pack(side="left")
        self.video_button = ttk.Button(actions, text="Open in Browser", command=self.open_video)
        self.video_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Stop Video", command=self.stop_stream).pack(side="left", padx=6)
        ttk.Button(actions, text="Map Location", command=self.open_map).pack(side="right")
        self.timer = ttk.Label(viewer, text="", style="Muted.TLabel", font=("Consolas", 10))
        self.timer.pack(anchor="w")
        self.gallery = ttk.Frame(body)
        self.gallery_count = ttk.Label(self.gallery, text="", style="Muted.TLabel")
        self.gallery_count.pack(anchor="w", pady=(0, 8))
        gallery_scroll = ttk.Scrollbar(self.gallery, orient="vertical")
        gallery_scroll.pack(side="right", fill="y")
        self.gallery_canvas = tk.Canvas(self.gallery, highlightthickness=0,
                                        yscrollcommand=gallery_scroll.set)
        self.gallery_canvas.pack(fill="both", expand=True)
        gallery_scroll.config(command=self.gallery_canvas.yview)
        self.tiles = ttk.Frame(self.gallery_canvas)
        self.tiles_window = self.gallery_canvas.create_window(0, 0, window=self.tiles, anchor="nw")
        self.tiles.bind("<Configure>", lambda _event: self.gallery_canvas.config(
            scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind("<Configure>", self.arrange_grid)
        self.gallery_canvas.bind("<MouseWheel>", self.scroll_grid)
        self.sidebar.pack_forget()
        self.viewer.pack_forget()
        self.gallery.pack(fill="both", expand=True)
        ttk.Label(root, text="I-35: Olathe to Shawnee Mission Pkwy (Merriam) | KC Scout / KDOT / MoDOT | Public live video",
                  style="Muted.TLabel", padding=12).pack(anchor="w")
        self.check_theme()
        self.poll_results()
        self.tick()
        self.reload_catalog()

    def reload_catalog(self):
        self.status.config(text="Loading KC Scout camera locations...", style="Muted.TLabel")
        def worker():
            try:
                self.results.put(("catalog", None, load_camera_catalog(), None))
            except Exception as error:
                self.results.put(("catalog", None, None, str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def filter_cameras(self):
        self.show_city_grid()

    def show_city_grid(self):
        self.stop_stream()
        self.stop_grid()
        self.selected = None
        self.grid_mode = True
        self.sidebar.pack_forget()
        self.viewer.pack_forget()
        self.gallery.pack(fill="both", expand=True)
        self.tree.delete(*self.tree.get_children())
        query = self.search.get().strip().casefold()
        cameras = [camera for camera in self.catalog[self.city.get()]
                   if query in camera.get("OnStreetName", "").casefold()]
        for camera in cameras:
            self.tree.insert("", "end", iid=str(camera["ID"]), text=camera.get("OnStreetName", camera["ID"]))
        self.count_label.config(text=f"{self.city.get()}: {len(cameras)} cameras")
        self.gallery_count.config(text=f"{self.city.get()}: {len(cameras)} I-35 cameras")
        self.status.config(text="Connecting city camera previews..." if cameras else "No matching cameras.",
                           style="Muted.TLabel")
        for camera in cameras:
            tile = ttk.Frame(self.tiles, padding=6)
            label = ttk.Label(tile, text=camera.get("OnStreetName", camera["ID"]),
                              font=("Segoe UI", 10, "bold"), wraplength=300)
            label.pack(anchor="w", pady=(0, 6))
            canvas = tk.Canvas(tile, width=320, height=180, highlightthickness=0,
                               background=self.colors["field"], cursor="hand2")
            canvas.pack(fill="x")
            status = ttk.Label(tile, text="Connecting...", style="Muted.TLabel")
            status.pack(anchor="w", pady=(4, 0))
            state = {"camera": camera, "tile": tile, "canvas": canvas, "label": label,
                     "status": status, "frames": queue.Queue(maxsize=1), "stop": threading.Event(),
                     "image": None, "photo": None, "retry": None, "last_frame": None,
                     "attempt": 0}
            self.grid_streams[camera["ID"]] = state
            canvas.bind("<Button-1>", lambda _event, chosen=camera: self.focus_camera(chosen))
            canvas.bind("<Configure>", lambda _event, current=state: self.draw_tile(current))
            canvas.bind("<MouseWheel>", self.scroll_grid)
            self.start_grid_stream(state)
        self.gallery_canvas.yview_moveto(0)
        self.arrange_grid()

    def scroll_grid(self, event):
        self.gallery_canvas.yview_scroll(-int(event.delta / 120), "units")

    def arrange_grid(self, _event=None):
        width = max(1, self.gallery_canvas.winfo_width())
        self.gallery_canvas.itemconfigure(self.tiles_window, width=width)
        columns = 3 if width >= 1100 else 2 if width >= 700 else 1
        for column in range(3):
            self.tiles.columnconfigure(column, weight=1 if column < columns else 0,
                                       uniform="camera_tiles" if column < columns else "")
        for index, state in enumerate(self.grid_streams.values()):
            state["tile"].grid(row=index // columns, column=index % columns, sticky="nsew", padx=4, pady=4)
            tile_width = max(180, width // columns - 28)
            state["canvas"].config(width=tile_width, height=round(tile_width * 9 / 16))
            state["label"].config(wraplength=tile_width)

    def stop_grid(self):
        self.grid_generation += 1
        for state in self.grid_streams.values():
            state["stop"].set()
            state["tile"].destroy()
        self.grid_streams.clear()

    def start_grid_stream(self, state):
        state["stop"].set()
        state["stop"] = threading.Event()
        state["frames"] = queue.Queue(maxsize=1)
        state["attempt"] += 1
        state["retry"] = None
        state["last_frame"] = None
        state["status"].config(text="Connecting...", style="Muted.TLabel")
        identity = (self.grid_generation, state["camera"]["ID"], state["attempt"])
        threading.Thread(target=self.stream_worker,
                         args=(dict(state["camera"]), identity, state["stop"], state["frames"], True),
                         daemon=True).start()

    def draw_tile(self, state):
        canvas = state["canvas"]
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if state["image"] is not None and width > 1 and height > 1:
            image = ImageOps.contain(state["image"], (width, height), Image.Resampling.BILINEAR)
            state["photo"] = ImageTk.PhotoImage(image, master=self.root)
            canvas.create_image(width / 2, height / 2, image=state["photo"])
        else:
            state["photo"] = None
            canvas.create_text(width / 2, height / 2,
                               text="Video unavailable" if state["retry"] is not None else "Connecting...",
                               fill=self.colors["muted"])

    def focus_camera(self, camera):
        self.stop_grid()
        self.grid_mode = False
        self.gallery.pack_forget()
        self.sidebar.pack(side="left", fill="y", padx=(0, 12))
        self.viewer.pack(side="left", fill="both", expand=True)
        self.selected = camera
        self.title.config(text=camera.get("OnStreetName", camera["ID"]))
        self.location.config(text=f"{camera['city']} area | Camera {camera['ID']} | {camera.get('Direction', '')}")
        self.start_stream()

    def select_camera(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        camera = next((camera for camera in self.catalog[self.city.get()]
                       if str(camera["ID"]) == selection[0]), None)
        if camera is None or (self.selected and self.selected["ID"] == camera["ID"]):
            return
        self.focus_camera(camera)

    def stop_stream(self):
        self.stream_stop.set()
        self.generation += 1
        self.playing = False
        self.fetching_generation = None
        self.next_refresh = None
        self.last_frame_time = None
        self.connection_started = None
        self.image = None
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        if not self.closed:
            self.draw_image("Video stopped")
            self.status.config(text="Video stopped.", style="Muted.TLabel")

    def start_stream(self):
        if not self.selected:
            return
        self.stop_stream()
        generation = self.generation
        camera = dict(self.selected)
        stop = threading.Event()
        self.stream_stop = stop
        self.fetching_generation = generation
        self.connection_started = time.monotonic()
        self.status.config(text="Connecting to live camera video...", style="Muted.TLabel")
        self.draw_image("Connecting to live video...")
        threading.Thread(target=self.stream_worker, args=(camera, generation, stop), daemon=True).start()

    def stream_worker(self, camera, generation, stop, frames=None, preview=False):
        capture = None
        output = self.frames if frames is None else frames
        try:
            url = load_video_url(camera)
            if stop.is_set():
                return
            capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                                       [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
                                        cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000])
            if not capture.isOpened():
                raise ValueError("Unable to open this camera's live stream.")
            fps = capture.get(cv2.CAP_PROP_FPS)
            interval = 1 / fps if math.isfinite(fps) and 1 <= fps <= 120 else 1 / 25
            due = time.monotonic()
            published = 0.0
            while not stop.is_set():
                success, frame = capture.read()
                if stop.is_set():
                    return
                if not success:
                    raise ValueError("Camera stream interrupted or the public link expired.")
                if not preview or time.monotonic() - published >= 0.2:
                    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    if preview:
                        image.thumbnail((480, 270), Image.Resampling.BILINEAR)
                    published = time.monotonic()
                    try:
                        output.put_nowait((generation, image))
                    except queue.Full:
                        try:
                            output.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            output.put_nowait((generation, image))
                        except queue.Full:
                            pass
                due += interval
                if stop.wait(max(0, due - time.monotonic())):
                    return
                if due < time.monotonic() - 1:
                    due = time.monotonic()
        except Exception as error:
            if not stop.is_set():
                self.results.put(("stream_error", generation, None, str(error)))
        finally:
            if capture is not None:
                capture.release()

    def poll_results(self):
        if self.closed:
            return
        while True:
            try:
                kind, generation, payload, error = self.results.get_nowait()
            except queue.Empty:
                break
            if kind == "catalog":
                if error:
                    self.status.config(text="Camera catalog unavailable: " + error, style="Error.TLabel")
                else:
                    self.catalog = payload
                    self.status.config(text="Camera locations loaded.", style="Muted.TLabel")
                    self.filter_cameras()
            elif kind == "video":
                self.video_button.config(state="normal")
                if generation != self.generation or self.selected is None:
                    continue
                if error:
                    self.status.config(text="Video unavailable: " + error, style="Error.TLabel")
                else:
                    player = Path(__file__).with_name("kc_scout_video_player.html").resolve().as_uri()
                    fragment = urllib.parse.urlencode({"stream": payload, "name": self.selected.get("OnStreetName", "KC Scout")})
                    webbrowser.open(player + "#" + fragment)
                    self.status.config(text="Video opened in browser.", style="Muted.TLabel")
            elif kind == "stream_error" and isinstance(generation, tuple):
                grid_generation, camera_id, attempt = generation
                state = self.grid_streams.get(camera_id)
                if state is not None and grid_generation == self.grid_generation and attempt == state["attempt"]:
                    state["stop"].set()
                    state["image"] = None
                    state["retry"] = time.monotonic() + REFRESH_SECONDS
                    state["status"].config(text="Video unavailable; reconnecting...", style="Error.TLabel")
                    self.draw_tile(state)
            elif kind == "stream_error" and generation == self.generation:
                self.stream_stop.set()
                self.fetching_generation = None
                self.connection_started = None
                self.playing = False
                self.image = None
                self.draw_image("Video unavailable - reconnecting...")
                self.status.config(text="Live video unavailable: " + error, style="Error.TLabel")
                self.next_refresh = time.monotonic() + FOCUSED_RETRY_SECONDS
        try:
            generation, image = self.frames.get_nowait()
        except queue.Empty:
            pass
        else:
            if generation == self.generation and self.next_refresh is None and self.selected is not None:
                self.image = image
                self.last_frame_time = time.monotonic()
                self.connection_started = None
                self.fetching_generation = None
                if not self.playing:
                    self.status.config(text="Live camera video connected. Check the camera timestamp for source delay.",
                                       style="Muted.TLabel")
                self.playing = True
                self.draw_image()
        for state in self.grid_streams.values():
            try:
                identity, image = state["frames"].get_nowait()
            except queue.Empty:
                continue
            expected = (self.grid_generation, state["camera"]["ID"], state["attempt"])
            if identity == expected and state["retry"] is None:
                state["image"] = image
                state["last_frame"] = time.monotonic()
                state["status"].config(text="Live video", style="Muted.TLabel")
                self.draw_tile(state)
        self.root.after(33, self.poll_results)

    def draw_image(self, message="Choose a camera" ):
        self.canvas.delete("all")
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self.image is None:
            self.canvas.create_text(width / 2, height / 2, text=message, fill=self.colors["muted"])
            self.photo = None
        elif width > 1 and height > 1:
            resized = ImageOps.contain(self.image, (width, height), Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(resized, master=self.root)
            self.canvas.create_image(width / 2, height / 2, image=self.photo)

    def tick(self):
        if self.closed:
            return
        if self.grid_mode:
            now = time.monotonic()
            playing = 0
            for state in self.grid_streams.values():
                if state["retry"] is not None:
                    remaining = max(0, math.ceil(state["retry"] - now))
                    state["status"].config(text=f"Reconnecting in {remaining:02d} s", style="Error.TLabel")
                    if remaining == 0:
                        self.start_grid_stream(state)
                elif state["last_frame"] is not None:
                    age = now - state["last_frame"]
                    if age < 3:
                        playing += 1
                    else:
                        state["status"].config(text=f"Buffering ({int(age)} s)", style="Muted.TLabel")
            if self.grid_streams:
                self.status.config(text=f"{self.city.get()}: {playing}/{len(self.grid_streams)} camera previews playing.",
                                   style="Muted.TLabel")
            self.root.after(250, self.tick)
            return
        if self.selected and self.fetching_generation == self.generation:
            text = "Live video: connecting..."
            if self.connection_started is not None and time.monotonic() - self.connection_started >= CONNECTION_STALL_SECONDS:
                self.start_stream()
                text = "Live video: reconnecting..."
        elif self.selected and self.next_refresh is not None:
            remaining = max(0, math.ceil(self.next_refresh - time.monotonic()))
            text = f"Reconnecting in {remaining:02d} s"
            if remaining == 0:
                self.start_stream()
        elif self.playing and self.last_frame_time is not None:
            age = time.monotonic() - self.last_frame_time
            text = "Live video: playing" if age < 3 else f"Live video: buffering ({int(age)} s)"
            if age >= STREAM_STALL_SECONDS:
                self.start_stream()
                text = "Live video: reconnecting..."
        elif self.selected:
            text = "Video stopped"
        else:
            text = ""
        self.timer.config(text=text)
        self.root.after(250, self.tick)

    def open_map(self):
        if self.selected:
            lat, lon = self.selected.get("Latitude"), self.selected.get("Longitude")
            if lat is not None and lon is not None:
                webbrowser.open(f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}")

    def open_video(self):
        if not self.selected:
            return
        camera, generation = dict(self.selected), self.generation
        self.video_button.config(state="disabled")
        self.status.config(text="Requesting KC Scout public video link...", style="Muted.TLabel")
        def worker():
            try:
                self.results.put(("video", generation, load_video_url(camera), None))
            except Exception as error:
                self.results.put(("video", generation, None, str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def check_theme(self):
        if self.closed:
            return
        dark = system_dark_theme()
        if dark != self.dark:
            self.dark = dark
            self.colors = ({"bg": "#202020", "fg": "#f2f2f2", "field": "#2b2b2b",
                            "muted": "#b5b5b5", "error": "#ff9999"} if dark else
                           {"bg": "#f0f0f0", "fg": "#202020", "field": "#ffffff",
                            "muted": "#606060", "error": "#b00020"})
            colors = self.colors
            self.root.config(bg=colors["bg"])
            self.style.configure(".", background=colors["bg"], foreground=colors["fg"],
                                 fieldbackground=colors["field"], troughcolor=colors["bg"])
            self.style.configure("Muted.TLabel", foreground=colors["muted"])
            self.style.configure("Error.TLabel", foreground=colors["error"])
            self.style.configure("TEntry", insertcolor=colors["fg"])
            self.style.configure("Treeview", background=colors["field"], fieldbackground=colors["field"],
                                 foreground=colors["fg"], rowheight=30)
            self.style.map("Treeview", background=[("selected", "#0067c0")],
                           foreground=[("selected", "#ffffff")])
            self.style.map("TCombobox", fieldbackground=[("readonly", colors["field"])],
                           foreground=[("readonly", colors["fg"])], selectbackground=[("readonly", colors["field"])],
                           selectforeground=[("readonly", colors["fg"])])
            self.style.map("TButton", background=[("active", colors["field"])])
            self.root.option_add("*TCombobox*Listbox.background", colors["field"])
            self.root.option_add("*TCombobox*Listbox.foreground", colors["fg"])
            self.canvas.config(bg=colors["field"])
            self.gallery_canvas.config(bg=colors["bg"])
            for state in self.grid_streams.values():
                state["canvas"].config(bg=colors["field"])
                self.draw_tile(state)
            self.draw_image()
        self.root.after(2000, self.check_theme)

    def close(self):
        self.closed = True
        self.stop_stream()
        self.stop_grid()
        self.root.destroy()


def run_packaged_check(window, app, report_path):
    import hashlib
    started = time.monotonic()
    frame_hashes = set()

    def check():
        for state in app.grid_streams.values():
            if state["image"] is not None and state["photo"] is not None:
                frame_hashes.add(hashlib.sha256(state["image"].tobytes()).hexdigest())
        passed = len(frame_hashes) >= 3
        if passed or time.monotonic() - started >= 60:
            report = {"passed": passed, "frozen": bool(getattr(sys, "frozen", False)),
                      "python": sys.version, "decoder": cv2.__version__,
                      "distinct_rendered_frames": len(frame_hashes),
                      "camera_count": len(app.grid_streams), "status": app.status.cget("text"),
                      "browser_player_bundled": Path(__file__).with_name("kc_scout_video_player.html").is_file()}
            Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
            app.close()
        else:
            window.after(100, check)

    window.after(100, check)


if __name__ == "__main__":
    window = tk.Tk()
    application = CameraApp(window)
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        run_packaged_check(window, application, sys.argv[2])
    window.mainloop()
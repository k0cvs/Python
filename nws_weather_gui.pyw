"""NWS Weather by ZIP code - double-click to run (Windows). Uses only the Python standard library."""
import json
import math
import queue
import re
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

# NWS requires a User-Agent identifying the application.
USER_AGENT = "NWSZipWeatherGUI/1.0 (personal use)"
REFRESH_MS = 60_000
WARNING_EVENTS = {"Severe Thunderstorm Warning", "Tornado Warning"}
AMBIENT_API_KEY = "a77af6e49b234fcaa92793603b2e763ee34be781a2d04e529abb83476ae6931b"
AMBIENT_APP_KEY = "0948ec5093354c21b14ea8ed3f8b659fad3d41c09f3f4ef0945af9ebb9d8c825"


def system_uses_dark_theme():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


def set_title_bar_theme(root, dark):
    try:
        import ctypes
        from ctypes import wintypes
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [wintypes.HWND]
        get_parent.restype = wintypes.HWND
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD]
        set_attribute.restype = ctypes.c_long
        root.update_idletasks()
        handle = get_parent(root.winfo_id())
        enabled = wintypes.BOOL(dark)
        for attribute in (20, 19):
            if set_attribute(handle, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)) == 0:
                break
    except (AttributeError, OSError):
        pass


def fetch_home_weather():
    query = urllib.parse.urlencode({"apiKey": AMBIENT_API_KEY,
                                    "applicationKey": AMBIENT_APP_KEY})
    try:
        devices = get_json("https://api.ambientweather.net/v1/devices?" + query)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Ambient Weather request failed (HTTP {error.code}).") from None
    except urllib.error.URLError:
        raise ValueError("Unable to connect to Ambient Weather.") from None
    if not isinstance(devices, list) or not devices:
        raise ValueError("No Ambient Weather stations are available for these keys.")
    device = next((device for device in devices
                   if (device.get("lastData") or {}).get("tempf") is not None), None)
    if device is None:
        raise ValueError("Ambient Weather has no current outdoor temperature reading.")
    readings = device["lastData"]
    info = device.get("info") or {}
    station = info.get("name") or "Home station"
    current = {"textDescription": "Ambient Weather", "timestamp": readings.get("date")}
    for source, target, scale, offset in [
        ("tempf", "temperature", 5 / 9, -32),
        ("feelsLike", "heatIndex", 5 / 9, -32),
        ("dewPoint", "dewpoint", 5 / 9, -32),
        ("humidity", "relativeHumidity", 1, 0),
        ("windspeedmph", "windSpeed", 1 / 0.621371, 0),
        ("windgustmph", "windGust", 1 / 0.621371, 0),
        ("winddir", "windDirection", 1, 0),
        ("baromrelin", "barometricPressure", 3386.389, 0),
    ]:
        value = readings.get(source)
        current[target] = {"value": None if value is None else (value + offset) * scale}
    if not current["timestamp"] and readings.get("dateutc") is not None:
        current["timestamp"] = datetime.fromtimestamp(
            readings["dateutc"] / 1000, timezone.utc).isoformat()
    result = {"place": f"Home - {station}", "current": current, "station": station,
              "periods": [], "radar_station": None, "radar_gif": None, "warnings": []}
    try:
        local_weather = fetch_weather("66216")
        local_current = local_weather["current"] or {}
        current["textDescription"] = local_current.get("textDescription") or "Ambient Weather"
        current["icon"] = local_current.get("icon") or ""
        local_weather.update(place=result["place"], current=current, station=station)
        return local_weather
    except (ValueError, KeyError, urllib.error.URLError):
        pass
    return result


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_radar(station):
    url = f"https://radar.weather.gov/ridge/standard/{station}_loop.gif"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def zip_to_location(zip_code):
    try:
        data = get_json(f"https://api.zippopotam.us/us/{zip_code}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"ZIP code {zip_code} was not found.")
        raise
    place = data["places"][0]
    name = f'{place["place name"]}, {place["state abbreviation"]}'
    return float(place["latitude"]), float(place["longitude"]), name


def obs_value(obs, key):
    return (obs.get(key) or {}).get("value")


def c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def kmh_to_mph(k):
    return None if k is None else k * 0.621371


def deg_to_compass(deg):
    if deg is None:
        return ""
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) // 22.5) % 16]


def fetch_weather(zip_code):
    if zip_code.strip().lower() == "home":
        return fetch_home_weather()
    lat, lon, place = zip_to_location(zip_code)
    return fetch_location_weather(lat, lon, place)


def fetch_location_weather(lat, lon, place, current=None, station_name=None):
    try:
        points = get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("The National Weather Service has no data for this location.")
        raise
    props = points["properties"]
    periods = get_json(props["forecast"])["properties"]["periods"]

    stations = [] if current is not None else get_json(props["observationStations"])["features"]
    for st in stations[:5]:
        try:
            obs = get_json(st["id"] + "/observations/latest")["properties"]
        except urllib.error.HTTPError:
            continue
        if obs_value(obs, "temperature") is not None:
            current, station_name = obs, st["properties"].get("name", "")
            break

    radar_station = props.get("radarStation")
    try:
        radar_gif = fetch_radar(radar_station) if radar_station else None
    except Exception:
        radar_gif = None

    try:
        alerts = get_json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}")["features"]
        warnings = sorted({a["properties"]["event"] for a in alerts} & WARNING_EVENTS)
    except Exception:
        warnings = []

    return {"place": place, "current": current, "station": station_name, "periods": periods,
            "radar_station": radar_station, "radar_gif": radar_gif, "warnings": warnings}


def condition_icon(current):
    if not current:
        return None
    d = (current.get("textDescription") or "").lower()
    # NWS icon URLs contain /day/ or /night/.
    night = "/night/" in (current.get("icon") or "")
    rules = [
        (("tornado", "funnel"), "tornado"),
        (("thunder", "t-storm"), "thunder"),
        (("snow", "flurr", "blizzard"), "snow"),
        (("sleet", "ice", "freezing", "hail"), "sleet"),
        (("rain", "shower", "drizzle"), "rain"),
        (("fog", "mist", "haze", "smoke", "dust"), "fog"),
        (("wind", "breez"), "wind"),
        (("partly", "few clouds", "mostly clear", "mostly sunny"), "partly_night" if night else "partly_day"),
        (("mostly cloudy", "overcast", "cloudy"), "cloudy"),
        (("clear", "sunny", "fair"), "moon" if night else "sun"),
    ]
    for words, kind in rules:
        if any(w in d for w in words):
            return kind
    return "thermometer"


class WeatherIcon:
    """Animated weather icon drawn on a Tk canvas."""
    SIZE = 110

    def __init__(self, parent):
        self.bg = ttk.Style().lookup("TLabelframe", "background") or "SystemButtonFace"
        self.canvas = tk.Canvas(parent, width=self.SIZE, height=self.SIZE, bg=self.bg, highlightthickness=0)
        self.kind = None
        self.frame = 0
        self.job = None

    def set(self, kind):
        if kind == self.kind:
            return
        self.kind = kind
        self.frame = 0
        if self.job is None:
            self._tick()

    def _tick(self):
        self.canvas.delete("all")
        if not self.kind:
            self.job = None
            return
        getattr(self, "_draw_" + self.kind)(self.frame)
        self.frame += 1
        self.job = self.canvas.after(50, self._tick)

    # --- building blocks ---
    def _sun(self, cx, cy, r, f):
        c = self.canvas
        ray = r + 14 + 3 * math.sin(f / 5)
        for i in range(8):
            a = math.radians(f * 2 + i * 45)
            c.create_line(cx + (r + 5) * math.cos(a), cy + (r + 5) * math.sin(a),
                          cx + ray * math.cos(a), cy + ray * math.sin(a),
                          fill="#f2a900", width=4, capstyle="round")
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#ffc83d", outline="#f2a900", width=2)

    def _moon(self, cx, cy, r, f):
        c = self.canvas
        for i, (sx, sy) in enumerate([(20, 20), (88, 30), (78, 88), (25, 80)]):
            s = 1.5 + 1.5 * (1 + math.sin(f / 6 + i * 1.7)) / 2
            c.create_oval(sx - s, sy - s, sx + s, sy + s, fill="#e6d36a", outline="")
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#f5e27a", outline="")
        c.create_oval(cx - r * 0.4, cy - r * 1.15, cx + r * 1.6, cy + r * 0.85, fill=self.bg, outline="")

    def _cloud(self, cx, cy, s=1.0, fill="#c9ced6"):
        c = self.canvas
        for x1, y1, x2, y2 in [(-30, -5, -5, 20), (-18, -22, 12, 8), (0, -12, 30, 18), (-17, 2, 17, 20)]:
            c.create_oval(cx + x1 * s, cy + y1 * s, cx + x2 * s, cy + y2 * s, fill=fill, outline="")

    def _drops(self, f, color="#2f6fb5", ice=False):
        c = self.canvas
        for i in range(5):
            x = 31 + i * 12
            y = 66 + (f * 4 + i * 13) % 38
            if ice and i % 2:
                c.create_polygon(x, y, x + 3, y + 3, x, y + 6, x - 3, y + 3, fill="#9fd8ef", outline="#4fb3d9")
            else:
                c.create_line(x, y, x - 3, y + 8, fill=color, width=2, capstyle="round")

    # --- icons ---
    def _draw_sun(self, f):
        self._sun(55, 55, 20, f)

    def _draw_moon(self, f):
        self._moon(52, 55, 24, f)

    def _draw_partly_day(self, f):
        self._sun(40, 40, 16, f)
        self._cloud(62 + 4 * math.sin(f / 15), 66)

    def _draw_partly_night(self, f):
        self._moon(42, 40, 18, f)
        self._cloud(62 + 4 * math.sin(f / 15), 66)

    def _draw_cloudy(self, f):
        self._cloud(42 - 4 * math.sin(f / 18), 45, 0.85, "#dde1e7")
        self._cloud(62 + 4 * math.sin(f / 15), 64, 1.1, "#aeb5bf")

    def _draw_rain(self, f):
        self._drops(f)
        self._cloud(55 + 2 * math.sin(f / 15), 42, 1.2, "#9aa3ae")

    def _draw_sleet(self, f):
        self._drops(f, ice=True)
        self._cloud(55 + 2 * math.sin(f / 15), 42, 1.2, "#a7b0bb")

    def _draw_thunder(self, f):
        self._drops(f, "#4b6fa5")
        if f % 40 < 4 or 8 <= f % 40 < 11:
            self.canvas.create_polygon(58, 52, 44, 76, 54, 76, 46, 100, 68, 70, 57, 70, 64, 52,
                                       fill="#ffd400", outline="#e6a400")
        self._cloud(55 + 2 * math.sin(f / 15), 40, 1.25, "#6d7480")

    def _draw_snow(self, f):
        c = self.canvas
        for i in range(6):
            x = 28 + i * 11 + 4 * math.sin((f + i * 10) / 6)
            y = 64 + (f * 1.5 + i * 11) % 42
            c.create_text(x, y, text="*", font=("Segoe UI", 14, "bold"), fill="#6fa8dc")
        self._cloud(55 + 2 * math.sin(f / 15), 42, 1.2, "#b8c2cc")

    def _draw_fog(self, f):
        self._cloud(55, 35, 1.0, "#c9ced6")
        for i in range(4):
            off = 7 * math.sin((f + i * 8) / 10)
            y = 62 + i * 10
            self.canvas.create_line(18 + off, y, 92 + off, y, fill="#9a9a9a", width=4, capstyle="round")

    def _draw_wind(self, f):
        for i in range(3):
            x = (f * 3 + i * 45) % 150 - 45
            y = 35 + i * 20
            self.canvas.create_line(x, y, x + 40, y, x + 52, y - 6, x + 46, y - 14, x + 38, y - 8,
                                    smooth=True, fill="#7a9cb8", width=4, capstyle="round")

    def _draw_tornado(self, f):
        for i in range(7):
            w = 40 - i * 5
            cx = 55 + 6 * math.sin((f + i * 5) / 6) * (i / 6)
            y = 20 + i * 11
            self.canvas.create_oval(cx - w, y - 5, cx + w, y + 5, outline="#555555", width=3)

    def _draw_police(self, f):
        c = self.canvas
        angle = (f * 14) % 360
        for start in (angle, angle + 180):
            c.create_arc(-15, -15, 125, 115, start=start, extent=35, fill="#ffb3b3", outline="")
        c.create_arc(32, 26, 78, 118, start=0, extent=180, fill="#d10000", outline="#8a0000", width=2)
        # Moving highlight makes the reflector look like it's rotating inside the dome.
        hx = 55 + 15 * math.cos(math.radians(angle))
        hw = 4 + 5 * abs(math.sin(math.radians(angle)))
        c.create_oval(hx - hw, 38, hx + hw, 68, fill="#ff8080", outline="")
        c.create_rectangle(26, 72, 84, 88, fill="#333333", outline="#111111")

    def _draw_thermometer(self, f):
        c = self.canvas
        level = 45 + 15 * math.sin(f / 10)
        c.create_oval(43, 72, 67, 96, fill="#d9534f", outline="#888", width=2)
        c.create_rectangle(49, 14, 61, 78, fill="white", outline="#888", width=2)
        c.create_rectangle(52, level, 58, 80, fill="#d9534f", outline="")
        c.create_oval(46, 75, 64, 93, fill="#d9534f", outline="")


def format_current(current, station):
    if not current:
        return "Current conditions are unavailable."

    lines = [current.get("textDescription") or "—"]
    temp = c_to_f(obs_value(current, "temperature"))
    lines.append(f"\U0001F321 Temperature:  {temp:.0f}°F")

    feels = obs_value(current, "heatIndex")
    feels_icon = "\U0001F525"
    if feels is None:
        feels = obs_value(current, "windChill")
        feels_icon = "\u2744"
    if feels is not None:
        lines.append(f"{feels_icon} Feels like:   {c_to_f(feels):.0f}°F")

    dew = c_to_f(obs_value(current, "dewpoint"))
    if dew is not None:
        lines.append(f"\U0001F4A7 Dew point:    {dew:.0f}°F")

    rh = obs_value(current, "relativeHumidity")
    if rh is not None:
        lines.append(f"\U0001F4A6 Humidity:     {rh:.0f}%")

    speed = kmh_to_mph(obs_value(current, "windSpeed"))
    if speed is not None:
        wind = "Calm" if speed < 1 else f"{deg_to_compass(obs_value(current, 'windDirection'))} {speed:.0f} mph"
        gust = kmh_to_mph(obs_value(current, "windGust"))
        if gust:
            wind += f", gusts {gust:.0f} mph"
        lines.append(f"\U0001F4A8 Wind:         {wind}")

    pressure = obs_value(current, "barometricPressure")
    if pressure is not None:
        lines.append(f"\U0001F4C8 Pressure:     {pressure / 3386.389:.2f} inHg")

    vis = obs_value(current, "visibility")
    if vis is not None:
        lines.append(f"\U0001F441 Visibility:   {vis / 1609.344:.1f} mi")

    ts = current.get("timestamp")
    if ts:
        local = datetime.fromisoformat(ts).astimezone()
        lines.append(f"\n\U0001F552 Observed {local:%a %I:%M %p} at {station}")
    return "\n".join(lines)


class WeatherApp:
    def __init__(self, root):
        self.root = root
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.theme_dark = None
        self.results = queue.Queue()
        self.zip_code = None
        self.refresh_job = None
        self.next_weather_update: float | None = None
        self.weather_updating = False
        self.radar_frames = []
        self.radar_index = 0
        self.radar_station = None
        self.radar_anim_job = None
        root.title("NWS Weather by ZIP Code")
        root.geometry("1280x720")
        root.minsize(1000, 620)

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="ZIP code:", font=("Segoe UI", 11)).pack(side="left")
        self.zip_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.zip_var, width=10, font=("Segoe UI", 11))
        self.entry.pack(side="left", padx=6)
        self.entry.bind("<Return>", lambda _e: self.lookup())
        self.button = ttk.Button(top, text="Get Weather", command=self.lookup)
        self.button.pack(side="left")
        self.status = ttk.Label(top, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=10)

        self.countdown_label = ttk.Label(root, text="", style="Muted.TLabel",
                         font=("Consolas", 10), padding=(10, 0, 10, 6))
        self.countdown_label.pack(anchor="w")
        self.place_label = ttk.Label(root, text="", font=("Segoe UI", 14, "bold"), padding=(10, 0))
        self.place_label.pack(anchor="w")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        radar_frame = ttk.LabelFrame(body, text="Radar Loop", padding=5)
        radar_frame.pack(side="right", fill="y", padx=(0, 10), pady=(5, 10))
        self.radar_label = ttk.Label(radar_frame, text="Radar loop will appear here.", anchor="center",
                                     style="Muted.TLabel", width=85)
        self.radar_label.pack(fill="both", expand=True)
        self.radar_caption = ttk.Label(radar_frame, text="", style="Muted.TLabel")
        self.radar_caption.pack(anchor="w")
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        cur_frame = ttk.LabelFrame(left, text="Current Conditions", padding=10)
        cur_frame.pack(fill="x", padx=10, pady=5)
        self.weather_icon = WeatherIcon(cur_frame)
        self.weather_icon.canvas.pack(side="left", padx=(0, 15))
        self.current_label = ttk.Label(cur_frame, text="Enter a ZIP code above.",
                                       font=("Consolas", 10), justify="left")
        self.current_label.pack(side="left", anchor="w")

        fc_frame = ttk.LabelFrame(left, text="Forecast", padding=5)
        fc_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.forecast_text = ScrolledText(fc_frame, wrap="word", font=("Segoe UI", 10), state="disabled")
        self.forecast_text.pack(fill="both", expand=True)
        self.forecast_text.tag_configure("head", font=("Segoe UI", 10, "bold"))

        self.check_system_theme()
        self.update_countdown()
        self.entry.focus_set()

    def update_countdown(self):
        if self.weather_updating:
            text = "Weather: updating..."
        elif self.next_weather_update is not None:
            seconds = max(0, math.ceil(self.next_weather_update - time.monotonic()))
            text = f"Next update in {seconds // 60:02d}:{seconds % 60:02d}"
        else:
            text = ""
        self.countdown_label.config(text=text)
        self.root.after(250, self.update_countdown)

    def check_system_theme(self):
        dark = system_uses_dark_theme()
        if dark != self.theme_dark:
            self.apply_theme(dark)
        self.root.after(2000, self.check_system_theme)

    def apply_theme(self, dark):
        self.theme_dark = dark
        colors = ({"bg": "#202020", "fg": "#f2f2f2", "field": "#2b2b2b",
                   "muted": "#b5b5b5", "border": "#555555", "active": "#404040",
                   "error": "#ff9999", "selection": "#0067c0"} if dark else
                  {"bg": "#f0f0f0", "fg": "#202020", "field": "#ffffff",
                   "muted": "#606060", "border": "#b0b0b0", "active": "#e0e0e0",
                   "error": "#b00020", "selection": "#0067c0"})
        self.root.config(bg=colors["bg"])
        self.style.configure(".", background=colors["bg"], foreground=colors["fg"],
                             bordercolor=colors["border"], lightcolor=colors["border"],
                             darkcolor=colors["border"], troughcolor=colors["bg"])
        self.style.configure("TFrame", background=colors["bg"])
        self.style.configure("TLabel", background=colors["bg"], foreground=colors["fg"])
        self.style.configure("Muted.TLabel", foreground=colors["muted"])
        self.style.configure("Error.TLabel", foreground=colors["error"])
        self.style.configure("TLabelframe", background=colors["bg"])
        self.style.configure("TLabelframe.Label", background=colors["bg"], foreground=colors["fg"])
        self.style.configure("TEntry", fieldbackground=colors["field"], foreground=colors["fg"],
                             insertcolor=colors["fg"], selectbackground=colors["selection"],
                             selectforeground="#ffffff")
        self.style.map("TEntry", fieldbackground=[("disabled", colors["bg"])],
                       foreground=[("disabled", colors["muted"])])
        self.style.configure("TButton", background=colors["field"], foreground=colors["fg"])
        self.style.map("TButton", background=[("disabled", colors["bg"]),
                                              ("pressed", colors["active"]),
                                              ("active", colors["active"])],
                       foreground=[("disabled", colors["muted"])])
        self.style.configure("Vertical.TScrollbar", background=colors["active"],
                             arrowcolor=colors["fg"])
        self.style.map("Vertical.TScrollbar", background=[("active", colors["border"]),
                                                          ("pressed", colors["border"])])
        self.forecast_text.config(background=colors["field"], foreground=colors["fg"],
                                  insertbackground=colors["fg"], selectbackground=colors["selection"],
                                  selectforeground="#ffffff", highlightbackground=colors["border"],
                                  highlightcolor=colors["selection"])
        self.forecast_text.frame.config(background=colors["bg"])
        self.forecast_text.vbar.config(background=colors["active"], troughcolor=colors["bg"],
                                      activebackground=colors["border"],
                                      highlightbackground=colors["bg"])
        self.weather_icon.bg = colors["bg"]
        self.weather_icon.canvas.config(background=colors["bg"])
        set_title_bar_theme(self.root, dark)

    def lookup(self):
        zip_code = self.zip_var.get().strip().lower()
        if zip_code != "home" and not re.fullmatch(r"\d{5}", zip_code):
            self.status.config(text="Please enter a 5-digit ZIP code or home.", style="Error.TLabel")
            return
        self.zip_code = zip_code
        self.status.config(text="Loading...", style="Muted.TLabel")
        self.fetch()

    def fetch(self):
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        self.next_weather_update = None
        self.weather_updating = True
        self.button.config(state="disabled")
        threading.Thread(target=self._worker, args=(self.zip_code,), daemon=True).start()
        self.root.after(100, self._poll)

    def auto_refresh(self):
        self.refresh_job = None
        self.status.config(text="Refreshing...", style="Muted.TLabel")
        self.fetch()

    def _worker(self, zip_code):
        try:
            self.results.put((zip_code, "ok", fetch_weather(zip_code)))
        except ValueError as e:
            self.results.put((zip_code, "err", str(e)))
        except urllib.error.URLError as e:
            self.results.put((zip_code, "err", f"Network error: {getattr(e, 'reason', e)}"))
        except Exception as e:
            self.results.put((zip_code, "err", f"Unexpected error: {e}"))

    def _poll(self):
        try:
            zip_code, kind, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        if zip_code != self.zip_code:
            return
        self.button.config(state="normal")
        if kind == "err":
            self.status.config(text=payload, style="Error.TLabel")
        else:
            self.status.config(text=f"Updated {datetime.now():%I:%M:%S %p} (refreshes every minute)",
                               style="Muted.TLabel")
            self.show(payload)
        self.weather_updating = False
        self.next_weather_update = time.monotonic() + REFRESH_MS / 1000
        self.refresh_job = self.root.after(REFRESH_MS, self.auto_refresh)

    def show(self, data):
        self.place_label.config(text=data["place"])
        self.current_label.config(text=format_current(data["current"], data["station"]))
        self.weather_icon.set("police" if data["warnings"] else condition_icon(data["current"]))

        t = self.forecast_text
        scroll = t.yview()[0]
        t.config(state="normal")
        t.delete("1.0", "end")
        for p in data["periods"]:
            t.insert("end", f'{p["name"]}: {p["temperature"]}°{p["temperatureUnit"]} – {p["shortForecast"]}\n', "head")
            t.insert("end", f'{p["detailedForecast"]}\n\n')
        t.config(state="disabled")
        t.yview_moveto(scroll)

        same_station = data["radar_station"] == self.radar_station
        self.radar_station = data["radar_station"]
        # Keep the old loop if a refresh's radar download fails.
        if data["radar_gif"] or not same_station:
            self.load_radar(data["radar_gif"], keep_position=same_station)

    def load_radar(self, gif, keep_position=False):
        if self.radar_anim_job:
            self.root.after_cancel(self.radar_anim_job)
        self.radar_anim_job = None
        frames = []
        if gif:
            while True:
                try:
                    frames.append(tk.PhotoImage(data=gif, format=f"gif -index {len(frames)}"))
                except tk.TclError:
                    break
        # Switch the label off the old images before they're garbage-collected.
        self.radar_label.config(image=frames[0] if frames else "")
        self.radar_frames = frames
        if not self.radar_frames:
            self.radar_label.config(image="", text="Radar loop is unavailable.")
            self.radar_caption.config(text="")
        else:
            self.radar_label.config(text="")
            self.radar_caption.config(
                text=f"NWS radar {self.radar_station} \u2013 base reflectivity, "
                     f"loaded {datetime.now():%I:%M %p}")
            if not keep_position or self.radar_index >= len(self.radar_frames):
                self.radar_index = 0
            self.animate_radar()

    def animate_radar(self):
        self.radar_label.config(image=self.radar_frames[self.radar_index])
        last = self.radar_index == len(self.radar_frames) - 1
        self.radar_index = 0 if last else self.radar_index + 1
        self.radar_anim_job = self.root.after(1500 if last else 400, self.animate_radar)


if __name__ == "__main__":
    root = tk.Tk()
    WeatherApp(root)
    root.mainloop()

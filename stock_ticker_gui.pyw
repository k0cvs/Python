"""Stock quote & charts by ticker - double-click to run (Windows). Uses only the Python standard library."""
import bisect
import http.cookiejar
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
from datetime import datetime
from tkinter import ttk

REFRESH_MS = 60_000
LIVE_MS = 5_000
UP, DOWN = "#1a9e3f", "#d03030"
CHARTS = [  # (title, Yahoo range, interval, x-axis time format)
    ("Intraday", "1d", "1m", "%I:%M %p"),
    ("5 Days", "5d", "15m", "%a %I %p"),
    ("30 Days", "1mo", "1h", "%b %d"),
    ("365 Days", "1y", "1d", "%b %Y"),
]
HOVER_FMT = {"Intraday": "%I:%M %p", "5 Days": "%a %b %d, %I:%M %p",
             "30 Days": "%a %b %d, %I:%M %p", "365 Days": "%a %b %d, %Y"}


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


def get_chart(ticker, rng, interval):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
           f"?range={rng}&interval={interval}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f'Ticker "{ticker}" was not found.')
        raise
    result = (data.get("chart") or {}).get("result")
    if not result:
        raise ValueError(f'Ticker "{ticker}" was not found.')
    result = result[0]
    quote = result["indicators"]["quote"][0]
    closes = quote.get("close") or []
    points = [(t, c) for t, c in zip(result.get("timestamp") or [], closes) if c is not None]
    if rng == "1d" and result["meta"].get("regularMarketOpen") is None:
        opens = quote.get("open") or []
        result["meta"]["regularMarketOpen"] = opens[0] if opens else None
    return result["meta"], points


def fetch_stock(ticker):
    charts = {}
    meta = None
    for title, rng, interval, _fmt in CHARTS:
        m, pts = get_chart(ticker, rng, interval)
        if meta is None:
            meta = m
        charts[title] = pts
    try:
        implied_shares = fetch_implied_shares(ticker)
    except Exception:
        implied_shares = None
    return {"meta": meta, "charts": charts, "shares": implied_shares}


_yahoo_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
_yahoo_crumb = None


def _yahoo_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _yahoo_opener.open(req, timeout=20) as resp:
        return resp.read()


def _get_crumb(refresh=False):
    global _yahoo_crumb
    if _yahoo_crumb is None or refresh:
        try:
            _yahoo_get("https://fc.yahoo.com")
        except urllib.error.HTTPError:
            pass  # Responds with an error page but still sets the session cookie.
        _yahoo_crumb = _yahoo_get("https://query1.finance.yahoo.com/v1/test/getcrumb").decode().strip()
    return _yahoo_crumb


def fetch_implied_shares(ticker):
    """Market cap / price from Yahoo's quote, so market cap can be recomputed from the live price."""
    for attempt in range(2):
        url = ("https://query1.finance.yahoo.com/v7/finance/quote?fields=marketCap,regularMarketPrice"
               f"&symbols={urllib.parse.quote(ticker)}&crumb={urllib.parse.quote(_get_crumb(attempt > 0))}")
        try:
            result = json.loads(_yahoo_get(url))["quoteResponse"]["result"]
            break
        except urllib.error.HTTPError as e:
            if e.code != 401 or attempt:
                raise
    if not result:
        return None
    cap, price = result[0].get("marketCap"), result[0].get("regularMarketPrice")
    return cap / price if cap and price else None


def format_big(n):
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if n >= size:
            return f"{n / size:,.2f}{suffix}"
    return f"{n:,.0f}"


def market_session_status(meta, now):
    periods = meta.get("currentTradingPeriod") or {}
    has_session = False
    for key, label in (("regular", "Market open"), ("pre", "Pre-market"),
                       ("post", "After-hours")):
        session = periods.get(key) or {}
        start, end = session.get("start"), session.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            has_session = True
            if start <= now < end:
                return label
    if has_session:
        return "Market closed"
    return {"REGULAR": "Market open", "PRE": "Pre-market", "POST": "After-hours",
            "CLOSED": "Market closed", "PREPRE": "Market closed",
            "POSTPOST": "Market closed"}.get(meta.get("marketState"), "Status unavailable")


def intraday_view(meta, points):
    """Return points with the latest live price appended, and the session (start, end) if it applies."""
    points = list(points)
    price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is not None and ts and (not points or ts > points[-1][0]):
        points.append((ts, price))
    session = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = session.get("start"), session.get("end")
    # Before today's open Yahoo returns the previous session, so today's hours don't apply.
    if start and end and points and points[0][0] >= start - 3600:
        return points, (min(start, points[0][0]), max(end, points[-1][0]))
    return points, None


def draw_chart(canvas, title, points, time_fmt, baseline=None, x_range=None):
    colors = getattr(canvas, "theme_colors", {"fg": "#202020", "muted": "#606060",
                     "grid": "#e4e4e4", "border": "#999999", "field": "#ffffff",
                     "up": UP, "down": DOWN})
    canvas.delete("all")
    canvas.plot_points = []
    w, h = canvas.winfo_width(), canvas.winfo_height()
    left, right, top, bottom = 60, 12, 28, 24
    canvas.plot_box = (left, top, w - right, h - bottom)
    if len(points) < 2 or w < 100 or h < 80:
        canvas.create_text(w / 2, h / 2, text=f"{title}: no data", fill=colors["muted"])
        return

    prices = [p for _, p in points]
    base = baseline if baseline is not None else prices[0]
    change = (prices[-1] - base) / base * 100
    color = colors["up"] if prices[-1] >= base else colors["down"]
    canvas.create_text(8, 6, anchor="nw", text=title, fill=colors["fg"], font=("Segoe UI", 10, "bold"))
    canvas.create_text(w - 8, 6, anchor="ne", text=f"{change:+.2f}%", fill=color,
                       font=("Segoe UI", 10, "bold"))

    lo, hi = min(prices), max(prices)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    plot_w, plot_h = w - left - right, h - top - bottom

    def y_of(price):
        return top + (hi - price) / (hi - lo) * plot_h

    for i in range(5):
        price = lo + (hi - lo) * i / 4
        y = y_of(price)
        canvas.create_line(left, y, w - right, y, fill=colors["grid"])
        canvas.create_text(left - 5, y, anchor="e", text=f"{price:,.2f}", font=("Segoe UI", 8), fill=colors["muted"])

    if baseline is not None:
        y = y_of(baseline)
        canvas.create_line(left, y, w - right, y, fill=colors["border"], dash=(3, 3))

    coords = []
    n = len(prices)
    for i, (t, price) in enumerate(points):
        if x_range:
            x = left + (t - x_range[0]) / (x_range[1] - x_range[0]) * plot_w
        else:
            x = left + i / (n - 1) * plot_w
        coords += [x, y_of(price)]
    canvas.create_line(*coords, fill=color, width=2)
    canvas.plot_points = [(coords[2 * i], coords[2 * i + 1], t, p) for i, (t, p) in enumerate(points)]
    if x_range:
        x, y = coords[-2], coords[-1]
        canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=colors["field"])

    t0, t1 = x_range or (points[0][0], points[-1][0])
    start = datetime.fromtimestamp(t0).strftime(time_fmt)
    end = datetime.fromtimestamp(t1).strftime(time_fmt)
    canvas.create_text(left, h - 4, anchor="sw", text=start, font=("Segoe UI", 8), fill=colors["muted"])
    canvas.create_text(w - right, h - 4, anchor="se", text=end, font=("Segoe UI", 8), fill=colors["muted"])


class RichGuy:
    """Canvas animation: rich guy dances in falling money when up, cries when down."""
    W, H = 180, 120
    SKIN, GOLD = "#f1c27d", "#c9a227"
    SUIT: str = "#222222"

    def __init__(self, parent):
        bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        self.canvas = tk.Canvas(parent, width=self.W, height=self.H, bg=bg, highlightthickness=0)
        self.mood = None
        self.frame = 0
        self.job = None

    def set(self, mood):
        if mood == self.mood:
            return
        self.mood = mood
        self.frame = 0
        if self.job is None:
            self._tick()

    def _tick(self):
        self.canvas.delete("all")
        if not self.mood:
            self.job = None
            return
        (self._draw_happy if self.mood == "happy" else self._draw_sad)(self.frame)
        self.frame += 1
        self.job = self.canvas.after(50, self._tick)

    def _limb(self, *pts):
        self.canvas.create_line(*pts, fill=self.SUIT, width=4, capstyle="round", joinstyle="round")

    def _hat(self, cx, top, tilt=0.0):
        def rot(points):
            out = []
            for x, y in points:
                dx, dy = x - cx, y - (top + 18)
                out += [cx + dx * math.cos(tilt) - dy * math.sin(tilt), top + 18 + dx * math.sin(tilt) + dy * math.cos(tilt)]
            return out
        c = self.canvas
        c.create_polygon(rot([(cx - 8, top), (cx + 8, top), (cx + 8, top + 18), (cx - 8, top + 18)]), fill=self.SUIT)
        c.create_polygon(rot([(cx - 8, top + 13), (cx + 8, top + 13), (cx + 8, top + 16), (cx - 8, top + 16)]),
                         fill=self.GOLD)
        c.create_line(rot([(cx - 14, top + 18), (cx + 14, top + 18)]), fill=self.SUIT, width=3)

    def _body(self, cx, sy, hy):
        c = self.canvas
        c.create_polygon(cx - 11, sy, cx + 11, sy, cx + 9, hy, cx - 9, hy, fill=self.SUIT)
        c.create_polygon(cx - 5, sy, cx + 5, sy, cx, sy + 12, fill="white")
        c.create_polygon(cx - 5, sy + 1, cx, sy + 4, cx + 5, sy + 1, cx + 5, sy + 7, cx, sy + 4, cx - 5, sy + 7,
                         fill="#c0392b")

    def _bill(self, x, y, f):
        w = 3 + 10 * abs(math.cos(f / 6))
        self.canvas.create_rectangle(x - w, y - 6, x + w, y + 6, fill="#85bb65", outline="#2e6b30")
        if w > 7:
            self.canvas.create_text(x, y, text="$", fill="#1e4620", font=("Segoe UI", 7, "bold"))

    def _draw_happy(self, f):
        c = self.canvas
        for i in range(9):
            x = (i * 41 + 13) % self.W + 6 * math.sin((f + i * 7) / 5)
            y = (f * 2.5 + i * 31) % (self.H + 30) - 15
            self._bill(x, y, f + i * 5)

        s = math.sin(f / 4)
        bob = 4 * abs(s)
        cx = 90 + 4 * s
        hcy, sy, hy = 40 - bob, 56 - bob, 84 - bob
        # Legs: alternate kicking feet.
        for side, lift in ((-1, max(0, s)), (1, max(0, -s))):
            fx, fy = cx + side * 12 + 4 * s, 114 - 10 * lift
            self._limb(cx + side * 5, hy, cx + side * 9 + 2 * s, (hy + fy) / 2 - 3 * lift, fx, fy)
            c.create_oval(fx - 6 + 2 * side, fy - 3, fx + 6 + 2 * side, fy + 3, fill="black")
        self._body(cx, sy, hy)
        # Arms waving overhead.
        a = math.sin(f / 3)
        for side, wave in ((-1, a), (1, -a)):
            hx, hyy = cx + side * 24, sy - 18 + 8 * wave
            self._limb(cx + side * 10, sy + 2, cx + side * 20, sy - 2, hx, hyy)
            c.create_oval(hx - 3, hyy - 3, hx + 3, hyy + 3, fill=self.SKIN, outline="")
        self._bill(cx + 26, sy - 24 - 8 * a, 0)

        c.create_oval(cx - 11, hcy - 11, cx + 11, hcy + 11, fill=self.SKIN, outline="#c99a5b")
        c.create_oval(cx - 5, hcy - 3, cx - 3, hcy - 1, fill="black")
        c.create_oval(cx + 3, hcy - 3, cx + 5, hcy - 1, fill="black")
        c.create_oval(cx, hcy - 6, cx + 8, hcy + 2, outline=self.GOLD, width=2)
        c.create_line(cx + 8, hcy - 2, cx + 11, hcy + 12, fill=self.GOLD)
        c.create_line(cx - 7, hcy + 4, cx - 3, hcy + 2, cx, hcy + 3, cx + 3, hcy + 2, cx + 7, hcy + 4,
                      fill="#5a3a1a", width=2, smooth=True)
        c.create_arc(cx - 5, hcy + 1, cx + 5, hcy + 9, start=200, extent=140, style="arc", width=2)
        self._hat(cx, hcy - 29, 0.15 * s)

    def _draw_sad(self, f):
        c = self.canvas
        cx = 90 + 1.2 * math.sin(f * 1.5)
        hcy, sy, hy = 50, 64, 90
        pw = 34 + 3 * math.sin(f / 8)
        c.create_oval(cx - pw, 110, cx + pw, 118, fill="#9fd3f5", outline="")
        for side in (-1, 1):
            self._limb(cx + side * 5, hy, cx + side * 8, 114)
            c.create_oval(cx + side * 8 - 6, 111, cx + side * 8 + 6, 117, fill="black")
        self._body(cx, sy, hy)

        c.create_oval(cx - 11, hcy - 11, cx + 11, hcy + 11, fill=self.SKIN, outline="#c99a5b")
        for side in (-1, 1):
            ex = cx + side * 4
            c.create_arc(ex - 3, hcy - 4, ex + 3, hcy, start=180, extent=180, style="arc", width=2)
        c.create_line(cx - 7, hcy + 5, cx - 3, hcy + 3, cx + 3, hcy + 3, cx + 7, hcy + 5,
                      fill="#5a3a1a", width=2, smooth=True)
        c.create_arc(cx - 5, hcy + 5, cx + 5, hcy + 12, start=20, extent=140, style="arc", width=2)
        # Tears streaming outward from both eyes.
        for i in range(4):
            d = (f * 1.8 + i * 9) % 60
            for side in (-1, 1):
                tx, ty = cx + side * (6 + d * 0.5), hcy - 1 + d
                c.create_oval(tx - 2, ty - 3, tx + 2, ty + 3, fill="#3a8ee6", outline="")
        # Hands wiping the cheeks.
        for side in (-1, 1):
            hx = cx + side * (13 + 2 * math.sin(f / 3))
            self._limb(cx + side * 10, sy + 2, cx + side * 20, sy + 12, hx, hcy + 7)
            c.create_oval(hx - 3, hcy + 4, hx + 3, hcy + 10, fill=self.SKIN, outline="")
        self._hat(cx - 2, hcy - 29, -0.35)


class StockApp:
    def __init__(self, root):
        self.root = root
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.theme_dark = None
        self.results = queue.Queue()
        self.data = None
        self.ticker = None
        self.refresh_job = None
        self.hover = {}
        self.live_results = queue.Queue()
        self.live_job = None
        self.live_gen = 0
        self.last_price = None
        self.next_chart_update: float | None = None
        self.next_live_update: float | None = None
        self.chart_updating = False
        self.live_updating = False
        root.title("Stock Price & Charts")
        root.geometry("900x680")
        root.minsize(600, 480)

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Ticker:", font=("Segoe UI", 11)).pack(side="left")
        self.ticker_var = tk.StringVar()
        self.entry = ttk.Entry(top, textvariable=self.ticker_var, width=12, font=("Segoe UI", 11))
        self.entry.pack(side="left", padx=6)
        self.entry.bind("<Return>", lambda _e: self.lookup())
        self.button = ttk.Button(top, text="Get Quote", command=self.lookup)
        self.button.pack(side="left")
        self.status = ttk.Label(top, text="", style="Muted.TLabel")
        self.status.pack(side="left", padx=10)

        head = ttk.Frame(root, padding=(10, 0))
        head.pack(fill="x")
        self.rich_guy = RichGuy(head)
        self.rich_guy.canvas.pack(side="right")
        self.name_label = ttk.Label(head, text="Enter a stock ticker above (e.g. GRMN).",
                                    font=("Segoe UI", 14, "bold"))
        self.name_label.pack(anchor="w")
        price_row = ttk.Frame(head)
        price_row.pack(anchor="w")
        self.price_label = ttk.Label(price_row, text="", font=("Segoe UI", 24, "bold"))
        self.price_label.pack(side="left")
        self.change_label = ttk.Label(price_row, text="", font=("Segoe UI", 13))
        self.change_label.pack(side="left", padx=10)
        self.live_label = ttk.Label(price_row, text="", font=("Segoe UI", 10, "bold"))
        self.live_label.pack(side="left", padx=10)
        self.open_label = ttk.Label(head, text="", font=("Segoe UI", 11))
        self.open_label.pack(anchor="w")
        self.time_label = ttk.Label(head, text="", style="Muted.TLabel")
        self.cap_label = ttk.Label(head, text="", font=("Segoe UI", 11))
        self.cap_label.pack(anchor="w")
        self.time_label.pack(anchor="w")
        self.countdown_label = ttk.Label(head, text="", style="Muted.TLabel",
                         font=("Consolas", 10), wraplength=380)
        self.countdown_label.pack(anchor="w", pady=(4, 0))

        grid = ttk.Frame(root, padding=10)
        grid.pack(fill="both", expand=True)
        self.canvases = {}
        for i, (title, *_rest) in enumerate(CHARTS):
            c = tk.Canvas(grid, bg="white", width=100, height=100,
                          highlightthickness=1, highlightbackground="#ccc")
            if i == 0:
                c.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=4, pady=4)
            else:
                c.grid(row=1, column=i - 1, sticky="nsew", padx=4, pady=4)
            c.bind("<Configure>", lambda _e: self.redraw())
            c.bind("<Motion>", lambda e, t=title: self.show_hover(t, e.x))
            c.bind("<Leave>", lambda _e, t=title: self.hide_hover(t))
            self.canvases[title] = c
        grid.rowconfigure(0, weight=3)
        grid.rowconfigure(1, weight=2)
        for i in range(3):
            grid.columnconfigure(i, weight=1, uniform="charts")

        self.check_system_theme()
        self.update_countdown()
        self.entry.focus_set()

    def update_countdown(self):
        now = time.monotonic()
        parts = []
        for label, deadline, updating in (
            ("Live price", self.next_live_update, self.live_updating),
            ("Charts", self.next_chart_update, self.chart_updating),
        ):
            if updating:
                parts.append(f"{label}: updating...")
            elif deadline is not None:
                seconds = max(0, math.ceil(deadline - now))
                parts.append(f"{label}: {seconds // 60:02d}:{seconds % 60:02d}")
            elif self.ticker:
                parts.append(f"{label}: not scheduled")
        self.countdown_label.config(text="  |  ".join(parts))
        if self.data:
            self.update_market_status(self.data["meta"])
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
                   "error": "#ff9999", "selection": "#0067c0", "grid": "#404040",
                   "up": "#65d984", "down": "#ff8080", "hover": "#80baff"} if dark else
                  {"bg": "#f0f0f0", "fg": "#202020", "field": "#ffffff",
                   "muted": "#606060", "border": "#b0b0b0", "active": "#e0e0e0",
                   "error": "#b00020", "selection": "#0067c0", "grid": "#e4e4e4",
                   "up": UP, "down": DOWN, "hover": "#1f5fbf"})
        self.colors = colors
        self.root.config(bg=colors["bg"])
        self.style.configure(".", background=colors["bg"], foreground=colors["fg"],
                             bordercolor=colors["border"], lightcolor=colors["border"],
                             darkcolor=colors["border"], troughcolor=colors["bg"])
        self.style.configure("TFrame", background=colors["bg"])
        self.style.configure("TLabel", background=colors["bg"], foreground=colors["fg"])
        self.style.configure("Muted.TLabel", foreground=colors["muted"])
        self.style.configure("Error.TLabel", foreground=colors["error"])
        self.style.configure("Up.TLabel", foreground=colors["up"])
        self.style.configure("Down.TLabel", foreground=colors["down"])
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
        self.rich_guy.canvas.config(background=colors["bg"])
        self.rich_guy.SUIT = "#a0a0a0" if dark else "#222222"
        for canvas in self.canvases.values():
            canvas.theme_colors = colors
            canvas.config(background=colors["field"], highlightbackground=colors["border"])
        self.redraw()
        set_title_bar_theme(self.root, dark)

    def lookup(self):
        ticker = self.ticker_var.get().strip().upper()
        if not re.fullmatch(r"[A-Z0-9.\-^=]{1,15}", ticker):
            self.status.config(text="Please enter a valid ticker symbol.", style="Error.TLabel")
            return
        self.ticker = ticker
        self.last_price = None
        self.live_gen += 1
        if self.live_job:
            self.root.after_cancel(self.live_job)
        self.live_updating = False
        self.next_live_update = time.monotonic() + LIVE_MS / 1000
        self.live_job = self.root.after(LIVE_MS, self.live_tick, self.live_gen)
        self.start_fetch()

    def live_tick(self, gen):
        if gen != self.live_gen:
            return
        self.live_job = None
        self.next_live_update = None
        self.live_updating = True
        threading.Thread(target=self._live_worker, args=(self.ticker, gen), daemon=True).start()
        self.root.after(100, self._live_poll)

    def _live_worker(self, ticker, gen):
        try:
            meta, points = get_chart(ticker, "1d", "1m")
        except Exception:
            meta, points = None, None
        self.live_results.put((gen, meta, points))

    def _live_poll(self):
        try:
            gen, meta, points = self.live_results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._live_poll)
            return
        if gen != self.live_gen:
            return
        if meta and self.data and self.data["meta"]["symbol"] == meta.get("symbol"):
            self.data["meta"] = meta
            self.data["charts"]["Intraday"] = points
            self.update_price(meta)
            self.redraw()
        self.live_updating = False
        self.next_live_update = time.monotonic() + LIVE_MS / 1000
        self.live_job = self.root.after(LIVE_MS, self.live_tick, gen)

    def start_fetch(self):
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        self.next_chart_update = None
        self.chart_updating = True
        self.button.config(state="disabled")
        self.status.config(text="Loading...", style="Muted.TLabel")
        threading.Thread(target=self._worker, args=(self.ticker,), daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, ticker):
        try:
            self.results.put(("ok", ticker, fetch_stock(ticker)))
        except ValueError as e:
            self.results.put(("err", ticker, str(e)))
        except urllib.error.URLError as e:
            self.results.put(("err", ticker, f"Network error: {getattr(e, 'reason', e)}"))
        except Exception as e:
            self.results.put(("err", ticker, f"Unexpected error: {e}"))

    def _poll(self):
        try:
            kind, ticker, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self.button.config(state="normal")
        if ticker != self.ticker:
            return
        self.chart_updating = False
        if kind == "err":
            self.status.config(text=payload, style="Error.TLabel")
            return
        self.data = payload
        self.status.config(text="Updated " + datetime.now().strftime("%I:%M:%S %p") +
                           " (charts refresh every minute)", style="Muted.TLabel")
        self.show()
        self.next_chart_update = time.monotonic() + REFRESH_MS / 1000
        self.refresh_job = self.root.after(REFRESH_MS, self.start_fetch)

    def show(self):
        meta = self.data["meta"]
        name = meta.get("longName") or meta.get("shortName") or meta["symbol"]
        self.name_label.config(text=f'{name} ({meta["symbol"]})')
        self.update_price(meta)
        self.redraw()

    def update_price(self, meta):
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        currency = meta.get("currency", "")
        self.price_label.config(text=f"{price:,.2f} {currency}" if price is not None else "—")
        self.open_label.config(text=f"Previous close: {prev:,.2f} {currency}".rstrip()
                   if prev is not None else "Previous close: unavailable")
        if price is not None and prev:
            diff = price - prev
            self.change_label.config(text=f"{diff:+,.2f} ({diff / prev * 100:+.2f}%) today",
                                     style="Up.TLabel" if diff >= 0 else "Down.TLabel")
            self.rich_guy.set("happy" if diff > 0 else "sad" if diff < 0 else None)
        else:
            self.change_label.config(text="", style="TLabel")
            self.rich_guy.set(None)

        shares = self.data.get("shares") if self.data else None
        self.cap_label.config(text=f"Market cap: {format_big(price * shares)} {currency}"
                              if shares and price is not None else "")

        if price is not None and self.last_price is not None and price != self.last_price:
            self.price_label.config(style="Up.TLabel" if price > self.last_price else "Down.TLabel")
            self.root.after(1000, lambda: self.price_label.config(style="TLabel"))
        self.last_price = price

        ts = meta.get("regularMarketTime")
        self.time_label.config(text=f"As of {datetime.fromtimestamp(ts):%a %b %d, %I:%M:%S %p}" if ts else "")
        self.update_market_status(meta)

    def update_market_status(self, meta):
        status = market_session_status(meta, time.time())
        self.live_label.config(text="\u25cf " + status,
                               style="Up.TLabel" if status == "Market open" else "Muted.TLabel")

    def redraw(self):
        if not self.data:
            return
        meta = self.data["meta"]
        for title, _rng, _interval, fmt in CHARTS:
            points = self.data["charts"][title]
            if title == "Intraday":
                points, x_range = intraday_view(meta, points)
                draw_chart(self.canvases[title], title, points, fmt, meta.get("chartPreviousClose"), x_range)
            else:
                draw_chart(self.canvases[title], title, points, fmt)
            self.show_hover(title)

    def show_hover(self, title, x=None):
        c = self.canvases[title]
        c.delete("hover")
        if x is not None:
            self.hover[title] = x
        x = self.hover.get(title)
        pts = getattr(c, "plot_points", [])
        if x is None or not pts:
            return
        left, top, right, bottom = c.plot_box
        if x < left or x > right or x > pts[-1][0] + 15:
            return

        i = bisect.bisect_left([p[0] for p in pts], x)
        if i == len(pts) or (i > 0 and x - pts[i - 1][0] < pts[i][0] - x):
            i -= 1
        px, py, t, price = pts[i]

        c.create_line(px, top, px, bottom, fill=self.colors["muted"], dash=(2, 2), tags="hover")
        c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=self.colors["hover"], outline=self.colors["field"], tags="hover")
        when = datetime.fromtimestamp(t).strftime(HOVER_FMT[title])
        label = f"{price:,.2f}   {when}" if title == "Intraday" else f"{price:,.2f}\n{when}"
        tid = c.create_text(px + 10, top + 4, anchor="nw", text=label,
                            font=("Segoe UI", 9, "bold"), fill=self.colors["fg"], tags="hover")
        if c.bbox(tid)[2] > right:
            c.itemconfig(tid, anchor="ne")
            c.coords(tid, px - 10, top + 4)
        x1, y1, x2, y2 = c.bbox(tid)
        if x1 < 4:
            c.move(tid, 4 - x1, 0)
            x1, y1, x2, y2 = c.bbox(tid)
        rect = c.create_rectangle(x1 - 4, y1 - 2, x2 + 4, y2 + 2,
                      fill=self.colors["field"], outline=self.colors["border"], tags="hover")
        c.tag_lower(rect, tid)

    def hide_hover(self, title):
        self.hover.pop(title, None)
        self.canvases[title].delete("hover")


if __name__ == "__main__":
    root = tk.Tk()
    StockApp(root)
    root.mainloop()

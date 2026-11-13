"""
MusicPad — пускает музыку (например, из Яндекс Музыки) в виртуальный микрофон
по горячей клавише, как Soundpad.

Схема (режим «Приложение», рекомендуется):
  Яндекс Музыка --(MusicPad перехватывает звук только этого приложения)-->
      [виртуальный микрофон "CABLE Input"] --> Discord / игра ("CABLE Output")
  Музыка по-прежнему играет у вас в обычные наушники, ничего перенаправлять
  в микшере Windows не нужно. Нужен только один бесплатный VB-Cable.

Подробности — в README.txt.
"""

import json
import os
import queue
import re
import threading
import tkinter as tk
import webbrowser
from contextlib import ExitStack
from tkinter import messagebox, ttk

import keyboard
import numpy as np
import sounddevice as sd

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:  # без pystray/Pillow приложение работает, просто без трея
    HAS_TRAY = False

try:
    # proc-tap 1.1.x exports ProcessAudioCapture (not ProcTap/StreamConfig).
    from proctap import ProcessAudioCapture
    HAS_PROCTAP = True
    PROCTAP_IMPORT_ERROR = None
except Exception as ex:  # без proc-tap работает только режим «Устройство»
    ProcessAudioCapture = None
    HAS_PROCTAP = False
    PROCTAP_IMPORT_ERROR = ex

try:
    from pycaw.pycaw import AudioUtilities
    HAS_PYCAW = True
except Exception:
    HAS_PYCAW = False

APP_NAME = "MusicPad"
SAMPLE_RATE = 48000
BLOCK = 480            # 10 мс
MAX_LAG_SEC = 0.15     # лишняя задержка в буфере отбрасывается
CABLE_URL = "https://vb-audio.com/Cable/"

CONFIG_VERSION = 2
CONFIG_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME, "config.json"
)

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    "source_mode": "app",         # app = звук приложения, device = звук с устройства
    "app_name": "",               # имя процесса, например "Яндекс Музыка.exe"
    "music_source": "CABLE Output",   # только режим device
    "monitor": "",                # только режим device; "" = вывод по умолчанию
    "virtual_mic": "CABLE Input", # виртуальный микрофон, который слышат Discord/игры
    "real_mic": "default",        # "default" = микрофон Windows, "" = не подмешивать
    "mode": "toggle",             # toggle | hold
    "volume": 100,                # % громкости музыки в микрофоне (0-200)
    "tray": True,                 # сворачивать в трей
    "hotkeys": {
        "gate": "f8",
        "playpause": "f9",
        "next": "f10",
        "prev": "f7",
        "vol_down": "f11",
        "vol_up": "f12",
    },
}
DEVICE_KEYS = ("source_mode", "app_name", "music_source", "monitor", "virtual_mic", "real_mic")

HOTKEY_ACTIONS = [
    ("gate", "Музыка в микрофон"),
    ("playpause", "Пауза / играть"),
    ("next", "Следующий трек"),
    ("prev", "Предыдущий трек"),
    ("vol_down", "Тише в микрофоне"),
    ("vol_up", "Громче в микрофоне"),
]

NONE_DEFAULT = "— по умолчанию —"
REAL_DEFAULT = "— микрофон по умолчанию —"
NONE_UNUSED = "— не подмешивать —"
# для этих списков: (значение в конфиге, подпись в списке)
SPECIAL = {
    "monitor": [("", NONE_DEFAULT)],
    "real_mic": [("default", REAL_DEFAULT), ("", NONE_UNUSED)],
}
VOLUME_STEP = 0.1

# состояние -> (цвет иконки в трее, подсказка)
TRAY_STATES = {
    "stopped": ("#888888", "MusicPad — остановлено"),
    "off": ("#c0392b", "MusicPad — музыка в микрофоне выкл"),
    "on": ("#1a9b3c", "MusicPad — музыка в микрофоне ВКЛ"),
}


def make_tray_icon(color):
    """Круглая иконка 64x64 с белой нотой; цвет показывает состояние."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color)
    d.ellipse((16, 38, 34, 52), fill="white")       # головка ноты
    d.rectangle((31, 14, 35, 45), fill="white")     # штиль
    d.polygon([(35, 14), (50, 22), (35, 29)], fill="white")  # флажок
    return img


# ----------------------------- конфиг -----------------------------

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("version") != CONFIG_VERSION:
            # старые настройки устройств (схема с двумя кабелями) не подходят
            for k in DEVICE_KEYS:
                saved.pop(k, None)
        hotkeys = saved.pop("hotkeys", {})
        cfg.update({k: v for k, v in saved.items() if k in cfg and k != "hotkeys"})
        cfg["hotkeys"].update({k: v for k, v in hotkeys.items() if k in cfg["hotkeys"]})
        cfg["version"] = CONFIG_VERSION
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ----------------------------- звук -------------------------------

class Ring:
    """Потокобезопасный кольцевой буфер с ограничением задержки."""

    def __init__(self, channels, seconds=1.0):
        self.channels = channels
        self.cap = int(seconds * SAMPLE_RATE)
        self.buf = np.zeros((self.cap, channels), dtype=np.float32)
        self.r = 0
        self.w = 0
        self.n = 0
        self.lock = threading.Lock()
        self.max_lag = int(MAX_LAG_SEC * SAMPLE_RATE)

    def write(self, data):
        with self.lock:
            m = len(data)
            if m >= self.cap:
                data = data[-self.cap:]
                m = self.cap
            over = self.n + m - self.cap
            if over > 0:
                self.r = (self.r + over) % self.cap
                self.n -= over
            end = self.w + m
            if end <= self.cap:
                self.buf[self.w:end] = data
            else:
                k = self.cap - self.w
                self.buf[self.w:] = data[:k]
                self.buf[:m - k] = data[k:]
            self.w = end % self.cap
            self.n += m

    def read(self, frames):
        out = np.zeros((frames, self.channels), dtype=np.float32)
        with self.lock:
            excess = self.n - frames - self.max_lag
            if excess > 0:
                self.r = (self.r + excess) % self.cap
                self.n -= excess
            if self.n < frames:
                return out
            end = self.r + frames
            if end <= self.cap:
                out[:] = self.buf[self.r:end]
            else:
                k = self.cap - self.r
                out[:k] = self.buf[self.r:]
                out[k:] = self.buf[:frames - k]
            self.r = end % self.cap
            self.n -= frames
        return out


def wasapi_index():
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return i
    return None


def list_devices(kind):
    """Имена устройств ввода/вывода (только WASAPI, если он есть)."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    wasapi = wasapi_index()
    result = []
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and (wasapi is None or d["hostapi"] == wasapi):
            result.append((d["name"], i))
    return result


def match_name(names, wanted):
    """Точное совпадение имени, затем подстрока без учёта регистра."""
    if not wanted or wanted == "default":
        return None
    for n in names:
        if n == wanted:
            return n
    for n in names:
        if wanted.lower() in n.lower():
            return n
    return None


def resolve_device(name, kind):
    devices = list_devices(kind)
    found = match_name([n for n, _ in devices], name)
    if found is None:
        return None
    return next(idx for n, idx in devices if n == found)


def default_device(kind):
    wasapi = wasapi_index()
    if wasapi is not None:
        api = sd.query_hostapis()[wasapi]
        idx = api["default_output_device" if kind == "output" else "default_input_device"]
        if idx >= 0:
            return idx
    idx = sd.default.device[1 if kind == "output" else 0]
    return idx if idx is not None and idx >= 0 else None


def extra_for(dev):
    """auto_convert даёт WASAPI самому пересэмплировать под нашу частоту."""
    if sd.query_devices(dev)["hostapi"] == wasapi_index():
        return sd.WasapiSettings(auto_convert=True)
    return None


def pick_virtual(outputs):
    """Выбрать виртуальный кабель среди устройств вывода (или '')."""
    for key in ("cable input", "voicemeeter input"):
        for n in outputs:
            if key in n.lower():
                return n
    for n in outputs:
        if "vb-audio" in n.lower() or "virtual" in n.lower():
            return n
    return ""


def is_virtual(name):
    low = name.lower()
    return any(k in low for k in ("cable", "voicemeeter", "vb-audio", "virtual"))


def mic_name_for(output_name):
    """«CABLE Input (...)» -> «CABLE Output (...)» — этот микрофон выбирать в Discord."""
    return re.sub("Input", "Output", output_name, count=1)


# ------------------- звук отдельных приложений --------------------

def audio_sessions():
    """[(имя_процесса, pid, активна)] для процессов с аудио-сессиями Windows."""
    if not HAS_PYCAW:
        return []
    result = []
    try:
        for s in AudioUtilities.GetAllSessions():
            pid = s.ProcessId
            if not pid:
                continue
            try:
                name = s.Process.name()
            except Exception:
                continue
            result.append((name, pid, s.State == 1))
    except Exception:
        return []
    return result


def find_app_pid(name):
    """PID процесса приложения, который сейчас выводит звук (активные — в приоритете).

    У Electron-приложений (в том числе Яндекс Музыки) звук идёт не из главного
    процесса, а из дочернего, поэтому ищем именно аудио-сессию.
    """
    if not name:
        return None
    candidates = [(pid, active) for n, pid, active in audio_sessions()
                  if n.lower() == name.lower()]
    if not candidates:
        return None
    candidates.sort(key=lambda c: not c[1])
    return candidates[0][0]


class Engine:
    def __init__(self):
        self.stack = None
        self.running = False
        self.gate = False
        self.vol = 1.0
        self.peak = 0.0
        self.info = ""            # строка состояния захвата для окна
        self._gain = 0.0
        self.music_to_mic = None
        self.tap = None
        self.cur_pid = None
        self.app_name = ""
        self._stop_evt = threading.Event()
        self._watcher = None

    def toggle_gate(self):
        self.gate = not self.gate

    def set_gate(self, value):
        self.gate = bool(value)

    def change_vol(self, delta):
        self.vol = float(np.clip(self.vol + delta, 0.0, 2.0))

    # ---- захват звука приложения ----

    def _on_pcm(self, pcm, frames=None):
        """Колбэк proc-tap: float32, стерео, 48 кГц, чередующиеся каналы."""
        ring = self.music_to_mic
        if ring is None:
            return
        usable = len(pcm) // 8 * 8  # целые стереокадры float32 (по 8 байт)
        if usable == 0:
            return
        data = np.frombuffer(pcm, dtype=np.float32, count=usable // 4).reshape(-1, 2)
        self.peak = max(self.peak, float(np.abs(data).max()))
        ring.write(data)

    def _close_tap(self):
        tap, self.tap = self.tap, None
        self.cur_pid = None
        if tap is not None:
            for method in ("stop", "close"):
                try:
                    getattr(tap, method)()
                except Exception:
                    pass

    def _try_tap(self, pid):
        self._close_tap()
        try:
            tap = ProcessAudioCapture(pid, on_data=self._on_pcm)
            tap.start()
        except Exception as ex:
            self.info = f"Ошибка захвата звука: {ex}"
            return
        self.tap = tap
        self.cur_pid = pid
        self.info = f"Захват звука: {self.app_name} (pid {pid})"

    def _watch(self):
        """Следит за приложением: подключается, когда оно начинает играть, и
        переподключается, если аудио-процесс сменился (например, после перезапуска)."""
        com = None
        try:
            import comtypes
            comtypes.CoInitialize()
            com = comtypes
        except Exception:
            pass
        try:
            while not self._stop_evt.wait(2.0):
                pid = find_app_pid(self.app_name)
                if pid and pid != self.cur_pid:
                    self._try_tap(pid)
        finally:
            if com is not None:
                try:
                    com.CoUninitialize()
                except Exception:
                    pass

    def _begin_app_capture(self, name):
        self.app_name = name
        self.cur_pid = None
        self.info = f"Жду звук от «{name}»… Включите в нём музыку."
        self._stop_evt.clear()
        pid = find_app_pid(name)
        if pid:
            self._try_tap(pid)
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    # ---- запуск / остановка ----

    def start(self, cfg):
        if self.running:
            return
        mode = cfg["source_mode"]

        vmic = resolve_device(cfg["virtual_mic"], "output") if cfg["virtual_mic"] else None
        if vmic is None:
            raise RuntimeError(
                "Не найден виртуальный микрофон.\n"
                "Установите бесплатный VB-Cable (кнопка в окне), перезагрузите "
                "компьютер и нажмите «Обновить устройства»."
            )

        mic = None
        if cfg["real_mic"] == "default":
            mic = default_device("input")
        elif cfg["real_mic"]:
            mic = resolve_device(cfg["real_mic"], "input")
            if mic is None:
                raise RuntimeError(f'Не найден микрофон "{cfg["real_mic"]}".')

        src = mon = None
        app_name = ""
        if mode == "device":
            src = resolve_device(cfg["music_source"], "input")
            if src is None:
                raise RuntimeError(
                    f'Не найден источник музыки "{cfg["music_source"]}".\n'
                    "Выберите устройство из списка (обычно CABLE Output) "
                    "или переключитесь на режим «Приложение»."
                )
            mon = resolve_device(cfg["monitor"], "output") if cfg["monitor"] else default_output()
            if mon is None:
                raise RuntimeError("Не найдено устройство для наушников.")
            if sd.query_devices(src)["name"] == sd.query_devices(vmic)["name"]:
                raise RuntimeError("Источник музыки и виртуальный микрофон — одно устройство.")
        else:
            if not HAS_PROCTAP:
                raise RuntimeError(
                    "Не удалось импортировать proc-tap.\n"
                    f"Причина: {PROCTAP_IMPORT_ERROR!r}\n"
                    "Переустановите зависимость командой: "
                    "py -m pip install --force-reinstall proc-tap==1.1.1"
                )
            if not HAS_PYCAW:
                raise RuntimeError("Не установлен модуль pycaw (pip install pycaw).")
            app_name = cfg["app_name"].strip()
            if not app_name:
                raise RuntimeError(
                    "Выберите приложение, звук которого отправлять в микрофон.\n"
                    "Включите музыку в плеере и нажмите «Обновить» рядом со списком."
                )

        self.vol = cfg["volume"] / 100.0
        self.gate = False
        self._gain = 0.0
        self.peak = 0.0
        self.info = ""
        music_to_mic = Ring(2)
        self.music_to_mic = music_to_mic
        music_to_mon = Ring(2) if mode == "device" else None
        mic_ring = Ring(1) if mic is not None else None

        def src_cb(indata, frames, t, status):
            data = indata if src_ch == 2 else np.repeat(indata, 2, axis=1)
            if frames:
                self.peak = max(self.peak, float(np.abs(data).max()))
            music_to_mon.write(data)
            music_to_mic.write(data)

        def mon_cb(outdata, frames, t, status):
            outdata[:] = music_to_mon.read(frames)

        def vmic_cb(outdata, frames, t, status):
            target = self.vol if self.gate else 0.0
            ramp = np.linspace(self._gain, target, frames, dtype=np.float32)[:, None]
            self._gain = target  # плавное включение, без щелчков
            mix = music_to_mic.read(frames) * ramp
            if mic_ring is not None:
                mix = mix + mic_ring.read(frames)
            np.clip(mix, -1.0, 1.0, out=mix)
            outdata[:] = mix

        def mic_cb(indata, frames, t, status):
            mic_ring.write(indata[:, :1])

        common = dict(samplerate=SAMPLE_RATE, blocksize=BLOCK, dtype="float32", latency="low")
        stack = ExitStack()
        try:
            if mode == "device":
                src_ch = max(1, min(2, sd.query_devices(src)["max_input_channels"]))
                stack.enter_context(sd.InputStream(
                    device=src, channels=src_ch, callback=src_cb,
                    extra_settings=extra_for(src), **common))
                stack.enter_context(sd.OutputStream(
                    device=mon, channels=2, callback=mon_cb,
                    extra_settings=extra_for(mon), **common))
            stack.enter_context(sd.OutputStream(
                device=vmic, channels=2, callback=vmic_cb,
                extra_settings=extra_for(vmic), **common))
            if mic is not None:
                stack.enter_context(sd.InputStream(
                    device=mic, channels=1, callback=mic_cb,
                    extra_settings=extra_for(mic), **common))
        except Exception as ex:
            stack.close()
            raise RuntimeError(f"Не удалось открыть аудиоустройства:\n{ex}")
        self.stack = stack
        self.running = True
        if mode == "app":
            self._begin_app_capture(app_name)

    def stop(self):
        self._stop_evt.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher.is_alive() and watcher is not threading.current_thread():
            watcher.join(timeout=3)
        self._close_tap()
        if self.stack is not None:
            self.stack.close()
            self.stack = None
        self.running = False
        self.gate = False
        self._gain = 0.0
        self.info = ""


def default_output():
    return default_device("output")


# ----------------------------- окно -------------------------------

class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.engine = Engine()
        self.capturing = False
        self.combos = {}
        self.hk_buttons = {}
        self.disable_when_running = []
        self.ui_queue = queue.Queue()   # команды из потока трея -> основной поток
        self.tray = None
        self.tray_icons = {}
        self.tray_state = None
        self.tray_notified = False
        self.closing = False

        root.title("MusicPad")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        root.bind("<Unmap>", self.on_unmap)
        pad = {"padx": 10, "pady": 6}

        # --- источник музыки ---
        src = ttk.LabelFrame(root, text="1. Что отправлять в микрофон")
        src.grid(row=0, column=0, sticky="ew", **pad)
        self.src_mode_var = tk.StringVar(value=self.cfg["source_mode"])
        r1 = ttk.Radiobutton(src, text="Звук приложения (рекомендуется)",
                             variable=self.src_mode_var, value="app",
                             command=self.on_source_mode)
        r1.grid(row=0, column=0, sticky="w", padx=8, pady=(4, 0))
        r2 = ttk.Radiobutton(src, text="Звук с устройства (кабель, для опытных)",
                             variable=self.src_mode_var, value="device",
                             command=self.on_source_mode)
        r2.grid(row=1, column=0, sticky="w", padx=8)
        self.disable_when_running += [r1, r2]

        self.app_frame = ttk.Frame(src)
        self.app_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(4, 0))
        ttk.Label(self.app_frame, text="Приложение:").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(self.app_frame, width=40)
        cb.grid(row=0, column=1, padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.collect_cfg())
        cb.bind("<FocusOut>", lambda e: self.collect_cfg())
        self.combos["app_name"] = cb
        self.apps_btn = ttk.Button(self.app_frame, text="Обновить", width=10,
                                   command=self.refresh_apps)
        self.apps_btn.grid(row=0, column=2)
        self.disable_when_running.append(self.apps_btn)
        self.src_info = tk.Label(src, text="", anchor="w", justify="left", wraplength=470,
                                 fg="#666")
        self.src_info.grid(row=3, column=0, sticky="w", padx=8, pady=(2, 6))

        self.dev_frame = ttk.Frame(src)
        self.dev_frame.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 6))
        self.add_combo(self.dev_frame, 0, "music_source", "Источник музыки (куда играет плеер):")
        self.add_combo(self.dev_frame, 2, "monitor", "Мои наушники (чтобы слышать музыку):")

        # --- куда отправлять ---
        dst = ttk.LabelFrame(root, text="2. Куда отправлять")
        dst.grid(row=1, column=0, sticky="ew", **pad)
        self.add_combo(dst, 0, "virtual_mic", "Виртуальный микрофон (вход кабеля):", padx=8)
        self.virtual_hint = tk.Label(dst, text="", anchor="w", justify="left", wraplength=470)
        self.virtual_hint.grid(row=2, column=0, sticky="w", padx=8, pady=(2, 0))
        self.cable_btn = ttk.Button(dst, text="Скачать VB-Cable (бесплатно)",
                                    command=lambda: webbrowser.open(CABLE_URL))
        self.cable_btn.grid(row=3, column=0, sticky="w", padx=8, pady=(2, 0))
        self.add_combo(dst, 4, "real_mic", "Мой микрофон (чтобы собеседники слышали и вас):", padx=8)
        self.devices_btn = ttk.Button(dst, text="Обновить устройства", command=self.refresh_devices)
        self.devices_btn.grid(row=6, column=0, sticky="e", padx=8, pady=6)
        self.disable_when_running.append(self.devices_btn)

        # --- режим ---
        mode = ttk.LabelFrame(root, text="Режим клавиши «Музыка в микрофон»")
        mode.grid(row=2, column=0, sticky="ew", **pad)
        self.mode_var = tk.StringVar(value=self.cfg["mode"])
        ttk.Radiobutton(mode, text="Нажал — включилось, нажал ещё раз — выключилось",
                        variable=self.mode_var, value="toggle",
                        command=self.on_mode_change).grid(row=0, column=0, sticky="w", padx=8, pady=2)
        ttk.Radiobutton(mode, text="Играет, пока клавиша зажата",
                        variable=self.mode_var, value="hold",
                        command=self.on_mode_change).grid(row=1, column=0, sticky="w", padx=8, pady=2)

        # --- хоткеи ---
        hk = ttk.LabelFrame(root, text="Горячие клавиши (нажмите кнопку и затем нужную клавишу)")
        hk.grid(row=3, column=0, sticky="ew", **pad)
        for r, (action, label) in enumerate(HOTKEY_ACTIONS):
            ttk.Label(hk, text=label).grid(row=r, column=0, sticky="w", padx=8, pady=2)
            btn = ttk.Button(hk, width=18, text=self.cfg["hotkeys"][action].upper(),
                             command=lambda a=action: self.capture_hotkey(a))
            btn.grid(row=r, column=1, sticky="e", padx=8, pady=2)
            self.hk_buttons[action] = btn
        hk.columnconfigure(0, weight=1)

        # --- громкость и уровень ---
        vol = ttk.LabelFrame(root, text="Музыка в микрофоне")
        vol.grid(row=4, column=0, sticky="ew", **pad)
        ttk.Label(vol, text="Громкость:").grid(row=0, column=0, sticky="w", padx=8, pady=2)
        self.vol_var = tk.DoubleVar(value=self.cfg["volume"])
        ttk.Scale(vol, from_=0, to=200, variable=self.vol_var, length=250,
                  command=self.on_volume).grid(row=0, column=1, padx=6)
        self.vol_label = ttk.Label(vol, width=6, text=f'{int(self.cfg["volume"])}%')
        self.vol_label.grid(row=0, column=2, padx=6)
        ttk.Label(vol, text="Уровень музыки:").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        self.level = ttk.Progressbar(vol, maximum=100, length=250)
        self.level.grid(row=1, column=1, columnspan=2, sticky="w", padx=6, pady=(2, 6))

        # --- трей ---
        self.tray_var = tk.BooleanVar(value=bool(self.cfg["tray"]) and HAS_TRAY)
        tray_cb = ttk.Checkbutton(
            root, variable=self.tray_var, command=self.collect_cfg,
            text="Сворачивать в трей (при сворачивании и закрытии окна)")
        tray_cb.grid(row=5, column=0, sticky="w", padx=14, pady=(0, 2))
        if not HAS_TRAY:
            tray_cb.config(state="disabled", text="Трей недоступен (нужны pystray и Pillow)")

        # --- статус и управление ---
        bottom = ttk.Frame(root)
        bottom.grid(row=6, column=0, sticky="ew", **pad)
        self.status = tk.Label(bottom, text="Остановлено", font=("Segoe UI", 12, "bold"), fg="#888")
        self.status.grid(row=0, column=0, sticky="w")
        bottom.columnconfigure(0, weight=1)
        self.gate_btn = ttk.Button(bottom, text="Музыка в микрофон", command=self.engine.toggle_gate,
                                   state="disabled")
        self.gate_btn.grid(row=0, column=1, padx=6)
        self.start_btn = ttk.Button(bottom, text="Запустить", command=self.toggle_running)
        self.start_btn.grid(row=0, column=2)

        self.refresh_devices()
        self.refresh_apps()
        self.apply_source_mode_layout()
        self.start_tray()
        self.tick()

    def add_combo(self, parent, row, key, label, padx=0):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=padx, pady=(4, 0))
        cb = ttk.Combobox(parent, state="readonly", width=58)
        cb.grid(row=row + 1, column=0, sticky="ew", padx=padx, pady=(0, 2))
        cb.bind("<<ComboboxSelected>>", lambda e: self.on_device_chosen())
        self.combos[key] = cb

    # ---- устройства / приложения / конфиг ----

    def fill_combo(self, key, names):
        specials = SPECIAL.get(key, [])
        values = [label for _, label in specials] + names
        cb = self.combos[key]
        cb["values"] = values
        wanted = self.cfg[key]
        for val, label in specials:
            if wanted == val:
                cb.set(label)
                return
        found = match_name(names, wanted)
        if found:
            cb.set(found)
        elif key == "music_source":
            cb.set(next((n for n in names if "cable output" in n.lower()), ""))
        elif key == "virtual_mic":
            cb.set(pick_virtual(names))
        else:
            cb.set(values[0] if values else "")

    def refresh_devices(self):
        try:
            inputs = [n for n, _ in list_devices("input")]
            outputs = [n for n, _ in list_devices("output")]
        except Exception as ex:
            messagebox.showerror("Ошибка", f"Не удалось получить список устройств:\n{ex}")
            return
        self.fill_combo("music_source", inputs)
        self.fill_combo("monitor", outputs)
        self.fill_combo("virtual_mic", outputs)
        self.fill_combo("real_mic", inputs)
        self.collect_cfg()
        self.update_virtual_hint()

    def refresh_apps(self):
        sessions = sorted(audio_sessions(), key=lambda s: (not s[2], s[0].lower()))
        names = []
        for name, _, _ in sessions:
            if name not in names:
                names.append(name)
        cb = self.combos["app_name"]
        cb["values"] = names
        current = cb.get().strip() or self.cfg["app_name"]
        lowered = {n.lower(): n for n in names}
        if current and current.lower() in lowered:
            cb.set(lowered[current.lower()])
        else:
            guess = next((n for n in names
                          if any(k in n.lower() for k in ("яндекс", "музык", "yandex"))), None)
            cb.set(guess or current or "")
        self.collect_cfg()

    def update_virtual_hint(self):
        name = self.combos["virtual_mic"].get()
        if name and is_virtual(name):
            self.virtual_hint.config(
                fg="#1a7f37",
                text=f"В Discord и играх выберите микрофон:  «{mic_name_for(name)}»")
            self.cable_btn.grid_remove()
        else:
            self.virtual_hint.config(
                fg="#c0392b",
                text="Виртуальный микрофон не найден. Установите бесплатный VB-Cable "
                     "(один раз), перезагрузите компьютер и нажмите «Обновить устройства».")
            self.cable_btn.grid()

    def on_device_chosen(self):
        self.collect_cfg()
        self.update_virtual_hint()

    def collect_cfg(self):
        for key in ("music_source", "monitor", "virtual_mic", "real_mic"):
            value = self.combos[key].get()
            for val, label in SPECIAL.get(key, []):
                if value == label:
                    value = val
                    break
            self.cfg[key] = value
        self.cfg["app_name"] = self.combos["app_name"].get().strip()
        self.cfg["source_mode"] = self.src_mode_var.get()
        self.cfg["mode"] = self.mode_var.get()
        self.cfg["volume"] = int(round(self.vol_var.get()))
        self.cfg["tray"] = bool(self.tray_var.get())
        save_config(self.cfg)

    def on_source_mode(self):
        self.collect_cfg()
        self.apply_source_mode_layout()

    def apply_source_mode_layout(self):
        if self.src_mode_var.get() == "app":
            self.app_frame.grid()
            self.dev_frame.grid_remove()
        else:
            self.app_frame.grid_remove()
            self.dev_frame.grid()

    # ---- трей ----

    def start_tray(self):
        if not HAS_TRAY or self.tray is not None:
            return
        try:
            self.tray_icons = {k: make_tray_icon(c) for k, (c, _) in TRAY_STATES.items()}
            q = self.ui_queue
            menu = pystray.Menu(
                pystray.MenuItem("Показать окно", lambda icon, item: q.put(self.show_window),
                                 default=True),
                pystray.MenuItem("Музыка в микрофон",
                                 lambda icon, item: q.put(self.engine.toggle_gate),
                                 checked=lambda item: self.engine.gate,
                                 enabled=lambda item: self.engine.running),
                pystray.MenuItem(
                    lambda item: "Остановить" if self.engine.running else "Запустить",
                    lambda icon, item: q.put(self.toggle_running)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda icon, item: q.put(self.on_close)),
            )
            self.tray = pystray.Icon(APP_NAME, self.tray_icons["stopped"],
                                     TRAY_STATES["stopped"][1], menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None  # трей не поднялся — работаем как обычное окно
            self.tray_var.set(False)

    def tray_enabled(self):
        return self.tray is not None and bool(self.tray_var.get())

    def hide_to_tray(self):
        self.root.withdraw()
        if not self.tray_notified:
            self.tray_notified = True
            try:
                self.tray.notify("MusicPad продолжает работать в трее.", APP_NAME)
            except Exception:
                pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_unmap(self, event):
        # кнопка «Свернуть» -> уходим в трей
        if event.widget is self.root and self.tray_enabled() and self.root.state() == "iconic":
            self.hide_to_tray()

    def on_close_button(self):
        if self.tray_enabled():
            self.hide_to_tray()
        else:
            self.on_close()

    # ---- громкость / режим ----

    def on_volume(self, _value=None):
        v = self.vol_var.get()
        self.engine.vol = v / 100.0
        self.vol_label.config(text=f"{int(round(v))}%")

    def on_mode_change(self):
        self.collect_cfg()
        if self.engine.running:
            self.register_hotkeys()

    # ---- горячие клавиши ----

    def register_hotkeys(self):
        keyboard.unhook_all()
        hk = self.cfg["hotkeys"]
        e = self.engine
        try:
            if self.cfg["mode"] == "hold":
                keyboard.add_hotkey(hk["gate"], lambda: e.set_gate(True))
                keyboard.add_hotkey(hk["gate"], lambda: e.set_gate(False), trigger_on_release=True)
            else:
                keyboard.add_hotkey(hk["gate"], e.toggle_gate)
            keyboard.add_hotkey(hk["vol_up"], lambda: e.change_vol(VOLUME_STEP))
            keyboard.add_hotkey(hk["vol_down"], lambda: e.change_vol(-VOLUME_STEP))
            keyboard.add_hotkey(hk["playpause"], lambda: keyboard.send("play/pause media"))
            keyboard.add_hotkey(hk["next"], lambda: keyboard.send("next track"))
            keyboard.add_hotkey(hk["prev"], lambda: keyboard.send("previous track"))
        except Exception as ex:
            messagebox.showerror("Ошибка горячих клавиш", str(ex))

    def capture_hotkey(self, action):
        if self.capturing:
            return
        self.capturing = True
        self.hk_buttons[action].config(text="Нажмите клавишу…")
        keyboard.unhook_all()

        def worker():
            try:
                combo = keyboard.read_hotkey(suppress=False)
            except Exception:
                combo = None
            self.root.after(0, lambda: self.finish_capture(action, combo))

        threading.Thread(target=worker, daemon=True).start()

    def finish_capture(self, action, combo):
        self.capturing = False
        if combo:
            self.cfg["hotkeys"][action] = combo
            save_config(self.cfg)
        self.hk_buttons[action].config(text=self.cfg["hotkeys"][action].upper())
        if self.engine.running:
            self.register_hotkeys()

    # ---- запуск / остановка ----

    def set_controls_running(self, running):
        for key, cb in self.combos.items():
            if running:
                cb.config(state="disabled")
            else:
                cb.config(state="normal" if key == "app_name" else "readonly")
        for widget in self.disable_when_running:
            widget.config(state="disabled" if running else "normal")
        self.start_btn.config(text="Остановить" if running else "Запустить")
        self.gate_btn.config(state="normal" if running else "disabled")

    def toggle_running(self):
        if self.engine.running:
            keyboard.unhook_all()
            self.engine.stop()
            self.set_controls_running(False)
            return
        self.collect_cfg()
        try:
            self.engine.start(self.cfg)
        except Exception as ex:
            self.show_window()  # запуск мог быть из трея — показываем окно с ошибкой
            messagebox.showerror("Не удалось запустить", str(ex))
            return
        self.register_hotkeys()
        self.set_controls_running(True)

    # ---- обновление интерфейса ----

    def update_src_info(self):
        e = self.engine
        if self.src_mode_var.get() != "app":
            self.src_info.config(text="")
        elif e.running and e.info:
            ok = e.info.startswith("Захват")
            self.src_info.config(text=e.info, fg="#1a7f37" if ok else "#b9770e")
        else:
            self.src_info.config(
                text="Включите музыку в плеере и нажмите «Обновить» — приложение "
                     "появится в списке. Название можно ввести и вручную.", fg="#666")

    def tick(self):
        # команды из меню трея выполняем в основном потоке
        while True:
            try:
                command = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                command()
            except Exception:
                pass
            if self.closing:  # команда «Выход» уже уничтожила окно
                return
        e = self.engine
        # иконка и подсказка в трее отражают состояние
        state = ("on" if e.gate else "off") if e.running else "stopped"
        if self.tray is not None and state != self.tray_state:
            self.tray_state = state
            try:
                self.tray.icon = self.tray_icons[state]
                self.tray.title = TRAY_STATES[state][1]
            except Exception:
                pass
        self.update_src_info()
        if e.running:
            if e.gate:
                self.status.config(text="Музыка в микрофоне: ВКЛ", fg="#1a9b3c")
            else:
                self.status.config(text="Музыка в микрофоне: выкл", fg="#c0392b")
            # громкость могла измениться хоткеем
            current = e.vol * 100.0
            if abs(current - self.vol_var.get()) > 0.5:
                self.vol_var.set(current)
                self.vol_label.config(text=f"{int(round(current))}%")
            self.level["value"] = min(100.0, e.peak * 100.0)
            e.peak = 0.0
        else:
            self.status.config(text="Остановлено", fg="#888")
            self.level["value"] = 0
        self.root.after(80, self.tick)

    def on_close(self):
        self.closing = True
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.engine.stop()
        self.collect_cfg()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

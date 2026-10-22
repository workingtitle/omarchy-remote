"""Bounded external-process adapter for GPU Screen Recorder experiments."""

import os
import re
import signal
import subprocess
import threading
from typing import Optional


_OUTPUT = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")

class GsrCapture:
    """Own one short-lived GSR process and expose its Matroska stdout FD."""

    def __init__(self, output: str, fps: int, bitrate_kbps: int,
                 width: int = 1280, height: int = 720,
                 executable: str = "gpu-screen-recorder") -> None:
        if not isinstance(output, str) or not _OUTPUT.fullmatch(output):
            raise ValueError("output must be an identifier of 1..64 characters")
        if isinstance(fps, bool) or not isinstance(fps, int) or not 10 <= fps <= 120:
            raise ValueError("fps must be an integer from 10 to 120")
        if (isinstance(bitrate_kbps, bool) or not isinstance(bitrate_kbps, int)
                or not 250 <= bitrate_kbps <= 50000):
            raise ValueError("bitrate_kbps must be an integer from 250 to 50000")
        for name, value, low, high in (("width", width, 64, 7680),
                                       ("height", height, 64, 4320)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        self.output, self.fps, self.bitrate_kbps = output, fps, bitrate_kbps
        self.width, self.height = width, height
        if not isinstance(executable, str) or not executable:
            raise ValueError("executable must be a non-empty string")
        self.executable = executable
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._watchdog: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None

    @property
    def stdoutfd(self) -> int:
        with self._lock:
            if self._process is None or self._process.stdout is None:
                raise RuntimeError("capture has not started")
            return self._process.stdout.fileno()

    @property
    def stdout_fd(self) -> int:
        return self.stdoutfd

    @property
    def returncode(self) -> Optional[int]:
        with self._lock:
            return None if self._process is None else self._process.poll()

    def start(self) -> "GsrCapture":
        env=os.environ.copy()
        env.setdefault('XDG_RUNTIME_DIR',f'/run/user/{os.getuid()}')
        env.setdefault('DBUS_SESSION_BUS_ADDRESS',f'unix:path=/run/user/{os.getuid()}/bus')
        # SSH exec does not inherit the graphical session's Wayland socket.
        # Bound both the duration and output of the local session lookup.
        data = self._load_session_environment()
        for line in data.decode('utf-8',errors='strict').splitlines():
            name,sep,value=line.partition('=')
            if name == 'WAYLAND_DISPLAY' and sep and _OUTPUT.fullmatch(value):
                env[name]=value
        if not env.get('WAYLAND_DISPLAY'):
            raise RuntimeError('Wayland session unavailable')
        command = [self.executable, "-w", self.output, "-s",
                   f"{self.width}x{self.height}", "-c", "matroska", "-o",
                   "/dev/stdout", "-f", str(self.fps), "-fm", "vfr", "-k",
                   "h264", "-encoder", "gpu", "-fallback-cpu-encoding", "no",
                   "-bm", "cbr", "-q", str(self.bitrate_kbps), "-ffmpeg-opts",
                   "cluster_time_limit=1;flush_packets=1;live=1", "-v", "no"]
        with self._lock:
            if self._process is not None:
                raise RuntimeError("capture already started")
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True,env=env)
            self._process = process
            self._stopped.clear()
        self._stderr_thread = threading.Thread(target=self._drain_stderr,
                                                name="gsr-stderr", daemon=True)
        self._stderr_thread.start()
        self._watchdog = threading.Thread(target=self._watchdog_stop,
                                           name="gsr-watchdog", daemon=True)
        self._watchdog.start()
        return self

    def _load_session_environment(self) -> bytes:
        with subprocess.Popen(['timeout','3s','systemctl','--user','show-environment'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as lookup:
            data=lookup.stdout.read(65537)
            if len(data)>65536:
                lookup.kill()
                raise RuntimeError('Session environment exceeds limit')
            if lookup.wait(timeout=1) != 0:
                raise RuntimeError('Graphical session environment unavailable')
        return data

    def _drain_stderr(self) -> None:
        with self._lock:
            process = self._process
        stream = process.stderr if process is not None else None
        if stream is None:
            return
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            # close() may release the pipe after its bounded join.
            pass
        finally:
            stream.close()

    def _watchdog_stop(self) -> None:
        if not self._stopped.wait(45.0):
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._process is None or self._stopped.is_set():
                return
            process = self._process
            self._stopped.set()
        # The parent can exit while a child still owns stdout/stderr.  Since
        # start_new_session=True gives this capture its own process group,
        # terminate that group whenever the drainer is still alive as well.
        group_active = process.poll() is None or (
            self._stderr_thread is not None and self._stderr_thread.is_alive())
        if group_active:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
            if process.poll() is None or (self._stderr_thread is not None and self._stderr_thread.is_alive()):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
        if self._watchdog is not None and threading.current_thread() is not self._watchdog:
            self._watchdog.join(timeout=0.1)
        if self._stderr_thread is not None and threading.current_thread() is not self._stderr_thread:
            # A detached descendant can retain stderr forever.  Do not let it
            # turn close() into an unbounded wait; the process group is already
            # terminated where possible, and the daemon thread is bounded.
            self._stderr_thread.join(timeout=0.2)
        if process.stdout is not None:
            process.stdout.close()
        # BufferedReader.close() takes the reader lock. Never take that lock
        # while a detached writer can keep the draining thread blocked.
        if process.stderr is not None and (self._stderr_thread is None or not self._stderr_thread.is_alive()):
            process.stderr.close()

    def __enter__(self) -> "GsrCapture":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

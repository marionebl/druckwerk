"""Unit tests that import camera.py and server.py in the test process: Live and Daemon.restart need no subprocess."""
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

for _d in ("..",):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), _d))
import camera  # noqa: E402


class Quiet(camera.Live):
    """A Live without its upstream thread."""

    def run(self):
        pass


def make(keep=False, serial="S1"):
    live = Quiet(SimpleNamespace(port=0), serial, "", keep=keep)
    live.first = 0.1
    return live


def test_idle_timer_does_not_close_a_live_that_got_a_client():
    live = make()
    camera.lives["S1"] = live
    live.leave(live.join())
    timer = live.idle_timer
    timer.cancel()
    c = live.join()  # joins in the moment between the timer firing and its callback taking the lock
    timer.function()
    assert not live.closed and c in live.clients and camera.lives.get("S1") is live
    live.close()


def test_idle_timer_removes_the_live_from_lives_with_the_close():
    live = make(serial="S2")
    camera.lives["S2"] = live
    live.leave(live.join())
    live.idle_timer.cancel()
    live.idle_timer.function()
    assert live.closed and "S2" not in camera.lives


def test_drop_survives_a_consumer_that_emptied_the_queue_first():
    class Racy(queue.Queue):
        def empty(self):
            return False  # the consumer took the last item between empty() and get_nowait()

        def get_nowait(self):
            raise queue.Empty

    live = make()
    c = Racy(maxsize=2048)
    live.clients.add(c)
    live.drop(c)
    assert c not in live.clients
    live.close()


def test_broadcast_arms_the_idle_timer_when_the_last_client_was_dropped():
    live = make()
    c = queue.Queue(maxsize=1)
    c.put(b"x")
    live.clients.add(c)
    live.broadcast(b"y")
    assert not live.clients and live.idle_timer is not None
    live.close()


def test_close_waits_for_ffmpeg_so_no_zombie_stays():
    live = make()
    live.proc = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    pid = live.proc.pid
    live.close()
    assert live.proc.returncode is not None and not os.path.exists("/proc/%d" % pid)
    assert live.proc.stdin.closed


def test_stills_of_one_keyframe_decode_once(monkeypatch):
    runs = []
    monkeypatch.setattr(camera, "ffmpeg_jpeg", lambda args, data: runs.append(1) or b"jpeg")
    live = make()
    live.gop = [b"key", b"p1"]
    assert [live.frame_jpeg() for _ in range(10)] == [b"jpeg"] * 10 and len(runs) == 1
    live.gop = [b"key2"]
    live.frame_jpeg()
    assert len(runs) == 2
    live.close()


def test_parallel_stills_of_one_keyframe_decode_once(monkeypatch):
    runs = []

    def decode(args, data):
        runs.append(1)
        time.sleep(0.2)
        return b"jpeg"

    monkeypatch.setattr(camera, "ffmpeg_jpeg", decode)
    live = make()
    live.gop = [b"key"]
    ts = [threading.Thread(target=live.frame_jpeg) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(runs) == 1
    live.close()


def test_restart_kills_a_daemon_that_ignores_sigterm(monkeypatch):
    import server
    monkeypatch.setattr(server, "TERM_WAIT", 0.3, raising=False)
    proc = subprocess.Popen([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);time.sleep(60)"],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "up"
    d = server.Daemon.__new__(server.Daemon)
    d.proc, d.wake = proc, threading.Event()
    try:
        d.restart()
        end = time.time() + 5
        while proc.poll() is None and time.time() < end:
            time.sleep(0.05)
        assert proc.poll() == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()

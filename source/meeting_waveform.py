"""Bounded live waveforms for the meeting recorder UI.

The audio callbacks can enqueue small amplitude blocks from any thread.  Only
the Tk poll/redraw path touches the widget.  The model is deliberately pure so
capture and geometry behaviour can be tested without a display.
"""

from collections import deque
from itertools import islice
import math
import queue
import tkinter as tk

import ui_theme


TRACKS = ("microphone", "system")
DEFAULT_CAPACITY = 4096
MAX_AMPLITUDE_BLOCK = 4096
MAX_PENDING_BLOCKS = 64
MAX_BLOCKS_PER_POLL = 16


def envelope_coordinates(points):
    """Flatten columns into one upper-edge/lower-edge polygon."""
    points = tuple(points)
    if not points:
        return ()
    if len(points) == 1:
        center, top, bottom = points[0]
        return (
            center - 0.5, top,
            center + 0.5, top,
            center + 0.5, bottom,
            center - 0.5, bottom,
        )
    upper = tuple(value for center, top, _bottom in points for value in (center, top))
    lower = tuple(value for center, _top, bottom in reversed(points) for value in (center, bottom))
    return upper + lower


class AmplitudeRingBuffer:
    """A fixed-size, chronological buffer of normalized audio amplitudes."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        if isinstance(capacity, bool) or int(capacity) != capacity or int(capacity) < 1:
            raise ValueError("capacity must be a positive integer")
        self.capacity = int(capacity)
        self._samples = deque(maxlen=self.capacity)

    def append_block(self, amplitudes):
        """Append at most :data:`MAX_AMPLITUDE_BLOCK` finite samples.

        Capture adapters commonly provide signed PCM-derived values, while
        meters commonly provide non-negative values.  Both are accepted and
        clamped to the normalized ``[-1.0, 1.0]`` range.  Invalid samples are
        ignored rather than allowing one malformed callback to break redraws.
        """
        try:
            values = islice(iter(amplitudes), MAX_AMPLITUDE_BLOCK)
        except TypeError as exc:
            raise TypeError("amplitudes must be iterable") from exc
        added = 0
        for value in values:
            try:
                sample = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(sample):
                continue
            self._samples.append(max(-1.0, min(1.0, sample)))
            added += 1
        return added

    def clear(self):
        self._samples.clear()

    def values(self):
        """Return a bounded immutable snapshot in oldest-to-newest order."""
        return tuple(self._samples)

    def __len__(self):
        return len(self._samples)

    def geometry(self, width, height, *, x=0.0, y=0.0):
        """Return ``(center_x, top_y, bottom_y)`` envelope columns.

        The number of columns is bounded by both the available pixel width and
        the buffer capacity.  Each column is a symmetric peak around the lane
        centre, which also makes non-negative RMS/meter blocks render as a
        conventional waveform.  Older samples are on the left.
        """
        try:
            width = max(0, int(float(width)))
            height = max(0.0, float(height))
            x = float(x)
            y = float(y)
        except (TypeError, ValueError):
            return ()
        samples = self.values()
        columns = min(width, len(samples))
        if columns <= 0 or height <= 0:
            return ()
        centre = y + height / 2.0
        half_height = height / 2.0
        result = []
        sample_count = len(samples)
        for column in range(columns):
            start = (column * sample_count) // columns
            stop = max(start + 1, ((column + 1) * sample_count) // columns)
            peak = max(abs(value) for value in samples[start:stop])
            amplitude = half_height * peak
            result.append((
                x + (column + 0.5) * width / columns,
                centre - amplitude,
                centre + amplitude,
            ))
        return tuple(result)


class MeetingWaveformModel:
    """Two bounded tracks and their testable display geometry."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self.capacity = int(capacity)
        self.tracks = {track: AmplitudeRingBuffer(self.capacity) for track in TRACKS}

    def _track(self, track):
        if track not in self.tracks:
            raise ValueError(f"unknown waveform track: {track!r}")
        return self.tracks[track]

    def append_block(self, track, amplitudes):
        return self._track(track).append_block(amplitudes)

    add_block = append_block

    def clear(self, track=None):
        if track is None:
            for buffer in self.tracks.values():
                buffer.clear()
        else:
            self._track(track).clear()

    def sample_count(self, track):
        return len(self._track(track))

    def geometry(self, track, width, height, *, x=0.0, y=0.0):
        return self._track(track).geometry(width, height, x=x, y=y)


class MeetingWaveform(tk.Canvas):
    """A two-lane Canvas for microphone and system audio.

    ``enqueue_block`` is safe for capture threads: it copies a bounded block
    into a bounded queue and performs no Tk operation.  ``append_block``,
    ``set_state``, and ``clear`` are Tk-thread methods for callers already on
    the UI thread.  The Canvas drains its queue on a Tk timer and coalesces
    redraws, so the audio callback never paints or calls Tcl.
    """

    STATES = ("idle", "recording", "paused")
    _DEFAULT_TRACK_LABELS = {"microphone": "Microfone", "system": "Sistema"}
    _DEFAULT_STATE_LABELS = {"idle": "Pronto", "recording": "Gravando", "paused": "Pausado"}

    def __init__(self, master, model=None, *, theme=None, poll_ms=40,
                 track_labels=None, state_labels=None, **kwargs):
        self.model = model or MeetingWaveformModel()
        if not all(track in self.model.tracks for track in TRACKS):
            raise ValueError("model must contain microphone and system tracks")
        if isinstance(poll_ms, bool) or int(poll_ms) != poll_ms or int(poll_ms) < 1:
            raise ValueError("poll_ms must be a positive integer")
        self.poll_ms = int(poll_ms)
        self.ui = theme or ui_theme.theme()
        self.track_labels = dict(self._DEFAULT_TRACK_LABELS)
        self.track_labels.update(track_labels or {})
        self.state_labels = dict(self._DEFAULT_STATE_LABELS)
        self.state_labels.update(state_labels or {})
        self._state = "idle"
        self._pending = queue.Queue(MAX_PENDING_BLOCKS)
        self._redraw_pending = False
        self._destroyed = False
        self._poll_id = None
        self._redraw_id = None
        kwargs.setdefault("background", self.ui.card)
        kwargs.setdefault("highlightbackground", self.ui.border)
        kwargs.setdefault("highlightcolor", self.ui.focus_ring)
        kwargs.setdefault("highlightthickness", 1)
        kwargs.setdefault("takefocus", True)
        super().__init__(master, **kwargs)
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._poll_id = self.after(self.poll_ms, self._drain_pending)
        self._request_redraw()

    @property
    def state(self):
        return self._state

    @property
    def status_text(self):
        return f"Estado: {self.state_labels.get(self._state, self._state)}"

    def set_state(self, state):
        """Set the visible status; must be called from the Tk thread."""
        if state not in self.STATES:
            raise ValueError(f"unknown waveform state: {state!r}")
        if self._state != state:
            self._state = state
            self._request_redraw()

    def append_block(self, track, amplitudes):
        """Append a block immediately; must be called from the Tk thread."""
        added = self.model.append_block(track, amplitudes)
        if added:
            self._request_redraw()
        return added

    def enqueue_block(self, track, amplitudes):
        """Queue a bounded block without touching Tk, for any capture thread."""
        if track not in TRACKS:
            raise ValueError(f"unknown waveform track: {track!r}")
        try:
            block = tuple(islice(iter(amplitudes), MAX_AMPLITUDE_BLOCK))
        except TypeError as exc:
            raise TypeError("amplitudes must be iterable") from exc
        if not block or self._destroyed:
            return False
        try:
            self._pending.put_nowait((track, block))
        except queue.Full:
            # Fresh audio is more useful than stale audio when the UI is busy.
            try:
                self._pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self._pending.put_nowait((track, block))
            except queue.Full:
                return False
        return True

    def clear(self, track=None):
        self.model.clear(track)
        self._request_redraw()

    def _on_configure(self, _event=None):
        self._request_redraw()

    def _request_redraw(self):
        if self._destroyed or self._redraw_pending:
            return
        self._redraw_pending = True
        try:
            self._redraw_id = self.after_idle(self._redraw)
        except tk.TclError:
            self._redraw_pending = False

    def _drain_pending(self):
        if self._destroyed:
            return
        changed = False
        for _ in range(MAX_BLOCKS_PER_POLL):
            try:
                track, block = self._pending.get_nowait()
            except queue.Empty:
                break
            changed = bool(self.model.append_block(track, block)) or changed
        if changed:
            self._request_redraw()
        try:
            self._poll_id = self.after(self.poll_ms, self._drain_pending)
        except tk.TclError:
            self._destroyed = True

    def _wave_color(self, track):
        if self._state == "paused":
            return self.ui.warning
        if self._state == "idle":
            return self.ui.text_muted
        return self.ui.accent if track == "microphone" else self.ui.success

    def _redraw(self):
        self._redraw_pending = False
        self._redraw_id = None
        if self._destroyed:
            return
        width = max(0, self.winfo_width())
        height = max(0, self.winfo_height())
        self.delete("waveform", "baseline", "labels", "status")
        if width < 4 or height < 4:
            return
        margin_x = 12
        label_height = 20
        lane_gap = 8
        lane_height = max(1.0, (height - label_height * 2 - lane_gap * 3) / 2.0)
        lane_width = max(1, width - margin_x * 2)
        for index, track in enumerate(TRACKS):
            lane_top = label_height + lane_gap + index * (lane_height + label_height + lane_gap)
            baseline = lane_top + lane_height / 2.0
            self.create_line(
                margin_x, baseline, width - margin_x, baseline,
                fill=self.ui.divider, width=1, tags=("baseline",),
            )
            self.create_text(
                margin_x, lane_top - label_height / 2.0,
                text=self.track_labels.get(track, track), anchor="w",
                fill=self.ui.text, font=self.ui.font(9, "bold"), tags=("labels",),
            )
            points = self.model.geometry(track, lane_width, lane_height, x=margin_x, y=lane_top)
            if points:
                color = self._wave_color(track)
                self.create_polygon(
                    *envelope_coordinates(points), fill=color, outline=color,
                    tags=("waveform", track),
                )
        self.create_text(
            width - margin_x, 8, text=self.status_text, anchor="ne",
            fill=self.ui.text_muted, font=self.ui.font(8), tags=("status",),
        )

    def _on_destroy(self, event):
        if event.widget is not self:
            return
        self._destroyed = True
        for after_id in (self._poll_id, self._redraw_id):
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._poll_id = self._redraw_id = None


WaveformCanvas = MeetingWaveform


__all__ = [
    "AmplitudeRingBuffer", "MeetingWaveformModel", "MeetingWaveform",
    "WaveformCanvas", "TRACKS", "DEFAULT_CAPACITY", "MAX_AMPLITUDE_BLOCK",
    "envelope_coordinates",
]

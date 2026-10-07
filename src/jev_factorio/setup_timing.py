"""Optional, content-free boundaries for one-use controller initialization.

The diagnostic is written after the controller exits.  It never authorizes a
native action and a failed diagnostic cannot change a controller result.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from .iteration_timing import validate_timing


STAGES = (
    'setup_start', 'preflight_ready', 'research_ready', 'dashboard_ready',
    'backend_ready', 'controller_ready', 'outputs_ready', 'initialized',
)
# setup-attribution.v1 records written before selected checkpoint preflight
# moved ahead of telemetry writer creation used this order. Keep it available
# so offline reports can validate already-sealed runs without relabeling them.
LEGACY_SETUP_STAGES = (
    'setup_start', 'research_ready', 'dashboard_ready', 'preflight_ready',
    'backend_ready', 'controller_ready', 'outputs_ready', 'initialized',
)
BACKEND_STAGES = ('attach_start', 'instance_ready', 'installation_ready', 'fair_ready')


class SetupTiming:
    def __init__(self, path: Path | None, *, backend_expected=False, wall_clock=None,
                 cpu_clock=None, thread_clock=None):
        self.path = path
        self.backend_expected = backend_expected
        self.wall_clock = time.perf_counter_ns if wall_clock is None else wall_clock
        self.cpu_clock = time.process_time_ns if cpu_clock is None else cpu_clock
        self.thread_clock = (getattr(time, 'thread_time_ns', None)
                             if thread_clock is None else thread_clock)
        self.thread_valid = callable(self.thread_clock)
        self._last_thread_sample = None
        self.cuts: list[tuple[str, int, int]] = []
        self.backend_cuts: list[tuple[str, int, int]] = []
        self.thread_cuts: list[tuple[int | None, ...]] = []
        self.backend_thread_cuts: list[tuple[int | None, ...]] = []
        self.valid = True
        self.final_iteration = None

    def _sample(self) -> tuple[int, int, int | None] | None:
        try:
            wall, cpu = self.wall_clock(), self.cpu_clock()
        except Exception:
            return None
        if type(wall) is not int or type(cpu) is not int or wall < 0 or cpu < 0:
            return None
        thread = None
        if self.thread_valid:
            try:
                thread = self.thread_clock()
            except Exception:
                self.thread_valid = False
            else:
                if type(thread) is not int or thread < 0:
                    self.thread_valid = False
                    thread = None
                elif (self._last_thread_sample is not None
                      and thread < self._last_thread_sample):
                    self.thread_valid = False
                    thread = None
                else:
                    self._last_thread_sample = thread
        return wall, cpu, thread

    def mark(self, stage: str) -> None:
        if stage not in STAGES or (self.cuts and STAGES.index(stage) <= STAGES.index(self.cuts[-1][0])):
            self.valid = False
            return
        sample = self._sample()
        if sample is None:
            self.valid = False
            return
        wall, cpu, thread = sample
        if self.cuts and (wall < self.cuts[-1][1] or cpu < self.cuts[-1][2]):
            self.valid = False
            return
        self.cuts.append((stage, wall, cpu))
        self.thread_cuts.append((thread,))

    def mark_backend(self, stage: str) -> None:
        if stage not in BACKEND_STAGES or (self.backend_cuts and
                BACKEND_STAGES.index(stage) <= BACKEND_STAGES.index(self.backend_cuts[-1][0])):
            self.valid = False
            return
        sample = self._sample()
        if sample is None:
            self.valid = False
            return
        wall, cpu, thread = sample
        if (self.backend_cuts
                and (wall < self.backend_cuts[-1][1] or cpu < self.backend_cuts[-1][2])):
            self.valid = False
            return
        self.backend_cuts.append((stage, wall, cpu))
        self.backend_thread_cuts.append((thread,))

    def result(self) -> dict:
        phases = []
        for (start, wall0, cpu0), (end, wall1, cpu1) in zip(self.cuts, self.cuts[1:]):
            phases.append({'from': start, 'to': end,
                           'wall_ns': wall1 - wall0, 'process_cpu_ns': cpu1 - cpu0})
        backend_phases = [
            {'from': start, 'to': end, 'wall_ns': wall1 - wall0,
             'process_cpu_ns': cpu1 - cpu0}
            for (start, wall0, cpu0), (end, wall1, cpu1) in
            zip(self.backend_cuts, self.backend_cuts[1:])
        ]
        backend_complete = ([row[0] for row in self.backend_cuts] == list(BACKEND_STAGES)
                            if self.backend_expected else not self.backend_cuts)
        return {'schema': 'jev.setup-timing.v1',
                'status': ('complete' if self.valid and
                           [row[0] for row in self.cuts] == list(STAGES) and
                           backend_complete else 'partial'),
                'scope': 'ordered_one_use_setup_boundaries_no_native_or_wire_attribution',
                'phases': phases, 'backend_phases': backend_phases,
                'final_iteration': self.final_iteration}

    def profile_result(self) -> dict:
        """Return content-free startup clocks for an already-authorized run."""
        result = self.result()
        def prefix(cuts, thread_cuts, stages):
            """Keep only adjacent phases from the first boundary after a bad sample."""
            rows = []
            if not cuts or cuts[0][0] != stages[0]:
                return rows
            for index, ((start, wall0, cpu0), (end, wall1, cpu1)) in enumerate(
                    zip(cuts, cuts[1:])):
                if start != stages[len(rows)] or end != stages[len(rows) + 1]:
                    break
                thread = None
                if self.thread_valid:
                    first, second = thread_cuts[index][0], thread_cuts[index + 1][0]
                    if first is not None and second is not None and second >= first:
                        thread = second - first
                rows.append({'from': start, 'to': end, 'wall_ns': wall1 - wall0,
                             'process_cpu_ns': cpu1 - cpu0, 'thread_cpu_ns': thread})
            return rows

        phases = prefix(self.cuts, self.thread_cuts, STAGES)
        backend_phases = prefix(self.backend_cuts, self.backend_thread_cuts,
                                BACKEND_STAGES)
        complete = (result['status'] == 'complete'
                    and len(phases) == len(STAGES) - 1
                    and (not self.backend_expected
                         or len(backend_phases) == len(BACKEND_STAGES) - 1))
        return {
            'schema': 'jev.setup-attribution.v1',
            'status': 'complete' if complete else 'partial',
            'clocks': {'wall': 'perf_counter_ns', 'process_cpu': 'process_time_ns',
                       'thread_cpu': 'thread_time_ns' if self.thread_valid else None},
            'scope': ('ordered_setup_boundaries; backend phases are nested in '
                      'dashboard_ready_to_backend_ready'),
            'phases': phases,
            'backend_phases': backend_phases,
        }

    def capture_final_iteration(self, loop) -> None:
        """Publish the completed final step, leaving its following gap unknown."""
        pending = getattr(loop, '_timing_pending', None)
        summary = pending.get('summary') if isinstance(pending, dict) else None
        if not isinstance(summary, dict):
            return
        candidate = {**summary, 'gap': {
            'complete': False, 'total_ns': None, 'intentional_sleep_ns': None,
            'other_gap_ns': None,
            'sleep_calls': pending.get('sleep_calls', 0),
            'sleep_failed': pending.get('sleep_failed', 0),
            'requested_sleep_ns': pending.get('requested_sleep_ns', 0),
            'scope': 'previous_decorated_step_end_to_current_decorated_step_start',
        }}
        try:
            validate_timing(candidate)
        except Exception:
            self.valid = False
        else:
            self.final_iteration = candidate

    def capture_final_iteration_safely(self, loop) -> None:
        try:
            self.capture_final_iteration(loop)
        except BaseException:
            self.valid = False

    def write(self) -> None:
        """Best-effort exclusive publication; never replace an existing result."""
        if self.path is None:
            return
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, prefix='.setup-timing-',
                                             delete=False) as stream:
                tmp = Path(stream.name)
                os.chmod(tmp, 0o600)
                json.dump(self.result(), stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(tmp, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            # This is diagnostic only. The owner checks for a missing result.
            pass
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

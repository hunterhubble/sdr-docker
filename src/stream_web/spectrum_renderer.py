"""Spectrum-analyzer image renderer — runs in its own OS process.

The FCC-overlay/spurious-emission search plus the matplotlib draw
(render_spectrum_image) is heavy enough, and holds the GIL long enough during
pure-Python peak-search and text layout, that running it on a background
*thread* inside the processor process would still let it steal CPU time from
the real-time decode loop. Moving it to a separate process (the same reason
td_renderer.py and processor.py itself are split out) gives it true
parallelism instead of GIL-shared concurrency.

Communicates via two multiprocessing.Queue instances:
  * spectrum_job_queue — (chunks, lo_freq_hz) requests in, from processor.py
  * result_queue       — rendered "img" out, shared with processor.py's own
                         results back to app.py (spectrogram and spectrum
                         share the same display slot, so reusing the "img"
                         key is intentional)
"""

import queue

from .spectrogram import render_spectrum_image


def spectrum_renderer_main(spectrum_job_queue, result_queue, running_event):
    """Entry point for the spectrum-analyzer renderer process."""
    print("[SPECTRUM-PROC] Renderer process started (separate GIL).", flush=True)

    while running_event.is_set():
        try:
            chunks, lo_freq_hz = spectrum_job_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        render_result = {"img": None}
        try:
            render_result["img"] = render_spectrum_image(chunks, lo_freq_hz)
        except Exception as e:
            print(f"[SPECTRUM] Render error: {e}")
        try:
            result_queue.put_nowait(render_result)
        except Exception:
            pass

    print("[SPECTRUM-PROC] Renderer process exiting.", flush=True)

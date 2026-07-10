"""Time-domain plot renderer — runs in its own OS process.

matplotlib rendering (render_td_plot / render_symbol_zoom_plot) is heavy
enough, and holds the GIL long enough during pure-Python tick/text layout,
that running it on a background *thread* inside the processor process still
let it steal CPU time from the real-time decode loop. Moving it to a
separate process (the same reason processor.py itself is split from the RX
thread) gives it true parallelism instead of GIL-shared concurrency.

Communicates via two multiprocessing.Queue instances:
  * td_job_queue    — (td_seg, decode_info, n_syms) requests in, from processor.py
  * result_queue    — rendered td_img/td_zoom_img/td_decode_info/td_iq_segment out
                       (plus td_status on error), shared with processor.py's own
                       results back to app.py
"""

import queue

from .spectrogram import render_symbol_zoom_plot, render_td_plot


def td_renderer_main(td_job_queue, result_queue, running_event):
    """Entry point for the time-domain-plot renderer process."""
    print("[TD-PROC] Renderer process started (separate GIL).", flush=True)

    while running_event.is_set():
        try:
            td_seg, decode_info, n_syms = td_job_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        render_result = {"td_img": None, "td_zoom_img": None,
                          "td_decode_info": None, "td_iq_segment": None}
        try:
            td_img, td_stats = render_td_plot(td_seg, decode_info=decode_info)
            info_out = {
                k: v for k, v in decode_info.items()
                if isinstance(v, (str, int, float, bool, list, type(None)))
            }
            info_out.update(td_stats)
            render_result["td_img"] = td_img
            render_result["td_decode_info"] = info_out
            render_result["td_iq_segment"] = td_seg
        except Exception as e:
            print(f"[TD] Plot error: {e}")
            # Surface the failure to the UI -- the processor owns td_status
            # normally, but on a render error it has no way to know, so push
            # it from here (mirrors the pre-refactor inline behaviour).
            render_result["td_status"] = f"Render error: {e}"
        try:
            # render_symbol_zoom_plot may return b"" when there's nothing to
            # zoom; normalize to None so the drain guard (is not None) leaves
            # the previous good zoom image in place instead of blanking it.
            zoom = render_symbol_zoom_plot(
                td_seg, decode_info=decode_info, n_symbols=n_syms,
            )
            render_result["td_zoom_img"] = zoom or None
        except Exception as e:
            print(f"[TD] Zoom plot error: {e}")
        try:
            result_queue.put_nowait(render_result)
        except Exception:
            pass

    print("[TD-PROC] Renderer process exiting.", flush=True)

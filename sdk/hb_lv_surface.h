/*
 * hb_lv_surface.h — reusable runtime for a relocatable LVGL Silver app.
 *
 * Each LVGL app is a self-contained relocatable blob (.hbapp = LVGL + this
 * runtime + the app, with LVGL's TLSF pool in .bss). The resident loads it into
 * an operator-new arena (hb_reloc) and runs it from there — no fixed VA. The
 * runtime owns the entry point, LVGL setup (DIRECT render into the resident's
 * framebuffer), the per-frame pump, the hardware tick, and the touch indev. The app
 * only implements lv_app_main() (and, optionally, registers a per-frame hook).
 *
 * ABI the resident drives:
 *   void *payload_entry(int op, void *fb, int w, int h)   // op 0: setup, returns &frame fn
 *   the returned fn is called once per OS-timer tick to advance + render.
 */
#ifndef HB_LV_SURFACE_H
#define HB_LV_SURFACE_H

/* Implement this in the app: build the LVGL UI on lv_screen_active(). Query the
 * screen size with lv_display_get_horizontal_resolution(lv_display_get_default())
 * etc. The runtime has already done lv_init() + created the display + indev. */
void lv_app_main(void);

/* Optional: register a callback the runtime invokes once per frame, before
 * lv_timer_handler (e.g. a game's logic/physics tick). Pass 0 to clear. */
void hb_lv_set_frame_cb(void (*cb)(void));

/* Optional: register a callback the runtime invokes AFTER lv_timer_handler —
 * the last thing to touch the framebuffer before it's composited, so it draws
 * ON TOP of LVGL (e.g. a hardware overlay via hb_compositor). Pass 0 to clear. */
void hb_lv_set_post_cb(void (*cb)(void));

/* Optional teardown hook. The resident calls the surface entry with op=1 while
 * the app arena is still alive, giving apps that own OS resources (open files,
 * heap blocks, wake locks) a chance to release them on a Home/app transition. */
void hb_lv_set_exit_cb(void (*cb)(void));

/* The display framebuffer (LVGL renders directly into it; XRGB8888, w*h*4,
 * GPU-addressable). For an app frame cb that wants hardware 2D (hb_compositor). */
void *hb_lv_framebuffer(void);
int   hb_lv_fb_width(void);
int   hb_lv_fb_height(void);

#endif /* HB_LV_SURFACE_H */

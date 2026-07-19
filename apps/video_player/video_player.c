/*
 * video_player — streamed video + chunked WAV playback for NanoApps.
 *
 * The retail OS/SDK does not expose an arbitrary MP4/H.264 decoder to a
 * homebrew surface. Videos are converted on the host with tools/make_hbv.py
 * into .hbv files: RGB565 frames compressed as delta COPY/SKIP runs. The app
 * reads one compressed frame at a time, so file length is not limited by RAM.
 * A matching <name>.audio directory may contain meta.hba plus numbered WAV
 * chunks. Short chunks let pause/back take effect without needing an unknown
 * firmware stop/seek call; playback re-synchronizes at every chunk boundary.
 *
 * Files: .hbv entries inside /Apps/Data/Videos
 * Format (little-endian):
 *   32-byte header:
 *     "HBVID1\0\0", u32 width, u32 height, u32 frames,
 *     u32 frame_ms, u32 bpp (=2), u32 largest_packet
 *   frames:
 *     u32 packet_size (bit31 = keyframe), packet bytes
 *   packet ops:
 *     u16 token; bit15 COPY with count RGB565 literals, otherwise SKIP count.
 */
#include "hb_sdk.h"
#include "hb_heap.h"
#include "hb_prefs.h"
#include "hb_lv_surface.h"
#include "lvgl/lvgl.h"

#define DATA_DIR          "/Apps/Data/Videos"
#define MAX_VIDEOS        64
#define MAX_NAME          72
#define HBV_HEADER        32u
#define MAX_VIDEO_W       240u
#define MAX_VIDEO_H       320u
#define MAX_PACKET        (384u * 1024u)
#define HBA_HEADER        32u
#define MAX_AUDIO_CHUNKS  10000u

#define HEADER_H          40
#define STAGE_H           320
#define CONTROLS_Y        (HEADER_H + STAGE_H)
#define CONTROLS_H        (432 - CONTROLS_Y)

typedef struct {
    char name[MAX_NAME];
    uint32_t size;
    bool has_audio;
} video_t;

typedef struct {
    uint32_t w, h, frames, frame_ms, bpp, max_packet;
} hbv_header_t;

static video_t s_videos[MAX_VIDEOS];
static int s_n_videos;

static lv_obj_t *s_screen;
static lv_obj_t *s_library;
static lv_obj_t *s_player;
static lv_obj_t *s_canvas;
static lv_obj_t *s_play_icon;
static lv_obj_t *s_loop_label;
static lv_obj_t *s_progress;
static lv_obj_t *s_time_label;
static lv_obj_t *s_status_label;

static uint16_t *s_pixels;
static uint8_t  *s_packet;
static hbv_header_t s_hdr;
static char s_path[160];
static uint32_t s_frame;
static uint32_t s_next_ms;
static uint32_t s_last_hud_ms;
static bool s_playing;
static bool s_loop = true;
static bool s_stream_open;
static char s_audio_dir[192];
static uint32_t s_audio_chunk_ms;
static uint32_t s_audio_chunks;
static uint32_t s_audio_next;
static uint32_t s_audio_total_ms;
static uint32_t s_audio_active_until;
static bool s_has_audio;
static bool s_restart_pending;

/* ---------------------------------------------------------------- strings -- */

static int s_len(const char *s) { int n = 0; while (s[n]) n++; return n; }

static void s_copy(char *dst, const char *src, int cap)
{
    int i = 0;
    while (src[i] && i < cap - 1) { dst[i] = src[i]; i++; }
    dst[i] = 0;
}

static bool is_hbv(const char *name)
{
    int n = s_len(name);
    if (n < 5 || name[n - 4] != '.') return false;
    return (name[n - 3] | 0x20) == 'h' &&
           (name[n - 2] | 0x20) == 'b' &&
           (name[n - 1] | 0x20) == 'v';
}

static int cmp_ci(const char *a, const char *b)
{
    while (*a && *b) {
        char ca = *a++, cb = *b++;
        if (ca >= 'A' && ca <= 'Z') ca = (char)(ca + 32);
        if (cb >= 'A' && cb <= 'Z') cb = (char)(cb + 32);
        if (ca != cb) return ca < cb ? -1 : 1;
    }
    return *a ? 1 : (*b ? -1 : 0);
}

static void path_for(char *out, const char *name)
{
    int k = 0;
    const char *p = DATA_DIR "/";
    while (*p && k < 158) out[k++] = *p++;
    for (int i = 0; name[i] && k < 158; i++) out[k++] = name[i];
    out[k] = 0;
}

static void audio_dir_for(char *out, const char *name)
{
    path_for(out, name);
    int n = s_len(out);
    if (n >= 4 && out[n - 4] == '.') {
        out[n - 3] = 'a'; out[n - 2] = 'u'; out[n - 1] = 'd';
        out[n++] = 'i'; out[n++] = 'o'; out[n] = 0;
    }
}

static void audio_meta_for(char *out, const char *name)
{
    audio_dir_for(out, name);
    int k = s_len(out);
    const char *tail = "/meta.hba";
    while (*tail && k < 190) out[k++] = *tail++;
    out[k] = 0;
}

static void display_name(char *out, const char *name, int cap)
{
    s_copy(out, name, cap);
    int n = s_len(out);
    if (n >= 4 && out[n - 4] == '.') out[n - 4] = 0;
}

static void append_u(char *out, int *k, uint32_t value)
{
    char tmp[12]; int n = 0;
    if (value == 0) tmp[n++] = '0';
    else while (value) { tmp[n++] = (char)('0' + value % 10u); value /= 10u; }
    while (n) out[(*k)++] = tmp[--n];
}

static void format_size(uint32_t bytes, char out[24])
{
    int k = 0;
    if (bytes >= 1024u * 1024u) {
        append_u(out, &k, bytes / (1024u * 1024u));
        out[k++] = '.';
        append_u(out, &k, (bytes % (1024u * 1024u)) * 10u / (1024u * 1024u));
        out[k++] = ' '; out[k++] = 'M'; out[k++] = 'B';
    } else {
        append_u(out, &k, (bytes + 1023u) / 1024u);
        out[k++] = ' '; out[k++] = 'K'; out[k++] = 'B';
    }
    out[k] = 0;
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t rd32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

/* -------------------------------------------------------------- filesystem -- */

static void scan_videos(void)
{
    hb_dir_t d;
    char name[MAX_NAME];
    bool is_dir = false;
    s_n_videos = 0;
    if (!hb_fs_dir_open(&d, DATA_DIR, false)) return;
    while (hb_fs_dir_next(&d, name, sizeof name, &is_dir)) {
        if (is_dir || name[0] == '.' || !is_hbv(name) || s_n_videos >= MAX_VIDEOS)
            continue;
        s_copy(s_videos[s_n_videos].name, name, MAX_NAME);
        char path[160]; path_for(path, name);
        s_videos[s_n_videos].size = hb_fs_size(path);
        char meta[192]; audio_meta_for(meta, name);
        s_videos[s_n_videos].has_audio = hb_fs_exists(meta);
        s_n_videos++;
    }
    hb_fs_dir_close(&d);

    for (int i = 1; i < s_n_videos; i++) {
        video_t cur = s_videos[i]; int j = i;
        while (j > 0 && cmp_ci(s_videos[j - 1].name, cur.name) > 0) {
            s_videos[j] = s_videos[j - 1]; j--;
        }
        s_videos[j] = cur;
    }
}

static bool open_audio_meta(const char *name)
{
    uint8_t raw[HBA_HEADER];
    char meta[192];
    audio_meta_for(meta, name);
    s_has_audio = false;
    s_audio_chunk_ms = s_audio_chunks = s_audio_next = 0;
    s_audio_total_ms = 0;
    if (hb_fs_read(meta, raw, sizeof raw) != sizeof raw) return false;
    if (raw[0] != 'H' || raw[1] != 'B' || raw[2] != 'A' ||
        raw[3] != 'U' || raw[4] != 'D' || raw[5] != '1') return false;
    uint32_t chunk_ms = rd32(raw + 8);
    uint32_t chunks = rd32(raw + 12);
    uint32_t total_ms = rd32(raw + 16);
    uint32_t channels = rd32(raw + 24);
    uint32_t bits = rd32(raw + 28);
    if (chunk_ms < 250u || chunk_ms > 10000u || chunks == 0 || total_ms == 0 ||
        chunks > MAX_AUDIO_CHUNKS || channels != 1u || bits != 16u) return false;
    audio_dir_for(s_audio_dir, name);
    s_audio_chunk_ms = chunk_ms;
    s_audio_chunks = chunks;
    s_audio_total_ms = total_ms;
    s_has_audio = true;
    return true;
}

static void audio_chunk_path(char out[224], uint32_t chunk)
{
    int k = 0;
    while (s_audio_dir[k] && k < 200) { out[k] = s_audio_dir[k]; k++; }
    out[k++] = '/';
    uint32_t div = 10000u;
    while (div) {
        out[k++] = (char)('0' + (chunk / div) % 10u);
        div /= 10u;
    }
    out[k++] = '.'; out[k++] = 'w'; out[k++] = 'a'; out[k++] = 'v'; out[k] = 0;
}

static uint32_t stream_read_exact(void *buf, uint32_t len)
{
    uint8_t *p = (uint8_t *)buf;
    uint32_t total = 0;
    while (total < len) {
        uint32_t got = hb_fs_read_stream(p + total, len - total);
        if (got == 0) break;
        total += got;
    }
    return total;
}

static bool parse_header(const uint8_t raw[HBV_HEADER], hbv_header_t *h)
{
    if (raw[0] != 'H' || raw[1] != 'B' || raw[2] != 'V' ||
        raw[3] != 'I' || raw[4] != 'D' || raw[5] != '1') return false;
    h->w          = rd32(raw + 8);
    h->h          = rd32(raw + 12);
    h->frames     = rd32(raw + 16);
    h->frame_ms   = rd32(raw + 20);
    h->bpp        = rd32(raw + 24);
    h->max_packet = rd32(raw + 28);
    return h->w > 0 && h->w <= MAX_VIDEO_W &&
           h->h > 0 && h->h <= MAX_VIDEO_H &&
           h->frames > 0 && h->frame_ms >= 16 && h->frame_ms <= 1000 &&
           h->bpp == 2 && h->max_packet > 0 && h->max_packet <= MAX_PACKET;
}

static bool open_stream_header(const char *path, hbv_header_t *h)
{
    uint8_t raw[HBV_HEADER];
    if (!hb_fs_read_stream_open(path)) return false;
    s_stream_open = true;
    if (stream_read_exact(raw, sizeof raw) != sizeof raw || !parse_header(raw, h)) {
        hb_fs_read_stream_close(); s_stream_open = false; return false;
    }
    return true;
}

static void close_stream(void)
{
    if (s_stream_open) hb_fs_read_stream_close();
    s_stream_open = false;
}

/* Return 1 for a decoded frame, 0 for EOF, -1 for malformed/I/O failure. */
static int decode_next(void)
{
    uint8_t lb[4];
    if (s_frame >= s_hdr.frames) return 0;
    if (stream_read_exact(lb, 4) != 4) return -1;
    uint32_t raw_len = rd32(lb);
    bool keyframe = (raw_len & 0x80000000u) != 0;
    uint32_t len = raw_len & 0x7fffffffu;
    if (len == 0 || len > s_hdr.max_packet) return -1;
    if (stream_read_exact(s_packet, len) != len) return -1;

    uint32_t pixels = s_hdr.w * s_hdr.h;
    uint32_t pos = 0, px = 0;
    if (keyframe)
        for (uint32_t i = 0; i < pixels; i++) s_pixels[i] = 0;

    while (pos + 2u <= len && px < pixels) {
        uint16_t tok = rd16(s_packet + pos); pos += 2;
        uint32_t count = tok & 0x7fffu;
        if (count == 0 || px + count > pixels) return -1;
        if (tok & 0x8000u) {
            uint32_t bytes = count * 2u;
            if (pos + bytes > len) return -1;
            for (uint32_t i = 0; i < count; i++) {
                s_pixels[px++] = rd16(s_packet + pos);
                pos += 2;
            }
        } else {
            px += count;
        }
    }
    if (pos != len || px != pixels) return -1;
    s_frame++;
    return 1;
}

static bool rewind_stream(void)
{
    hbv_header_t again;
    close_stream();
    if (!open_stream_header(s_path, &again)) return false;
    if (again.w != s_hdr.w || again.h != s_hdr.h ||
        again.frames != s_hdr.frames || again.max_packet > s_hdr.max_packet) {
        close_stream(); return false;
    }
    s_frame = 0;
    for (uint32_t i = 0; i < s_hdr.w * s_hdr.h; i++) s_pixels[i] = 0;
    return true;
}

/* ----------------------------------------------------------------- player -- */

static void update_play_icon(void)
{
    if (s_play_icon)
        lv_label_set_text(s_play_icon, s_playing ? LV_SYMBOL_PAUSE : LV_SYMBOL_PLAY);
}

static void format_clock(uint32_t sec, char *out, int *k)
{
    append_u(out, k, sec / 60u);
    out[(*k)++] = ':';
    uint32_t s = sec % 60u;
    out[(*k)++] = (char)('0' + s / 10u);
    out[(*k)++] = (char)('0' + s % 10u);
}

static void update_hud(void)
{
    if (!s_progress || !s_time_label || s_hdr.frames == 0) return;
    int value = (int)((s_frame * 1000u) / s_hdr.frames);
    lv_bar_set_value(s_progress, value, LV_ANIM_OFF);
    char b[32]; int k = 0;
    format_clock((s_frame * s_hdr.frame_ms) / 1000u, b, &k);
    b[k++] = ' '; b[k++] = '/'; b[k++] = ' ';
    format_clock((s_hdr.frames * s_hdr.frame_ms) / 1000u, b, &k);
    b[k] = 0;
    lv_label_set_text(s_time_label, b);
}

static void playback_error(void)
{
    s_playing = false;
    hb_wake_lock(false);
    update_play_icon();
    if (s_status_label) lv_label_set_text(s_status_label, "Playback error");
}

static bool audio_active(uint32_t now)
{
    return s_has_audio && (int32_t)(now - s_audio_active_until) < 0;
}

static uint32_t audio_chunk_duration(uint32_t chunk)
{
    uint32_t start = chunk * s_audio_chunk_ms;
    if (start >= s_audio_total_ms) return s_audio_chunk_ms;
    uint32_t left = s_audio_total_ms - start;
    return left < s_audio_chunk_ms ? left : s_audio_chunk_ms;
}

static void try_start_audio(uint32_t now)
{
    if (!s_has_audio || !s_playing || s_audio_next >= s_audio_chunks ||
        audio_active(now)) return;

    uint32_t video_ms = s_frame > 0 ? (s_frame - 1u) * s_hdr.frame_ms : 0;
    uint32_t wanted = video_ms / s_audio_chunk_ms;
    if (wanted > s_audio_next) s_audio_next = wanted; /* re-sync after a stall */
    if (s_audio_next >= s_audio_chunks ||
        video_ms < s_audio_next * s_audio_chunk_ms) return;

    char path[224];
    uint32_t chunk = s_audio_next;
    audio_chunk_path(path, chunk);
    if (hb_audio_play_wav_main(path, 0x7000u)) {
        s_audio_next++;
        s_audio_active_until = now + audio_chunk_duration(chunk);
        if (s_status_label) {
            lv_obj_set_style_text_color(s_status_label, lv_color_hex(0x30d158), 0);
            lv_label_set_text(s_status_label, "Audio");
        }
    } else if (s_status_label) {
        lv_obj_set_style_text_color(s_status_label, lv_color_hex(0xff453a), 0);
        lv_label_set_text(s_status_label, "Audio start failed");
    }
}

static void set_playing(bool on)
{
    if (on && s_frame >= s_hdr.frames) {
        if (!rewind_stream() || decode_next() != 1) { playback_error(); return; }
        if (s_canvas) lv_obj_invalidate(s_canvas);
    }
    s_playing = on;
    hb_wake_lock(on);
    s_next_ms = lv_tick_get() + s_hdr.frame_ms;
    update_play_icon();
}

static bool restart_now(void)
{
    if (!rewind_stream() || decode_next() != 1) {
        playback_error(); return false;
    }
    s_audio_next = 0;
    s_audio_active_until = 0;
    s_restart_pending = false;
    if (s_canvas) lv_obj_invalidate(s_canvas);
    update_hud();
    set_playing(true);
    try_start_audio(lv_tick_get());
    return true;
}

static void request_restart(void)
{
    uint32_t now = lv_tick_get();
    if (audio_active(now)) {
        s_restart_pending = true;
        s_playing = false;
        hb_wake_lock(true);
        update_play_icon();
        if (s_status_label) {
            lv_obj_set_style_text_color(s_status_label, lv_color_hex(0x9aa5b1), 0);
            lv_label_set_text(s_status_label, "Restarting after audio chunk");
        }
        return;
    }
    restart_now();
}

static void player_frame(void)
{
    uint32_t now = lv_tick_get();
    if (s_restart_pending) {
        if (!audio_active(now)) restart_now();
        return;
    }
    if (!s_playing || !s_canvas) return;
    try_start_audio(now);
    if ((int32_t)(now - s_next_ms) < 0) return;

    int rc = decode_next();
    if (rc == 0) {
        if (!s_loop) { set_playing(false); update_hud(); return; }
        request_restart();
        return;
    }
    if (rc != 1) { playback_error(); return; }

    lv_obj_invalidate(s_canvas);
    /* Don't build an ever-growing catch-up debt after a slow disk frame. */
    s_next_ms = now + s_hdr.frame_ms;
    if (now - s_last_hud_ms >= 250u || s_frame == s_hdr.frames) {
        update_hud(); s_last_hud_ms = now;
    }
    try_start_audio(now);
}

static void toggle_cb(lv_event_t *e)
{
    (void)e;
    if (s_restart_pending) return;
    bool on = !s_playing;
    if (on && s_frame >= s_hdr.frames) {
        request_restart();
        return;
    }
    set_playing(on);
    if (on) {
        try_start_audio(lv_tick_get());
    } else if (s_has_audio && audio_active(lv_tick_get()) && s_status_label) {
        lv_obj_set_style_text_color(s_status_label, lv_color_hex(0x9aa5b1), 0);
        lv_label_set_text(s_status_label, "Pausing after audio chunk");
    }
}

static void restart_cb(lv_event_t *e)
{
    (void)e;
    request_restart();
}

static void loop_cb(lv_event_t *e)
{
    (void)e; s_loop = !s_loop;
    if (s_loop_label) lv_label_set_text(s_loop_label, s_loop ? "Loop on" : "Loop off");
}

static void player_free(void)
{
    hb_lv_set_frame_cb(0);
    hb_wake_lock(false);
    close_stream();
    if (s_pixels) { hb_os_free(s_pixels); s_pixels = 0; }
    if (s_packet) { hb_os_free(s_packet); s_packet = 0; }
    s_canvas = 0; s_play_icon = 0; s_loop_label = 0;
    s_progress = 0; s_time_label = 0; s_status_label = 0;
    s_playing = false;
    s_restart_pending = false;
    s_has_audio = false;
    s_audio_next = s_audio_chunks = s_audio_chunk_ms = 0;
    s_audio_total_ms = 0;
}

static void build_library(void);

static void player_back_cb(lv_event_t *e)
{
    (void)e;
    player_free();
    if (s_player) { lv_obj_delete(s_player); s_player = 0; }
    build_library();
}

static lv_obj_t *plain_box(lv_obj_t *parent, int x, int y, int w, int h, uint32_t color)
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, x, y); lv_obj_set_size(o, w, h);
    lv_obj_set_style_bg_color(o, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    return o;
}

static void show_player_error(const char *message)
{
    s_player = plain_box(s_screen, 0, 0, 240, 432, 0x000000);
    lv_obj_t *back = lv_button_create(s_player);
    lv_obj_set_pos(back, 4, 4); lv_obj_set_size(back, 38, 32);
    lv_obj_add_event_cb(back, player_back_cb, LV_EVENT_CLICKED, 0);
    lv_obj_t *bl = lv_label_create(back); lv_label_set_text(bl, LV_SYMBOL_LEFT); lv_obj_center(bl);
    lv_obj_t *msg = lv_label_create(s_player);
    lv_obj_set_width(msg, 208);
    lv_label_set_long_mode(msg, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(msg, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(msg, lv_color_hex(0x9aa5b1), 0);
    lv_label_set_text(msg, message); lv_obj_center(msg);
}

static void build_player(int idx)
{
    char title[MAX_NAME];
    if (idx < 0 || idx >= s_n_videos) return;
    if (s_library) { lv_obj_delete(s_library); s_library = 0; }
    path_for(s_path, s_videos[idx].name);
    display_name(title, s_videos[idx].name, sizeof title);
    open_audio_meta(s_videos[idx].name);

    if (!open_stream_header(s_path, &s_hdr)) {
        show_player_error("This is not a valid HBVID1 video."); return;
    }
    s_pixels = (uint16_t *)hb_os_alloc(s_hdr.w * s_hdr.h * 2u);
    s_packet = (uint8_t *)hb_os_alloc(s_hdr.max_packet);
    if (!s_pixels || !s_packet) {
        player_free(); show_player_error("Not enough memory to play this video."); return;
    }
    for (uint32_t i = 0; i < s_hdr.w * s_hdr.h; i++) s_pixels[i] = 0;
    s_frame = 0;

    s_player = plain_box(s_screen, 0, 0, 240, 432, 0x000000);
    lv_obj_t *header = plain_box(s_player, 0, 0, 240, HEADER_H, 0x111522);
    lv_obj_t *back = lv_button_create(header);
    lv_obj_set_pos(back, 4, 4); lv_obj_set_size(back, 36, 32);
    lv_obj_set_style_pad_all(back, 0, 0);
    lv_obj_add_event_cb(back, player_back_cb, LV_EVENT_CLICKED, 0);
    lv_obj_t *bl = lv_label_create(back); lv_label_set_text(bl, LV_SYMBOL_LEFT); lv_obj_center(bl);
    lv_obj_t *tl = lv_label_create(header);
    lv_obj_set_pos(tl, 46, 10); lv_obj_set_size(tl, 188, 24);
    lv_label_set_long_mode(tl, LV_LABEL_LONG_SCROLL_CIRCULAR);
    lv_obj_set_style_text_color(tl, lv_color_hex(0xffffff), 0);
    lv_label_set_text(tl, title);

    lv_obj_t *stage = plain_box(s_player, 0, HEADER_H, 240, STAGE_H, 0x000000);
    lv_obj_add_flag(stage, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(stage, toggle_cb, LV_EVENT_CLICKED, 0);
    s_canvas = lv_canvas_create(stage);
    lv_canvas_set_buffer(s_canvas, s_pixels, s_hdr.w, s_hdr.h, LV_COLOR_FORMAT_RGB565);
    lv_obj_center(s_canvas);

    lv_obj_t *controls = plain_box(s_player, 0, CONTROLS_Y, 240, CONTROLS_H, 0x111522);
    s_progress = lv_bar_create(controls);
    lv_obj_set_pos(s_progress, 8, 6); lv_obj_set_size(s_progress, 142, 8);
    lv_bar_set_range(s_progress, 0, 1000);
    s_time_label = lv_label_create(controls);
    lv_obj_set_pos(s_time_label, 156, 1); lv_obj_set_size(s_time_label, 80, 18);
    lv_obj_set_style_text_font(s_time_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(s_time_label, lv_color_hex(0x9aa5b1), 0);
    lv_obj_set_style_text_align(s_time_label, LV_TEXT_ALIGN_RIGHT, 0);

    lv_obj_t *restart = lv_button_create(controls);
    lv_obj_set_pos(restart, 8, 25); lv_obj_set_size(restart, 60, 40);
    lv_obj_add_event_cb(restart, restart_cb, LV_EVENT_CLICKED, 0);
    lv_obj_t *rl = lv_label_create(restart); lv_label_set_text(rl, LV_SYMBOL_REFRESH); lv_obj_center(rl);

    lv_obj_t *play = lv_button_create(controls);
    lv_obj_set_pos(play, 76, 25); lv_obj_set_size(play, 88, 40);
    lv_obj_add_event_cb(play, toggle_cb, LV_EVENT_CLICKED, 0);
    s_play_icon = lv_label_create(play); lv_obj_center(s_play_icon);

    lv_obj_t *loop = lv_button_create(controls);
    lv_obj_set_pos(loop, 172, 25); lv_obj_set_size(loop, 60, 40);
    lv_obj_set_style_pad_all(loop, 0, 0);
    lv_obj_add_event_cb(loop, loop_cb, LV_EVENT_CLICKED, 0);
    s_loop_label = lv_label_create(loop);
    lv_obj_set_style_text_font(s_loop_label, &lv_font_montserrat_14, 0);
    lv_label_set_text(s_loop_label, s_loop ? "Loop on" : "Loop off"); lv_obj_center(s_loop_label);

    s_status_label = lv_label_create(stage);
    lv_obj_set_style_text_color(s_status_label,
        lv_color_hex(s_has_audio ? 0x30d158 : 0x9aa5b1), 0);
    lv_label_set_text(s_status_label, s_has_audio ? "Audio" : "Silent");
    lv_obj_align(s_status_label, LV_ALIGN_BOTTOM_MID, 0, -4);

    if (decode_next() != 1) { playback_error(); return; }
    lv_obj_invalidate(s_canvas);
    update_hud();
    s_last_hud_ms = lv_tick_get();
    hb_lv_set_frame_cb(player_frame);
    uint32_t now = lv_tick_get();
    if (s_has_audio && audio_active(now)) {
        /* A chunk from the previous player can outlive Back by less than a
           second. Hold the new video at frame zero instead of overlapping or
           starting it late, then restart cleanly when that chunk expires. */
        s_restart_pending = true;
        s_playing = false;
        hb_wake_lock(true);
        update_play_icon();
        lv_obj_set_style_text_color(s_status_label, lv_color_hex(0x9aa5b1), 0);
        lv_label_set_text(s_status_label, "Waiting for audio");
    } else {
        set_playing(true);
        try_start_audio(now);
    }
}

/* ---------------------------------------------------------------- library -- */

static void row_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    build_player(idx);
}

static void refresh_cb(lv_event_t *e)
{
    (void)e;
    if (s_library) { lv_obj_delete(s_library); s_library = 0; }
    scan_videos(); build_library();
}

static void build_library(void)
{
    s_library = plain_box(s_screen, 0, 0, 240, 432, hb_color_bg());
    lv_obj_set_flex_flow(s_library, LV_FLEX_FLOW_COLUMN);

    lv_obj_t *header = lv_obj_create(s_library);
    lv_obj_remove_style_all(header);
    lv_obj_set_size(header, 240, 48);
    lv_obj_set_flex_flow(header, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(header, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_hor(header, 10, 0);
    lv_obj_set_style_pad_column(header, 6, 0);
    lv_obj_t *title = lv_label_create(header);
    lv_label_set_text(title, "Video Player");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0xff375f), 0);
    lv_obj_set_flex_grow(title, 1);
    lv_obj_t *refresh = lv_button_create(header);
    lv_obj_set_size(refresh, 36, 36); lv_obj_set_style_pad_all(refresh, 0, 0);
    lv_obj_add_event_cb(refresh, refresh_cb, LV_EVENT_CLICKED, 0);
    lv_obj_t *r = lv_label_create(refresh); lv_label_set_text(r, LV_SYMBOL_REFRESH); lv_obj_center(r);

    if (s_n_videos == 0) {
        lv_obj_t *empty = lv_label_create(s_library);
        lv_obj_set_width(empty, 210);
        lv_label_set_long_mode(empty, LV_LABEL_LONG_WRAP);
        lv_obj_set_style_text_align(empty, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_color(empty, lv_color_hex(0x9aa5b1), 0);
        lv_label_set_text(empty, "No videos\n\nConvert with tools/make_hbv.py, then copy .hbv + .audio to /Apps/Data/Videos");
        lv_obj_align(empty, LV_ALIGN_CENTER, 0, 0);
        return;
    }

    lv_obj_t *list = lv_obj_create(s_library);
    lv_obj_remove_style_all(list);
    lv_obj_set_width(list, 240); lv_obj_set_flex_grow(list, 1);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(list, 8, 0); lv_obj_set_style_pad_row(list, 7, 0);
    lv_obj_set_scroll_dir(list, LV_DIR_VER);

    for (int i = 0; i < s_n_videos; i++) {
        lv_obj_t *row = lv_button_create(list);
        lv_obj_set_width(row, lv_pct(100)); lv_obj_set_height(row, 54);
        lv_obj_set_style_radius(row, 7, 0);
        lv_obj_set_style_bg_color(row, lv_color_hex(hb_color_surface()), 0);
        lv_obj_add_event_cb(row, row_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);

        lv_obj_t *play = lv_label_create(row);
        lv_label_set_text(play, LV_SYMBOL_PLAY);
        lv_obj_set_style_text_color(play, lv_color_hex(0xff375f), 0);
        lv_obj_align(play, LV_ALIGN_LEFT_MID, 0, 0);

        char title_text[MAX_NAME]; display_name(title_text, s_videos[i].name, sizeof title_text);
        lv_obj_t *name = lv_label_create(row);
        lv_obj_set_pos(name, 30, 2); lv_obj_set_width(name, 142);
        lv_label_set_long_mode(name, LV_LABEL_LONG_DOT);
        lv_obj_set_style_text_color(name, lv_color_hex(hb_color_text()), 0);
        lv_label_set_text(name, title_text);

        char size[24]; format_size(s_videos[i].size, size);
        if (s_videos[i].has_audio) {
            int k = s_len(size);
            const char *tag = " + audio";
            while (*tag && k < 23) size[k++] = *tag++;
            size[k] = 0;
        }
        lv_obj_t *sz = lv_label_create(row);
        lv_obj_set_pos(sz, 30, 25); lv_obj_set_width(sz, 142);
        lv_obj_set_style_text_font(sz, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(sz, lv_color_hex(hb_color_text_dim()), 0);
        lv_label_set_text(sz, size);
    }
}

void lv_app_main(void)
{
    s_screen = lv_screen_active();
    lv_obj_set_style_bg_color(s_screen, lv_color_hex(hb_color_bg()), 0);
    lv_obj_set_style_bg_opa(s_screen, LV_OPA_COVER, 0);
    hb_fs_mkdir("/Apps/Data");
    hb_fs_mkdir(DATA_DIR);
    hb_lv_set_exit_cb(player_free);
    scan_videos();
    build_library();
}

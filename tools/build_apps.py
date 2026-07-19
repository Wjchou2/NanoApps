#!/usr/bin/env python3
"""Build the homebrew /Apps image for the relocatable LVGL surface model.

The per-user staging tree under the system temporary directory mirrors the
device 1:1 (it IS the /Apps image), so install is a plain recursive copy. Apps
are ordered alphabetically by CFBundleName — the pack order, icon-id assignment
and the k-th icon file all follow it, so the on-disk order is derivable from the
names alone.

For every app that builds (apps/<name>/build/<name>.hbapp):
  - generate a 112x112 home icon (glossy 90x90 color disc + white FontAwesome
    glyph, OS-sampled palette) -> <stage>/Apps/Icons/<CFBundleName>.bin,
  - copy the relocatable blob -> <stage>/Apps/Executables/<CFBundleName>.hbapp,
  - make a bundle (label + a SHARED custom screen; NO icon/code section — those
    are separate disk files) and pack them all into Apps/AllApps.pack.

Everything we install lives under /Apps — nothing of ours is written to
/iPod_Control.

Usage: build_apps.py [app1 app2 ...]   (default: every app with a built .hbapp)
"""
import sys, os, struct, subprocess, json, plistlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "dependencies" / "ipodhax"))
from ipodhax.silverdb import pack_silverdb
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageFilter

# shared custom screen the resident injects into (all surface apps reuse it)
# Surface screen DB id, generated from tools/screens/ by build_screens() below.
# One screen shared by all surface apps; apps that
# should stay lit call hb_wake_lock(true) at entry (a runtime idle-timer reset —
# the OS dim-permission screen flag can't gate a homebrew-pushed controller).
HBSCREEN = 229474306        # HBCustom_Screen (0x0dad8002)
HBLAYOUT = 229474305        # HBCustom_Layout (0x0dad8001)
HBAP = 0x48426170     # 'HBap' label chain tag
HBSD = 0x48425344     # 'HBSD' screen chain tag

# Per-app metadata now lives next to each app in apps/<name>/Info.plist (the same
# file that ships in the on-device bundle and the resident parses). Keys:
#   CFBundleIdentifier  stable bundle id (identity)
#   CFBundleName        home-screen label
#   CFBundleExecutable  executable name
#   HBAppKind           1 = X+ framebuffer surface (default; LVGL or raw
#                           direct-draw — same composited view), 2 = GL surface
#   HBIconGlyph         FontAwesome glyph name for the generated icon
#   HBIconColor         icon disc color, "#rrggbb"
# The GLYPH/PALETTE tables below are now only FALLBACKS for an app with no plist.
SCREEN = ROOT / "tools" / "hb_silverdb.bin"
SIZE = 112            # match the stock springboard icon size
FMT = 0x1888          # 32-bit pixel-format token used in icon filenames
ICON_BASE_ID = 229441873   # placeholder ids for pack_silverdb; the registrar
                           # extends the bitmap cache at a custom offset and
                           # reassigns the real ids (see register_bitmaps)

# Vibrant tile colors sampled from the stock iPod springboard icons:
# gold, purple, cyan, red, sky/azure blue,
# green, brown, steel — assigned round-robin so adjacent apps differ.
PALETTE = [
    (0xf2, 0xbc, 0x02), (0xc4, 0x68, 0xee), (0x38, 0xc8, 0xf5), (0xe6, 0x5d, 0x53),
    (0x0d, 0xa5, 0xf5), (0x33, 0xc0, 0x59), (0xa0, 0x54, 0x34), (0x69, 0x80, 0x9d),
    (0xe6, 0x99, 0x34), (0x29, 0x80, 0xb9), (0xc0, 0x39, 0x2b), (0x8e, 0x44, 0xad),
]

# Per-app FontAwesome (Free Solid 900) glyph. Names validated against the
# bundled icons.json; "_default" covers anything not listed.
FA_DIR = ROOT / "tools" / "dependencies" / "fontawesome"
FA_OTF = str(FA_DIR / "otfs" / "Font Awesome 7 Free-Solid-900.otf")
FA_META = json.load(open(FA_DIR / "metadata" / "icons.json"))
GLYPH = {
    "benchmark": "gauge-high", "books": "book", "bowling": "bowling-ball",
    "breakout": "cubes-stacked", "calculator": "calculator", "calculator2": "calculator",
    "calendar": "calendar-days", "chess": "chess-knight", "clock": "clock",
    "connect4": "circle", "contacts": "address-book", "converter": "right-left",
    "countdown_days": "hourglass-half", "counter": "hashtag", "crosswords": "table-cells",
    "dictionary": "book-open", "file_browser": "folder-open", "freecell": "diamond",
    "g2048": "table-cells", "gba": "gamepad", "golf": "golf-ball-tee",
    "heads_up": "lightbulb", "holdem": "coins", "keypad_encoder": "keyboard",
    "lawn": "seedling", "lv_surface": "display", "mahjong": "border-all",
    "match3": "gem", "minesweeper": "bomb", "music_remote": "music",
    "notes": "note-sticky", "paint": "paintbrush", "passwords": "key",
    "periodic_table": "atom", "pinball": "circle-dot", "pong": "table-tennis-paddle-ball",
    "quiz": "circle-question", "reminders": "list-check", "rhythm": "drum",
    "screenshot": "camera", "settings": "gear", "showcase": "wand-magic-sparkles", "simon_says": "clone",
    "skateboard": "bolt", "slicer": "scissors", "slide_clicker": "arrow-pointer",
    "snake": "worm", "solitaire": "clover", "spirit_level": "ruler-horizontal",
    "streamdeck": "table-cells-large", "sudoku": "border-all", "tetris": "shapes",
    "tictactoe": "hashtag", "tilt_ball": "circle", "toolbox": "toolbox",
    "tower": "layer-group", "wallet": "wallet", "wav_player": "play",
    "widgets": "shapes", "wiki": "book-open-reader", "wordle": "font", "zuma": "circle-dot",
    "_default": "shapes",
}

# disc geometry within the 112x112 tile (top-left origin): 88x89 disc at (12, 6)
CIRC_X0, CIRC_Y0, CIRC_W, CIRC_H = 12, 6, 88, 89
CIRC_X1, CIRC_Y1 = CIRC_X0 + CIRC_W, CIRC_Y0 + CIRC_H


def nice(name):
    return name.replace("_", " ").title()[:23]

def hex_rgb(s, fallback):
    """'#rrggbb' -> (r,g,b); fallback tuple on anything malformed."""
    try:
        s = s.lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return fallback

def load_meta(app, i):
    """Per-app metadata from apps/<app>/Info.plist, with table fallbacks for any
    app that hasn't got one yet (keeps unmigrated apps building)."""
    fb_color = PALETTE[i % len(PALETTE)]
    meta = {
        "bundle_id": f"org.nanoapps.{app}",
        "label":     LABELS_FALLBACK.get(app, nice(app)),
        "kind":      1,
        "glyph":     GLYPH.get(app, GLYPH["_default"]),
        "color":     fb_color,
    }
    p = ROOT / "apps" / app / "Info.plist"
    if p.exists():
        d = plistlib.loads(p.read_bytes())
        meta["bundle_id"] = d.get("CFBundleIdentifier", meta["bundle_id"])
        meta["label"]     = d.get("CFBundleName", meta["label"])[:23]
        meta["kind"]      = int(d.get("HBAppKind", meta["kind"]))
        meta["glyph"]     = d.get("HBIconGlyph", meta["glyph"])
        meta["color"]     = hex_rgb(d.get("HBIconColor", ""), fb_color)
    return meta

LABELS_FALLBACK = {"agl": "AGL"}

# sbid: the springboard item identity the OS persists home-screen position by, and
# the resident's tap router matches. Name-based ("hb.<folder>") — stable per app
# (folder names don't change), unique, and never a bare number. No registry file.
def sbid_for(app_dir):
    return "hb." + app_dir

# app_id doubles as the home-label resource id. It must be a UNIQUE integer >=
# OUR_ID_BASE (0x0dad8000) and clear of the screen/layout ids (0x0dad8001/2) — an
# internal OS resource id, not a device-facing identifier. Derive it stably from
# the bundle id with an FNV-1a hash + linear probe (deterministic, sorted), so no
# committed slot registry is needed.
def assign_app_ids(bundle_ids):
    LO, HI = 0x0dad8100, 0x0dadff00          # clear of 0x0dad8001/2
    span = HI - LO
    used, out = set(), {}
    for bid in sorted(bundle_ids):
        h = 0x811c9dc5
        for ch in bid.encode():
            h = ((h ^ ch) * 0x01000193) & 0xffffffff
        x = LO + (h % span)
        while x in used:
            x = LO + ((x - LO + 1) % span)
        used.add(x); out[bid] = x
    return out

def _scale(rgb, f):
    return tuple(max(0, min(255, int(c * f))) for c in rgb)

def _glyph_mask(fa_name, px):
    """Render a FontAwesome glyph as a tight alpha (L) mask at `px`."""
    name = fa_name if fa_name in FA_META else GLYPH["_default"]
    cp = chr(int(FA_META[name]["unicode"], 16))
    font = ImageFont.truetype(FA_OTF, px)
    big = Image.new("L", (px * 3, px * 3), 0)
    d = ImageDraw.Draw(big)
    bb = d.textbbox((0, 0), cp, font=font)
    d.text((-bb[0], -bb[1]), cp, font=font, fill=255)
    return big.crop((0, 0, max(1, bb[2] - bb[0]), max(1, bb[3] - bb[1])))

def make_png(fa_name, rgb, path):
    """Glossy circular springboard icon. Rendered 4x supersampled and downsampled
    so the disc border + glyph edges are cleanly anti-aliased; centred disc; a
    defined top-gloss highlight with a crisp lower edge."""
    SS = 4
    S  = SIZE * SS
    x0, y0 = int(round(CIRC_X0 * SS)), int(round(CIRC_Y0 * SS))
    dw, dh = int(round(CIRC_W * SS)), int(round(CIRC_H * SS))
    x1, y1 = x0 + dw, y0 + dh
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # soft drop shadow under the disc. The shadow ellipse is inset from the disc on
    # the top + sides (and offset down) by more than the blur spread, so even after
    # the GaussianBlur expands it the result never pokes past the disc's top/side
    # rim (no halo) — it only extends below, as a drop shadow should.
    sm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sm).ellipse([x0 + 4 * SS, y0 + 7 * SS, x1 - 4 * SS, y1 + 3 * SS], fill=70)
    sm = sm.filter(ImageFilter.GaussianBlur(3 * SS))
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0)); sh.putalpha(sm)
    img = Image.alpha_composite(img, sh)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([x0, y0, x1, y1], fill=255)

    # vertical gradient (lighter top), iOS-6 flavour
    top, bot = _scale(rgb, 1.18), _scale(rgb, 0.86)
    grad = Image.new("RGBA", (S, S)); gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = min(1.0, max(0.0, (y - y0) / dh))
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        gd.line([(0, y), (S, y)], fill=c + (255,))
    grad.putalpha(mask)
    img = Image.alpha_composite(img, grad)

    # top gloss — a SHARP highlight (no blur). Its lower edge is an arc through
    # (left,46) (middle,52) (right,46) in 112-tile space, so it bulges gently
    # downward at the centre. The gloss fills the disc above that arc; the only
    # smoothing is the 4x supersample + downsample, keeping the edge crisp.
    GLOSS_A = 105
    axc = CIRC_X0 + CIRC_W / 2.0                       # arc centre x (disc middle)
    acu = (46 - 52) / (CIRC_X0 - axc) ** 2             # parabola y = acu(x-axc)^2 + 52
    pts = [(0, 0), (S, 0)]
    NSEG = 96
    for k in range(NSEG + 1):
        x = SIZE * (NSEG - k) / NSEG                   # sweep right -> left
        y = acu * (x - axc) ** 2 + 52
        pts.append((x * SS, y * SS))
    ga = Image.new("L", (S, S), 0)
    ImageDraw.Draw(ga).polygon(pts, fill=GLOSS_A)
    ga = ImageChops.multiply(ga, mask)
    gloss = Image.new("RGBA", (S, S), (255, 255, 255, 0)); gloss.putalpha(ga)
    img = Image.alpha_composite(img, gloss)

    # 1px inner border at 25% black — drawn WITHIN the disc edge (over the color),
    # so it reads as a darkened rim rather than an outside stroke.
    bw = SS                                                   # 1px final
    inner = Image.new("L", (S, S), 0)
    ImageDraw.Draw(inner).ellipse([x0 + bw, y0 + bw, x1 - bw, y1 - bw], fill=255)
    ring = ImageChops.subtract(mask, inner)
    bdr = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bdr.putalpha(ring.point(lambda v: int(v * 0.25)))         # 25% alpha black
    img = Image.alpha_composite(img, bdr)

    # white glyph, centred in the disc, with a faint thin outline (no drop shadow):
    # a ~0.5px dark ring at 20% alpha for just enough edge definition.
    gm = _glyph_mask(fa_name, int(dw * 0.50))
    gw, gh = gm.size
    gx, gy = x0 + (dw - gw) // 2, y0 + (dh - gh) // 2
    ga2 = Image.new("L", (S, S), 0); ga2.paste(gm, (gx, gy))
    outline = ga2.filter(ImageFilter.MaxFilter(SS + 1))       # dilate 2 SS px = ~0.5px final
    oa = outline.point(lambda v: int(v * 0.20))               # 20% alpha
    ol = Image.new("RGBA", (S, S), (0, 0, 0, 0)); ol.putalpha(oa)
    img = Image.alpha_composite(img, ol)
    gl = Image.new("RGBA", (S, S), (255, 255, 255, 0)); gl.putalpha(ga2)
    img = Image.alpha_composite(img, gl)

    img.resize((SIZE, SIZE), Image.LANCZOS).save(path)

def build_screens():
    """Generate the homebrew surface screen DB (tools/hb_silverdb.bin) from the
    source screens (tools/screens/*.yaml) using the silverutil resource
    compiler. Two screens share one layout (229474305):
      229474306  "dim"    — idle flag set (default apps may auto-dim)
      229474320  "no-dim" — idle flag clear (panel stays lit; video/slideshow)
    An app picks one via the screen id it pushes (see NODIM_APPS)."""
    su = ROOT / "tools" / "dependencies" / "silverutil" / "target" / "release" / "silverutil"
    if not su.is_file() or not os.access(su, os.X_OK):
        print("==> building silverutil")
        # --locked: honor the committed Cargo.lock, which is resolved MSRV-aware
        # (rust-version = 1.85) so deps like `image` don't float up to a release
        # that needs a newer rustc than the build machine has.
        subprocess.run(["cargo", "build", "--release", "--locked"],
                       cwd=str(su.parents[2]), check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(su), "create", str(ROOT / "tools" / "screens"),
                    str(ROOT / "tools" / "hb_silverdb.bin")], check=True)


def main(apps):
    # Regenerate the surface screen DB from the source screens (tools/screens/)
    # so the dim/no-dim screens always match the committed source.
    if (ROOT / "tools" / "screens").is_dir():
        build_screens()
    # Clean slate. A per-user path prevents an old root-owned staging directory
    # (for example, from running start with sudo) from breaking later normal runs.
    # NANOAPPS_STAGE lets the launcher pass the exact same path explicitly.
    # The staging tree mirrors the device 1:1 — it
    # IS the /Apps image (AllApps.pack + Executables/ + Icons/), so install is a
    # plain recursive copy, no rename/remap. Build scratch (intermediate .app
    # bundles, icon db) lives in a SEPARATE dir so it never ships.
    import shutil as _sh, tempfile
    uid = os.getuid() if hasattr(os, "getuid") else 0
    out = Path(os.environ.get(
        "NANOAPPS_STAGE", Path(tempfile.gettempdir()) / f"nanoapps-hbapps-{uid}"))
    try:
        _sh.rmtree(out)
    except FileNotFoundError:
        pass
    except PermissionError as exc:
        sys.exit(f"cannot clean staging directory {out}: {exc}\n"
                 "Remove that directory once as its owner, then rerun ./start.")
    appsd  = out / "Apps"            # == device /Apps
    execd  = appsd / "Executables"  # == device /Apps/Executables/<CFBundleName>.hbapp
    iconsd = appsd / "Icons"        # == device /Apps/Icons/<CFBundleName>.bin
    execd.mkdir(parents=True, exist_ok=True)
    iconsd.mkdir(parents=True, exist_ok=True)
    # Throwaway scratch: the intermediate .app bundles + icon SilverDB that mkapp.py
    # (bundle->pack) and pack_silverdb hand off through files. Auto-deleted at the
    # end — nothing here ships, and it shouldn't linger to confuse the /Apps tree.
    scratch = Path(tempfile.mkdtemp(prefix="hbapps_build_"))
    icons_src = scratch / "icons_src"
    icons_src.mkdir(parents=True, exist_ok=True)

    # discover apps with a built relocatable blob (.hbapp)
    if not apps:
        apps = sorted(d.name for d in (ROOT / "apps").iterdir()
                      if (d / "build" / f"{d.name}.hbapp").exists())
    # keep only real surface apps (exclude the resident etc.)
    apps = [a for a in apps if (ROOT / "apps" / a / "build" / f"{a}.hbapp").exists()
            and a != "silver_resident"]

    # per-app metadata (Info.plist next to each app, table fallback otherwise)
    metas = [load_meta(a, i) for i, a in enumerate(apps)]

    # Canonical order = alphabetical by CFBundleName. Everything downstream (pack
    # order, icon id assignment, the k-th icon file) follows this, so the on-disk
    # icon/exe order is derivable from the names alone — no separate ordering file.
    order = sorted(range(len(apps)), key=lambda i: metas[i]["label"].lower())
    apps  = [apps[i]  for i in order]
    metas = [metas[i] for i in order]

    # 1. icons. An app may ship its own apps/<name>/icon.png (used verbatim,
    # resized to the springboard tile); otherwise we generate a glossy disc from
    # the Info.plist glyph + color.
    for i, a in enumerate(apps):
        dst = icons_src / f"{ICON_BASE_ID + i}_{FMT:04x}.png"
        custom = ROOT / "apps" / a / "icon.png"
        if custom.exists():
            Image.open(custom).convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS).save(dst)
        else:
            make_png(metas[i]["glyph"], metas[i]["color"], dst)
    icondb = scratch / "icons.bin"
    with open(icondb, "wb") as f: pack_silverdb(f, icons_src)
    data = open(icondb, "rb").read()
    ds = struct.unpack_from("<I", data, 4)[0]
    cnt = struct.unpack_from("<I", data, 16)[0]
    icon_blobs = []
    for k in range(cnt):
        off = struct.unpack_from("<I", data, 28 + k * 12 + 4)[0]
        ln = struct.unpack_from("<I", data, 28 + k * 12 + 8)[0]
        icon_blobs.append(data[ds + off:ds + off + ln])

    # Lazy serving: write each icon straight to the shipped file
    # /Apps/Icons/<bundle-name>.bin (each bitmap is exactly what
    # pack_silverdb emits above). The resident registers the ids empty + serves
    # them on demand from these files (not held in heap / the pack); the resolver
    # maps the k-th cache id to the k-th app's name.
    for i in range(len(apps)):
        (iconsd / f"{metas[i]['label']}.bin").write_bytes(icon_blobs[i])

    # 2. bundles (label + screen ref only) + copy each blob to
    #    Apps/Executables/<CFBundleName>.hbapp
    #
    # Contract enforced by the resident (silver_resident.c):
    #   - label-id / app-id MUST be >= OUR_ID_BASE (0x0dad8000) or getres_hook
    #     passes the id through to the master DB and the springboard resolves it
    #     to a STOCK string (we saw "%d Photos"/"%d Events"). assign_app_ids keeps
    #     them in [0x0dad8100, 0x0dadff00), clear of the screen/layout ids.
    #   - sbid is "hb.<folder>" (assign_app_ids' sibling sbid_for) — a stable,
    #     unique, NON-numeric springboard identity. The OS persists home-screen
    #     reorder keyed by sbid, so a name-based sbid keeps an app's position across
    #     other apps being added/removed. The tap router matches it by STRING.
    #   - the launch path uses CFBundleName: the resident reads the blob from
    #     /Apps/Executables/<CFBundleName>.hbapp and the icon from
    #     /Apps/Icons/<CFBundleName>.bin, so both files are named to match.
    app_ids = assign_app_ids([m["bundle_id"] for m in metas])
    bundles = []
    mapping = []
    for i, a in enumerate(apps):
        app_id = app_ids[metas[i]["bundle_id"]]
        sbid = sbid_for(a)
        label = metas[i]["label"]
        # The resident copies sbid/name into 24-byte launch-record fields (cap 23).
        # Fail loudly here rather than silently truncate (which would break tap
        # routing). label is already capped at 23 by load_meta.
        if len(sbid) > 23:
            sys.exit(f"sbid '{sbid}' > 23 chars — shorten the apps/{a} folder name")
        appf = scratch / f"{a}.app"
        subprocess.run(["python3", str(ROOT / "tools" / "mkapp.py"), "bundle",
            str(appf), "--id", hex(app_id), "--kind", str(metas[i]["kind"]),
            "--sbid", sbid, "--name", label,
            "--label-id", hex(app_id), "--label-tag", hex(HBAP), "--label-text", label,
            "--screen-id", str(HBSCREEN), "--layout-id", str(HBLAYOUT),
            "--screen-tag", hex(HBSD), "--screen", str(SCREEN)],
            check=True, stdout=subprocess.DEVNULL)
            # NOTE: no --icon — icons are served lazily from /Apps/Icons/
        bundles.append(str(appf))
        # device executable = the relocatable .hbapp blob (resident hb_reloc-loads
        # it into an operator-new arena), named by CFBundleName so the launch path
        # /Apps/Executables/<CFBundleName>.hbapp reads it directly.
        binsrc = ROOT / "apps" / a / "build" / f"{a}.hbapp"
        (execd / f"{label}.hbapp").write_bytes(binsrc.read_bytes())
        mapping.append(f"{label}({sbid})")

    # 3. pack — the single manifest the resident reads (app identity + order)
    pack = appsd / "AllApps.pack"
    subprocess.run(["python3", str(ROOT / "tools" / "mkapp.py"), "pack",
                    str(pack)] + bundles, check=True)
    print(f"packed {len(apps)} apps -> {pack} ({pack.stat().st_size} bytes); "
          f"exes in {execd}, icons in {iconsd}")
    print("  " + "  ".join(mapping))
    # NOTE: per-app data lives in the repo's top-level data/<Name>/ (PascalCase,
    # matching each app's /Apps/Data/<Name> path) and is copied to the device on
    # demand by `nano deploy --data` (skip-existing, so user edits survive) — never
    # wiped by a normal app deploy (see nano's _install_disk).
    _sh.rmtree(scratch, ignore_errors=True)   # drop build scratch; only Apps/ ships

if __name__ == "__main__":
    main(sys.argv[1:])

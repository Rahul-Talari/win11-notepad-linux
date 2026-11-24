"""End-to-end editing tests: runs the real app on a hidden display.

Run with:  python3 tests/test_gui.py

Needs gtk4-broadwayd (part of GTK 4 on Ubuntu: package libgtk-4-bin).
Uses throwaway settings, so your own Notepad settings and session are not touched.
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def relaunch_headless():
    """Re-run this script inside a private broadway display."""
    if not shutil.which("gtk4-broadwayd"):
        sys.exit("gtk4-broadwayd not found (sudo apt install libgtk-4-bin)")
    tmp = tempfile.mkdtemp(prefix="notepad-test-")
    display = ":%d" % random.randint(20, 90)
    server = subprocess.Popen(["gtk4-broadwayd", display],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    env = dict(os.environ, GDK_BACKEND="broadway", BROADWAY_DISPLAY=display,
               GSETTINGS_BACKEND="memory", NOTEPAD_TEST_CHILD="1",
               XDG_CONFIG_HOME=os.path.join(tmp, "config"),
               XDG_DATA_HOME=os.path.join(tmp, "data"))
    try:
        result = subprocess.run([sys.executable, "-I", __file__], env=env, timeout=180,
                                stderr=subprocess.PIPE, text=True)
        noise = [ln for ln in result.stderr.splitlines() if "Theme parser" not in ln and ln.strip()]
        if noise:
            print("\n".join(noise), file=sys.stderr)
        return result.returncode
    finally:
        server.terminate()
        shutil.rmtree(tmp, ignore_errors=True)


if os.environ.get("NOTEPAD_TEST_CHILD") != "1":
    sys.exit(relaunch_headless())

sys.path.insert(0, ROOT)
import notepad as n  # noqa: E402
from gi.repository import GLib, Gio, Gdk, Gtk  # noqa: E402


class TestApp(n.NotepadApp):
    def __init__(self):
        super().__init__()
        self.set_application_id("io.local.NotepadTests")
        self.set_flags(Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.NON_UNIQUE)


app = TestApp()
results = []


def pump(ms=250):
    end = GLib.get_monotonic_time() + ms * 1000
    ctx = GLib.MainContext.default()
    while GLib.get_monotonic_time() < end:
        ctx.iteration(False)


def check(name, cond, info=""):
    results.append((bool(cond), name, info))


def key(d, keyval, shift=False):
    """Press a key the way the text view would receive it."""
    state = Gdk.ModifierType.SHIFT_MASK if shift else Gdk.ModifierType(0)
    if not d.on_key(None, keyval, 0, state):
        b = d.buffer
        it = b.get_iter_at_mark(b.get_insert())
        if keyval == Gdk.KEY_BackSpace:
            b.backspace(it, True, True)
        elif keyval == Gdk.KEY_Delete:
            e = it.copy()
            e.forward_cursor_position()
            b.delete(it, e)
        elif keyval == Gdk.KEY_Return:
            b.insert_at_cursor("\n")
        elif keyval == Gdk.KEY_Tab:
            b.insert_at_cursor("\t")
    pump()


def type_text(d, s):
    for ch in s:
        d.buffer.insert_interactive_at_cursor(ch, -1, True)
        pump(60)
    pump()


def tag_state(b, tags):
    out, it = [], b.get_start_iter()
    while not it.is_end():
        out.append(tuple(t.get_property("name") for t in tags if it.has_tag(t)))
        it.forward_char()
    return out


def run():
    w = app.active_notepad()
    w.present()
    pump(600)
    d = w.new_tab(markdown=True)
    b = d.buffer
    hidden = d.formatter.hidden
    text = d.get_text

    def put(t, cursor):
        b.set_text(t)
        pump(300)
        b.place_cursor(b.get_iter_at_offset(cursor))
        pump()

    put("say **bold** ok", 8)
    check("syntax hidden on the cursor's own line", b.get_iter_at_offset(4).has_tag(hidden))

    put("say **bold** ok", len("say **bold**"))
    key(d, Gdk.KEY_BackSpace)
    check("Backspace after bold deletes the visible letter", text() == "say **bol** ok", text())
    for _ in range(3):
        key(d, Gdk.KEY_BackSpace)
    check("deleting all bold letters removes the empty **** too", text() == "say  ok", text())

    put("x**bold**", 1)
    key(d, Gdk.KEY_Delete)
    check("Delete before bold deletes the visible letter", text() == "x**old**", text())

    put("hello ", 6)
    w.activate_action("win.bold", None)
    pump()
    type_text(d, "world")
    check("Ctrl+B with nothing selected, then typing, gives bold", text() == "hello **world**", text())
    w.activate_action("win.bold", None)
    pump()
    type_text(d, " after")
    check("Ctrl+B again at the end of bold stops bold", text() == "hello **world** after", text())

    put("**bold text**", len("**bold"))
    key(d, Gdk.KEY_Return)
    check("Enter inside bold keeps both halves bold", text() == "**bold**\n\n** text**", text())

    put("First", 5)
    key(d, Gdk.KEY_Return)
    type_text(d, "Second")
    check("Enter writes a paragraph break like Windows", text() == "First\n\nSecond", text())
    key(d, Gdk.KEY_Return, shift=True)
    type_text(d, "Third")
    check("Shift+Enter writes a plain line break", text() == "First\n\nSecond\nThird", text())
    b.place_cursor(b.get_iter_at_offset(7))
    pump()
    key(d, Gdk.KEY_BackSpace)
    check("Backspace at a paragraph start joins in one press", text() == "FirstSecond\nThird", text())

    put("- one", 5)
    key(d, Gdk.KEY_Return)
    check("Enter continues a bullet list", text() == "- one\n- ", text())
    key(d, Gdk.KEY_Return)
    check("Enter on an empty item ends the list", text() == "- one\n", text())

    put("intro\n\n# Title", 0)
    _, it = b.get_iter_at_line(2)
    b.place_cursor(it)
    pump()
    check("cursor skips the hidden '# ' at a heading start",
          b.get_iter_at_mark(b.get_insert()).get_line_offset() == 2)
    type_text(d, "My ")
    check("typing at a heading start stays in the heading", text() == "intro\n\n# My Title", text())
    _, it = b.get_iter_at_line(2)
    b.place_cursor(it)
    pump()
    key(d, Gdk.KEY_BackSpace)
    check("Backspace at a heading start makes it normal text", text() == "intro\n\nMy Title", text())

    put("# Title\n\nbody", 4)
    w.activate_action("win.select-all", None)
    pump()
    check("Ctrl+A selects everything when the file starts with a heading",
          d.selected_text() == text(), d.selected_text())

    put("A\n\nB", 3)
    check("a Windows paragraph break shows as a plain line break",
          b.get_iter_at_offset(2).has_tag(hidden))

    put("A\n\nB\n\nC\n\nD", 0)
    for _ in range(3):
        d.view.emit("move-cursor", Gtk.MovementStep.DISPLAY_LINES, 1, True)   # Shift+Down
        pump(200)
    breaks = [2, 5, 8]          # the empty paragraph-break lines
    check("Shift+Down selects line by line", d.selected_text() == "A\n\nB\n\nC\n\n", d.selected_text())
    check("selecting with Shift+Down keeps paragraph breaks hidden",
          all(b.get_iter_at_offset(o).has_tag(hidden) for o in breaks),
          [b.get_iter_at_offset(o).has_tag(hidden) for o in breaks])

    def render():
        """Draw the window once: GTK only finishes laying out lines when it
        draws, and positions measured before that are estimates."""
        snap = Gtk.Snapshot()
        Gtk.WidgetPaintable.new(w).snapshot(snap, w.get_width(), w.get_height())
        snap.to_node()
        pump(100)

    def tab_matches(spaces):
        put("\tX\n" + " " * spaces + "X", 0)
        render()                    # apply any font/zoom change, as a real frame would
        d.apply_tabs(later=False)   # what the app's delayed re-measure does
        render()
        x_tab = d.view.get_cursor_locations(b.get_iter_at_offset(1))[0].x                # X after the Tab
        x_spaces = d.view.get_cursor_locations(b.get_iter_at_offset(3 + spaces))[0].x    # X after the spaces
        return abs(x_tab - x_spaces) <= 1, (x_tab, x_spaces)

    ok, info = tab_matches(8)
    check("a Tab is drawn 8 spaces wide by default (like Windows)", ok, info)
    w.activate_action("win.zoom-in", None)
    w.activate_action("win.zoom-in", None)
    pump(400)
    ok, info = tab_matches(8)
    check("Tab width stays 8 spaces when zoomed in", ok, info)
    w.activate_action("win.zoom-reset", None)
    app.set_setting("tab_width", 4)
    pump(400)
    ok, info = tab_matches(4)
    check("changing the Tab width setting applies immediately", ok, info)
    app.set_setting("tab_width", 8)
    pump(300)

    put("ab", 1)
    key(d, Gdk.KEY_Tab)
    check("the Tab key types 4 spaces", text() == "a    b", text())
    app.set_setting("tab_key", 0)
    put("ab", 1)
    key(d, Gdk.KEY_Tab)
    check("Tab key setting 'A Tab character' types a real Tab", text() == "a\tb", text())
    app.set_setting("tab_key", 4)

    put("say **bold** ok", 0)
    w.activate_action("win.syntax-view", None)
    pump(300)
    check("Markdown syntax view shows everything", not b.get_iter_at_offset(4).has_tag(hidden))
    w.activate_action("win.syntax-view", None)
    pump(300)

    # Incremental formatting must match a full re-format after any edits.
    rnd = random.Random(42)
    snippets = ["a", " ", "\n", "\n\n", "*", "**", "# ", "`", "```", "~~", "- ", "\\", "&#x20;", "===", "word"]
    put("# Title\n\nSome **bold** and *it*\n\n- a\n- b\n\n```\ncode\n```\n\ntext", 0)
    tags = d.formatter.all_tags
    mismatches = 0
    for _ in range(150):
        for _ in range(rnd.randint(1, 3)):
            size = b.get_char_count()
            if size and rnd.random() < 0.4:
                a = rnd.randint(0, size - 1)
                b.delete(b.get_iter_at_offset(a), b.get_iter_at_offset(min(size, a + rnd.randint(1, 5))))
            else:
                b.insert(b.get_iter_at_offset(rnd.randint(0, size)), rnd.choice(snippets))
        d._do_format()
        ref = Gtk.TextBuffer()
        ref_fmt = n.MarkdownFormatter(ref)
        ref.set_text(text())
        ref_fmt.apply(d.cursor_lines())
        if tag_state(b, tags) != tag_state(ref, ref_fmt.all_tags):
            mismatches += 1
    check("incremental formatting matches a full re-format (150 random edits)", mismatches == 0,
          f"{mismatches} mismatches")

    for doc in w.docs():
        doc.buffer.set_modified(False)
    app.settings["startup"] = "new"


def main_cb():
    try:
        run()
    except Exception:
        import traceback
        check("no crash", False, traceback.format_exc())
    finally:
        app.quit()
    return False


app.connect("activate", lambda *_: GLib.timeout_add(500, main_cb))
app.run([sys.argv[0]])
failed = [r for r in results if not r[0]]
for ok, name, info in results:
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok else f"  -> {info!r}"))
print(f"{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)

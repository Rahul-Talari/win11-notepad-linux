#!/usr/bin/env python3
"""Notepad - a GTK4 text editor modelled on the Windows 11 Notepad feature set.

Tabs, session restore, Markdown formatting (Ctrl+B / Ctrl+I, headings, lists,
links), find & replace, go to line, time/date, zoom, word wrap, status bar,
line-ending and encoding control, printing, recent files and settings.
"""
import bisect
import datetime
import html
import json
import os
import re
import sys
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

APP_ID = "io.local.Notepad"
APP_NAME = "Notepad"
VERSION = "1.0"
ICON = "accessories-text-editor"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "notepad-app")
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "notepad-app")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
SESSION_FILE = os.path.join(DATA_DIR, "session.json")

FORMAT_LIMIT = 400_000  # characters; larger documents are shown unformatted
MAX_RECENT = 10
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 10, 500, 10

DEFAULT_SETTINGS = {
    "theme": "system",          # system | light | dark
    "font": "Monospace 11",
    "word_wrap": True,
    "status_bar": True,
    "open_in": "tab",           # tab | window
    "startup": "continue",      # continue | new
    "markdown_new": True,       # formatting on for new tabs
    "recent_enabled": True,
    "recent": [],
    "default_eol": "\n",
    "tab_width": 8,             # a Tab is drawn this many spaces wide (Windows: 8)
    "tab_key": 4,               # the Tab key types this many spaces; 0 = a real Tab
}

ENCODINGS = [
    ("utf-8", "UTF-8"),
    ("utf-8-sig", "UTF-8 with BOM"),
    ("utf-16-le", "UTF-16 LE"),
    ("utf-16-be", "UTF-16 BE"),
    ("cp1252", "ANSI"),
]
ENCODING_LABELS = dict(ENCODINGS)
EOLS = [("\r\n", "Windows (CRLF)"), ("\n", "Unix (LF)"), ("\r", "Macintosh (CR)")]
EOL_LABELS = dict(EOLS)
EOL_KEYS = {"\r\n": "crlf", "\n": "lf", "\r": "cr"}
EOL_FROM_KEY = {v: k for k, v in EOL_KEYS.items()}


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------

def decode_bytes(data):
    """Return (text, encoding, eol) with text normalised to '\n' line endings."""
    if data.startswith(b"\xef\xbb\xbf"):
        text, enc = data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    elif data.startswith(b"\xff\xfe"):
        text, enc = data[2:].decode("utf-16-le", errors="replace"), "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        text, enc = data[2:].decode("utf-16-be", errors="replace"), "utf-16-be"
    else:
        try:
            text, enc = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, enc = data.decode("cp1252", errors="replace"), "cp1252"
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        eol = "\r\n"
    elif cr > lf:
        eol = "\r"
    elif lf > 0:
        eol = "\n"
    else:
        eol = None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, enc, eol


def encode_text(text, encoding, eol):
    if eol != "\n":
        text = text.replace("\n", eol)
    if encoding == "utf-16-le":
        return b"\xff\xfe" + text.encode("utf-16-le")
    if encoding == "utf-16-be":
        return b"\xfe\xff" + text.encode("utf-16-be")
    return text.encode(encoding)


def write_file_atomic(path, data):
    target = os.path.realpath(path)
    directory = os.path.dirname(target) or "."
    tmp = os.path.join(directory, f".{os.path.basename(target)}.notepad-tmp")
    mode = None
    if os.path.exists(target):
        mode = os.stat(target).st_mode & 0o7777
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, target)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------
# Markdown parsing (pure functions, no GTK)
# --------------------------------------------------------------------------

RE_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
RE_HEADING = re.compile(r"^(#{1,6})([ \t]+|$)")
RE_HR = re.compile(r"^\s{0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
RE_QUOTE = re.compile(r"^(?:\s{0,3}>[ \t]?)+")
RE_BULLET = re.compile(r"^(\s*)([-*+])([ \t]+)(\[[ xX]\][ \t]+)?")
RE_NUMBER = re.compile(r"^(\s*)(\d{1,9}[.)])([ \t]+)")
RE_LINE_PREFIX = re.compile(
    r"^(\s*)(?:#{1,6}[ \t]+|(?:>[ \t]?)+|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?|\d{1,9}[.)][ \t]+)")

RE_LINK = re.compile(r"(\[)([^\]\n]+)(\]\(\s*([^)\s]+)(?:\s+\"[^\"\n]*\")?\s*\))")
RE_AUTOLINK = re.compile(r"(?<![\w(\[/])(?:https?://|www\.)[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"]")

RE_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
RE_ENTITY = re.compile(r"&(#[xX][0-9a-fA-F]{1,6}|#[0-9]{1,7}|[A-Za-z][A-Za-z0-9]{1,31});")
BLANK_CHARS = {" ", "\u00a0", "\u2002", "\u2003", "\u2009", "\t"}

INLINE_RULES = [
    ("code", re.compile(r"(?<!\\)(`+)(?!`)(.+?)(?<!`)\1(?!`)"), "atomic"),
    ("escape", re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])"), "escape"),
    ("entity", RE_ENTITY, "entity"),
    ("link", RE_LINK, "link"),
    ("link", RE_AUTOLINK, "auto"),
    ("bolditalic", re.compile(r"(\*\*\*)(?=\S)(.+?)(?<=\S)(\*\*\*)"), "wrap"),
    ("bold", re.compile(r"(\*\*)(?=\S)(.+?)(?<=\S)(\*\*)"), "wrap"),
    ("bold", re.compile(r"(?<!\w)(__)(?=\S)(.+?)(?<=\S)(__)(?!\w)"), "wrap"),
    ("strike", re.compile(r"(~~)(?=\S)(.+?)(?<=\S)(~~)"), "wrap"),
    ("italic", re.compile(r"(?<!\*)(\*)(?=[^\s*])(.+?)(?<=[^\s*])(\*)(?!\*)"), "wrap"),
    ("italic", re.compile(r"(?<!\w)(_)(?=[^\s_])(.+?)(?<=[^\s_])(_)(?!\w)"), "wrap"),
]
WRAP_RULES = {"**": INLINE_RULES[6][1], "*": INLINE_RULES[9][1], "~~": INLINE_RULES[8][1]}
PH = "\x00"


def _mask(chars, a, b):
    for i in range(a, b):
        chars[i] = PH


def parse_inline(line, col0, base, lineno, spans, markers, ghosts=None):
    if ghosts is None:
        ghosts = []
    work = PH * col0 + line[col0:]
    for tag, rx, kind in INLINE_RULES:
        chars = None
        for m in rx.finditer(work):
            if chars is None:
                chars = list(work)
            if kind == "atomic":
                a, b = m.span()
                n = len(m.group(1))
                markers.append((base + a, base + a + n, lineno))
                markers.append((base + b - n, base + b, lineno))
                spans.append(("code", base + a + n, base + b - n))
                _mask(chars, a, b)
            elif kind == "escape":
                # hide the backslash, show the escaped character literally
                a, b = m.span()
                markers.append((base + a, base + a + 1, lineno))
                _mask(chars, a, b)
            elif kind == "entity":
                a, b = m.span()
                value = html.unescape(m.group(0))
                if value in BLANK_CHARS:
                    # keep one character cell, drawn invisibly, so it looks like a space
                    markers.append((base + a, base + b - 1, lineno))
                    ghosts.append((base + b - 1, base + b, lineno))
                    spans.append(("marker", base + b - 1, base + b))
                    _mask(chars, a, b)
                elif value == "&":
                    markers.append((base + a + 1, base + b, lineno))
                    _mask(chars, a, b)
            elif kind == "link":
                a, t0 = m.span(1)
                t1, b = m.span(3)
                markers.append((base + a, base + t0, lineno))
                markers.append((base + t1, base + b, lineno))
                spans.append(("link", base + t0, base + t1))
                _mask(chars, a, t0)
                _mask(chars, t1, b)
            elif kind == "auto":
                a, b = m.span()
                spans.append(("link", base + a, base + b))
                _mask(chars, a, b)
            else:
                a, t0 = m.span(1)
                t1, b = m.span(3)
                markers.append((base + a, base + t0, lineno))
                markers.append((base + t1, base + b, lineno))
                if tag == "bolditalic":
                    spans.append(("bold", base + t0, base + t1))
                    spans.append(("italic", base + t0, base + t1))
                else:
                    spans.append((tag, base + t0, base + t1))
                _mask(chars, a, t0)
                _mask(chars, t1, b)
        if chars is not None:
            work = "".join(chars)


def parse_markdown(text, ghosts=None, runs=None):
    """Return (spans, markers).

    spans:   list of (tag_name, start, end) character offsets
    markers: list of (start, end, line_number) for syntax characters to hide
    ghosts:  optional list, filled with (start, end, line_number) ranges that
             are drawn invisibly (one-cell stand-ins for &#x20; and friends)
    runs:    optional list, filled with runs of consecutive blank lines outside
             code blocks; each run is a list of (start, end, line_number) where
             [start, end) hides that blank line.  Windows 11 Notepad writes a
             blank line for every paragraph break, so a run of k blank lines is
             displayed as k // 2 empty lines.
    """
    if ghosts is None:
        ghosts = []
    if runs is None:
        runs = []
    spans, markers = [], []
    offset = 0
    in_fence = False
    fence = None
    total = len(text)
    lines = text.split("\n")
    setext_level = {}
    run = []
    last = len(lines) - 1
    for lineno, line in enumerate(lines):
        base = offset
        offset += len(line) + 1
        end_par = min(base + len(line) + 1, total)
        if not in_fence and not line.strip():
            if lineno < last:
                run.append((base, base + len(line) + 1, lineno))
            elif base > 0:
                # last line has no newline of its own: hide the one before it
                run.append((base - 1, base + len(line), lineno))
            if lineno == last and run:
                runs.append(run)
            continue
        if run:
            runs.append(run)
            run = []
        if lineno in setext_level and not in_fence:
            # the === / --- underline of a setext heading
            markers.append((base, base + len(line), lineno))
            spans.append((f"h{setext_level[lineno]}", base, base + len(line)))
            continue
        if (not in_fence and lineno + 1 < len(lines) and line.strip()
                and RE_SETEXT.match(lines[lineno + 1])
                and not RE_FENCE.match(line) and not RE_HEADING.match(line)
                and not RE_QUOTE.match(line) and not RE_BULLET.match(line)
                and not RE_NUMBER.match(line) and not RE_HR.match(line)
                and not line.startswith(("    ", "\t"))):
            level = 1 if lines[lineno + 1].strip().startswith("=") else 2
            setext_level[lineno + 1] = level
            spans.append((f"h{level}", base, base + len(line)))
            parse_inline(line, 0, base, lineno, spans, markers, ghosts)
            continue
        m = RE_FENCE.match(line)
        if in_fence or m:
            spans.append(("codeblock", base, end_par))
            if m and (not in_fence or m.group(1) == fence):
                if line:
                    markers.append((base, base + len(line), lineno))
                in_fence = not in_fence
                fence = m.group(1) if in_fence else None
            continue
        if not line.strip():
            continue
        if RE_HR.match(line):
            spans.append(("hr", base, base + len(line)))
            continue
        col = 0
        m = RE_HEADING.match(line)
        if m:
            level = len(m.group(1))
            spans.append((f"h{level}", base, base + len(line)))
            markers.append((base, base + m.end(), lineno))
            col = m.end()
        else:
            m = RE_QUOTE.match(line)
            if m:
                spans.append(("quote", base, base + len(line)))
                markers.append((base, base + m.end(), lineno))
                col = m.end()
            rest = line[col:]
            mb = RE_BULLET.match(rest)
            mn = None if mb else RE_NUMBER.match(rest)
            if mb:
                a, b = col + mb.start(2), col + mb.end(2)
                spans.append(("listmark", base + a, base + b))
                if mb.group(4):
                    c0 = col + mb.start(4)
                    spans.append(("listmark", base + c0, base + c0 + 3))
                col += mb.end()
            elif mn:
                a, b = col + mn.start(2), col + mn.end(2)
                spans.append(("listmark", base + a, base + b))
                col += mn.end()
        parse_inline(line, col, base, lineno, spans, markers, ghosts)
    return spans, markers


def find_link_at(line, col):
    for m in RE_LINK.finditer(line):
        if m.start() <= col <= m.end():
            return m.group(4)
    for m in RE_AUTOLINK.finditer(line):
        if m.start() <= col <= m.end():
            url = m.group(0)
            return url if "://" in url else "https://" + url
    return None


def _run_len(s, i, ch, direction):
    n = 0
    while 0 <= i < len(s) and s[i] == ch:
        n += 1
        i += direction
    return n


def _wrap_present(left, right, mark):
    n = len(mark)
    if left < n or right < n:
        return False
    if mark == "*":
        return left != 2 and right != 2
    return True


def toggle_wrap_text(full, so, eo, mark):
    """Toggle an inline marker around [so, eo).

    Returns (start, end, replacement, new_sel_start, new_sel_end)."""
    n = len(mark)
    ch = mark[0]
    left = _run_len(full, so - 1, ch, -1)
    right = _run_len(full, eo, ch, 1)
    if _wrap_present(left, right, mark):
        inner = full[so:eo]
        return so - n, eo + n, inner, so - n, eo - n
    sel = full[so:eo]
    left_in = _run_len(sel, 0, ch, 1)
    right_in = _run_len(sel, len(sel) - 1, ch, -1)
    if len(sel) > 2 * n and _wrap_present(left_in, right_in, mark):
        inner = sel[n:len(sel) - n]
        return so, eo, inner, so, so + len(inner)
    out = []
    for i, line in enumerate(sel.split("\n")):
        lead = ""
        if i > 0 or so == 0 or full[so - 1] == "\n":
            pm = RE_LINE_PREFIX.match(line)
            if pm:
                lead = line[:pm.end()]
                line = line[pm.end():]
        stripped = line.strip()
        if not stripped:
            out.append(lead + line)
            continue
        ls = line[:len(line) - len(line.lstrip())]
        rs = line[len(line.rstrip()):]
        out.append(f"{lead}{ls}{mark}{stripped}{mark}{rs}")
    rep = "\n".join(out)
    return so, eo, rep, so, so + len(rep)


def find_wrap_span(line, mark, col):
    rx = WRAP_RULES[mark]
    for m in rx.finditer(line):
        if m.start() <= col <= m.end():
            return m.start(), m.end()
    return None


def set_heading_lines(lines, level):
    out = []
    for ln in lines:
        body = re.sub(r"^#{1,6}[ \t]+", "", ln)
        if level > 0 and (body.strip() or len(lines) == 1):
            body = "#" * level + " " + body
        out.append(body)
    return out


def _strip_list(line):
    m = RE_BULLET.match(line) or RE_NUMBER.match(line)
    if m:
        return m.group(1), line[m.end():]
    indent = line[:len(line) - len(line.lstrip(" \t"))]
    return indent, line[len(indent):]


def toggle_bullet_lines(lines):
    nonempty = [ln for ln in lines if ln.strip()]
    remove = bool(nonempty) and all(RE_BULLET.match(ln) for ln in nonempty)
    out = []
    for ln in lines:
        if not ln.strip() and len(lines) > 1:
            out.append(ln)
            continue
        indent, body = _strip_list(ln)
        out.append(indent + body if remove else f"{indent}- {body}")
    return out


def toggle_number_lines(lines):
    nonempty = [ln for ln in lines if ln.strip()]
    remove = bool(nonempty) and all(RE_NUMBER.match(ln) for ln in nonempty)
    out, n = [], 1
    for ln in lines:
        if not ln.strip() and len(lines) > 1:
            out.append(ln)
            continue
        indent, body = _strip_list(ln)
        if remove:
            out.append(indent + body)
        else:
            out.append(f"{indent}{n}. {body}")
            n += 1
    return out


def clear_formatting_lines(lines):
    out = []
    for ln in lines:
        ln = RE_LINE_PREFIX.sub(lambda m: m.group(1), ln, count=1)
        ln = RE_LINK.sub(lambda m: m.group(2), ln)
        for tag, rx, kind in INLINE_RULES:
            if kind in ("wrap", "atomic"):
                ln = rx.sub(lambda m: m.group(2), ln)
        out.append(ln)
    return out


def looks_like_markdown(text):
    return bool(re.search(
        r"(^#{1,6} |\*\*|__|~~|`|^\s*[-*+] |^\s*\d+[.)] |^> |\[[^\]]+\]\([^)]+\))",
        text, re.M))


def derive_title(text):
    for line in text.split("\n"):
        line = RE_LINE_PREFIX.sub("", line).strip()
        line = re.sub(r"[*_~`#]", "", line).strip()
        if line:
            return line[:40].rstrip()
    return None


def safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    return name[:60] or "Untitled"


def split_marks_at(line, col):
    """Where and how to break a line at `col` without breaking inline styles.

    Returns (insert_col, closers, openers): insert the paragraph break at
    insert_col, preceded by `closers` and followed by `openers`, so bold /
    italic / strikethrough text stays styled on both sides."""
    spans = []
    for mark in ("**", "~~", "*"):
        for m in WRAP_RULES[mark].finditer(line):
            a, z, n = m.start(), m.end(), len(mark)
            if a < col < z:
                spans.append((a, z, n, mark))
    insert = col
    # cursor at the edge of a span's text: break outside the hidden markers
    for a, z, n, mark in spans:
        if insert == z - n:
            insert = z
        elif insert == a + n:
            insert = a
    inside = sorted((s for s in spans if s[0] + s[2] < insert < s[1] - s[2]), key=lambda s: s[0])
    closers = "".join(s[3] for s in reversed(inside))
    openers = "".join(s[3] for s in inside)
    return insert, closers, openers


EMPTY_PAIRS = ("******", "****", "~~~~", "**")


def empty_pair_at(text, pos):
    """If deleting left an empty style pair (e.g. '****') around pos, return its range."""
    for pair in EMPTY_PAIRS:
        half = len(pair) // 2
        a, z = pos - half, pos + half
        if a >= 0 and text[a:z] == pair:
            if pair == "**" and ((a > 0 and text[a - 1] == "*") or (z < len(text) and text[z] == "*")):
                continue
            return a, z
    return None


def tab_array(layout, spaces):
    """Tab stops every `spaces` space widths, measured in the layout's own font."""
    probe = layout.copy()
    probe.set_tabs(None)
    probe.set_attributes(None)
    probe.set_text(" " * spaces, -1)
    width = probe.get_size()[0]
    tabs = Pango.TabArray.new(1, False)      # one stop; Pango repeats the interval
    tabs.set_tab(0, Pango.TabAlign.LEFT, max(1, width))
    return tabs


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULT_SETTINGS)
        data = read_json(SETTINGS_FILE, {})
        if isinstance(data, dict):
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    self[k] = v

    def save(self):
        try:
            write_json(SETTINGS_FILE, dict(self))
        except OSError as e:
            print("notepad: could not save settings:", e, file=sys.stderr)


# --------------------------------------------------------------------------
# Markdown formatter (applies tags to a GtkTextBuffer)
# --------------------------------------------------------------------------

def rgba(spec):
    c = Gdk.RGBA()
    c.parse(spec)
    return c


class MarkdownFormatter:
    TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "bold", "italic", "strike", "code",
            "codeblock", "quote", "link", "listmark", "hr", "marker"]

    def __init__(self, buffer):
        self.buffer = buffer
        b = buffer
        scales = [1.6, 1.4, 1.25, 1.12, 1.0, 0.95]
        for i, s in enumerate(scales, 1):
            b.create_tag(f"h{i}", weight=Pango.Weight.BOLD, scale=s,
                         pixels_above_lines=4, pixels_below_lines=2)
        b.create_tag("bold", weight=Pango.Weight.BOLD)
        b.create_tag("italic", style=Pango.Style.ITALIC)
        b.create_tag("strike", strikethrough=True)
        b.create_tag("code", family="monospace",
                     background_rgba=rgba("rgba(127,127,127,0.18)"))
        b.create_tag("codeblock", family="monospace",
                     paragraph_background_rgba=rgba("rgba(127,127,127,0.12)"))
        b.create_tag("quote", style=Pango.Style.ITALIC, left_margin=36,
                     foreground_rgba=rgba("rgba(127,127,127,1)"))
        b.create_tag("link", underline=Pango.Underline.SINGLE,
                     foreground_rgba=rgba("#3584e4"))
        b.create_tag("listmark", weight=Pango.Weight.BOLD,
                     foreground_rgba=rgba("#3584e4"))
        b.create_tag("hr", foreground_rgba=rgba("rgba(127,127,127,0.8)"))
        b.create_tag("marker", foreground_rgba=rgba("rgba(127,127,127,0.85)"),
                     weight=Pango.Weight.NORMAL, style=Pango.Style.NORMAL)
        b.create_tag("hidden", invisible=True)
        # Pango treats alpha 0 as "unset", so use the smallest visible-to-nobody alpha
        b.create_tag("ghost", foreground_rgba=rgba("rgba(127,127,127,0.003)"))
        self.tag_objs = [b.get_tag_table().lookup(n) for n in self.TAGS]
        self.hidden = b.get_tag_table().lookup("hidden")
        self.ghost = b.get_tag_table().lookup("ghost")
        self.all_tags = self.tag_objs + [self.hidden, self.ghost]
        self.items = None
        self.edits = []
        self.markers = []
        self.ghosts = []
        self.runs = []
        self.run_first = []
        self.run_hidden = []
        self.stale = False
        self.reveal = None
        self.active = False

    def clear(self):
        b = self.buffer
        s, e = b.get_bounds()
        for t in self.all_tags:
            b.remove_tag(t, s, e)
        self.markers = []
        self.ghosts = []
        self.runs = []
        self.run_first = []
        self.run_hidden = []
        self.items = None
        self.edits = []
        self.reveal = None
        self.active = False

    # -- edit tracking (lets apply() restyle only what changed) -----------
    def note_insert(self, pos, n):
        if self.items is not None and n:
            self.edits.append((pos, n))

    def note_delete(self, pos, n):
        if self.items is not None and n:
            self.edits.append((pos, -n))

    def _run_want(self, run, lines):
        l0, l1 = lines
        to_hide = len(run) - len(run) // 2
        want = []
        for a, z, ln in run:
            if to_hide <= 0:
                break
            if l0 <= ln <= l1 or z <= a:
                continue
            want.append((a, z))
            to_hide -= 1
        return frozenset(want)

    def _desired(self, text, lines):
        ghosts, runs = [], []
        spans, markers = parse_markdown(text, ghosts, runs)
        items = set()
        for name, a, z in spans:
            if z > a:
                items.add((name, a, z))
        # Like Windows 11 Notepad, syntax stays hidden on every line, including
        # the one being edited ("Markdown syntax" view shows it).
        for a, z, _ in markers:
            if z > a:
                items.add(("marker", a, z))
                items.add(("hidden", a, z))
        for a, z, _ in ghosts:
            items.add(("ghost", a, z))
        self.markers, self.ghosts, self.runs = markers, ghosts, runs
        self.run_first = [run[0][2] for run in runs]
        self.run_hidden = [self._run_want(run, lines) for run in runs]
        for want in self.run_hidden:
            for a, z in want:
                items.add(("hidden", a, z))
        return items

    def _map_old(self, text_len):
        """Shift the previous items through the recorded edits.

        Returns (shifted_items, dirty) where dirty is a list of (a, z) ranges,
        in current offsets, that the edits touched."""
        dirty = []
        items = []
        for item in self.items:
            items.append(list(item))
        for pos, d in self.edits:
            gone = -d if d < 0 else 0
            for r in dirty:
                for k in (0, 1):
                    if r[k] > pos:
                        r[k] = max(pos, r[k] - gone) if d < 0 else r[k] + d
            dirty.append([pos, pos + max(d, 0)])
            for item in items:
                if item[0] is None:
                    continue
                a, z = item[1], item[2]
                if a <= pos + gone and z >= pos:
                    item[0] = None          # touches the edit: restyled below
                elif a > pos:
                    item[1], item[2] = a + d, z + d
        shifted = {(t, a, z) for t, a, z in items if t is not None}
        return shifted, dirty

    def apply(self, lines):
        b = self.buffer
        was_modified = b.get_modified()
        s, e = b.get_bounds()
        text = b.get_text(s, e, True)
        table = b.get_tag_table()
        it = b.get_iter_at_offset
        incremental = self.active and self.items is not None and len(self.edits) <= 200
        old_items = self.items if incremental else None
        if incremental:
            shifted, dirty = self._map_old(len(text))
        new = self._desired(text, lines)
        if not incremental:
            for t in self.all_tags:
                b.remove_tag(t, s, e)
            for name, a, z in new:
                b.apply_tag(table.lookup(name), it(a), it(z))
        else:
            # widen each touched range to whole lines (plus the newline before)
            n = len(text)
            ranges = []
            for a, z in dirty:
                a = max(0, min(a, n))
                z = max(0, min(z, n))
                ls = text.rfind("\n", 0, a)
                ls = max(0, ls)
                le = text.find("\n", z)
                le = n if le < 0 else le + 1
                ranges.append((ls, le))
            ranges.sort()
            merged = []
            for a, z in ranges:
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], z))
                else:
                    merged.append((a, z))
            for a, z in merged:
                for t in self.all_tags:
                    b.remove_tag(t, it(a), it(z))

            lo = merged[0][0] if merged else 0
            hi = merged[-1][1] if merged else 0

            def touches(a, z):
                if not merged or z <= lo or a >= hi:
                    return False
                return any(a < mz and z > ma for ma, mz in merged)

            to_remove = shifted - new
            to_add = {i for i in new if i not in shifted or touches(i[1], i[2])}
            removed = {}
            for name, a, z in to_remove:
                b.remove_tag(table.lookup(name), it(a), it(z))
                removed.setdefault(name, []).append((a, z))
            if removed:
                # removing one range may have cut a kept range with the same tag
                for item in new & shifted:
                    name, a, z = item
                    if any(a < rz and z > ra for ra, rz in removed.get(name, ())):
                        to_add.add(item)
            for name, a, z in to_add:
                b.apply_tag(table.lookup(name), it(a), it(z))
        self.items = new
        self.edits = []
        self.reveal = lines
        self.stale = False
        self.active = True
        if b.get_modified() != was_modified:
            b.set_modified(was_modified)

    def _runs_near(self, lo, hi):
        """Indexes of blank-line runs that touch lines lo..hi."""
        i = bisect.bisect_right(self.run_first, hi) - 1
        out = []
        while i >= 0 and self.runs[i][-1][2] >= lo:
            out.append(i)
            i -= 1
        return out

    def update_hidden(self, lines, force=False):
        """Paragraph breaks: show k // 2 of every k blank lines, never hiding
        the line the cursor is on.  Only runs next to the old and new cursor
        lines are touched, so moving the cursor doesn't re-lay-out the file."""
        if not self.active or self.stale or (lines == self.reveal and not force):
            return
        old, self.reveal = self.reveal, lines
        if force or old is None:
            affected = range(len(self.runs))
        else:
            affected = set(self._runs_near(*old)) | set(self._runs_near(*lines))
        b = self.buffer
        it = b.get_iter_at_offset
        for i in affected:
            want = self._run_want(self.runs[i], lines)
            have = self.run_hidden[i]
            for a, z in have - want:
                b.remove_tag(self.hidden, it(a), it(z))
                if self.items is not None:
                    self.items.discard(("hidden", a, z))
            for a, z in want - have:
                b.apply_tag(self.hidden, it(a), it(z))
                if self.items is not None:
                    self.items.add(("hidden", a, z))
            self.run_hidden[i] = want


# --------------------------------------------------------------------------
# Document (one tab)
# --------------------------------------------------------------------------

class Document(Gtk.ScrolledWindow):
    _next_id = 0

    def __init__(self, win, markdown):
        super().__init__()
        Document._next_id += 1
        self.uid = Document._next_id
        self.win = win
        self.path = None
        self.encoding = "utf-8"
        self.eol = win.app.settings["default_eol"]
        self.markdown = markdown
        self.syntax_view = False
        self.zoom = 100
        self.page = None
        self._format_source = None
        self._too_big = False

        self.buffer = Gtk.TextBuffer()
        self.buffer.set_enable_undo(True)
        self.view = Gtk.TextView(buffer=self.buffer)
        self.view.add_css_class("notepad-doc")
        self.view.add_css_class(f"doc-{self.uid}")
        self.view.set_left_margin(12)
        self.view.set_right_margin(12)
        self.view.set_top_margin(10)
        self.view.set_bottom_margin(40)
        self.view.set_pixels_below_lines(1)
        self.set_child(self.view)
        self.set_vexpand(True)
        self.set_hexpand(True)
        self.formatter = MarkdownFormatter(self.buffer)
        # created after the Markdown tags so they draw on top
        self.buffer.create_tag("search", background_rgba=rgba("rgba(255,200,0,0.35)"))
        self.buffer.create_tag("search-current", background_rgba=rgba("rgba(255,140,0,0.75)"))
        self.apply_wrap(win.app.settings["word_wrap"])
        self.apply_tabs()

        # extra entries in the right-click menu
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Search with Bing", "win.search-web")
        menu.append_section(None, section)
        fmt = Gio.Menu()
        fmt.append("Bold", "win.bold")
        fmt.append("Italic", "win.italic")
        fmt.append("Insert link…", "win.link")
        menu.append_section(None, fmt)
        self.view.set_extra_menu(menu)

        sc = Gtk.ShortcutController()
        sc.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>y"),
            Gtk.CallbackAction.new(lambda *a: (self.redo(), True)[1])))
        self.view.add_controller(sc)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.view.add_controller(keys)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self.on_scroll)
        self.view.add_controller(scroll)

        click = Gtk.GestureClick()
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self.on_click)
        self.view.add_controller(click)

        self.pending = None      # (offset, marks) style to apply to typed text
        self._adjusting = False
        self.buffer.connect("insert-text", self.on_insert_text)
        self.buffer.connect("delete-range", self.on_delete_range)
        self.buffer.connect("changed", self.on_changed)
        self.buffer.connect("modified-changed", self.on_modified_changed)
        self.buffer.connect("notify::cursor-position", self.on_cursor)
        self.buffer.connect("mark-set", self.on_mark_set)

    # -- state ------------------------------------------------------------
    @property
    def modified(self):
        return self.buffer.get_modified()

    def get_text(self):
        s, e = self.buffer.get_bounds()
        return self.buffer.get_text(s, e, True)

    def set_text(self, text):
        self.buffer.begin_irreversible_action()
        self.buffer.set_text(text)
        self.buffer.end_irreversible_action()
        self.buffer.place_cursor(self.buffer.get_start_iter())

    def is_blank(self):
        return self.path is None and not self.modified and self.buffer.get_char_count() == 0

    def title(self):
        if self.path:
            return os.path.basename(self.path)
        return derive_title(self.get_text()) or "Untitled"

    def suggested_name(self):
        if self.path:
            return os.path.basename(self.path)
        ext = ".md" if self.markdown and looks_like_markdown(self.get_text()) else ".txt"
        return safe_filename(derive_title(self.get_text()) or "Untitled") + ext

    # -- loading / saving -------------------------------------------------
    def load(self, path):
        with open(path, "rb") as fh:
            data = fh.read()
        text, enc, eol = decode_bytes(data)
        self.path = os.path.abspath(path)
        self.encoding = enc
        if eol:
            self.eol = eol
        self.markdown = path.lower().endswith((".md", ".markdown", ".mdown", ".mkd"))
        self.set_text(text)
        self.buffer.set_modified(False)
        self.schedule_format(0)

    def write(self, path):
        data = encode_text(self.get_text(), self.encoding, self.eol)
        write_file_atomic(path, data)
        self.path = os.path.abspath(path)
        self.buffer.set_modified(False)

    # -- formatting -------------------------------------------------------
    def formatting_wanted(self):
        return self.markdown and not self.syntax_view and not self._too_big

    def cursor_lines(self):
        """The line that must stay visible: only the cursor's own line.

        Lines inside a selection are not included, so extending a selection
        (Shift+Down) over hidden paragraph breaks doesn't reveal them."""
        b = self.buffer
        line = b.get_iter_at_mark(b.get_insert()).get_line()
        return (line, line)

    def schedule_format(self, delay=120):
        if self._format_source:
            GLib.source_remove(self._format_source)
        self._format_source = GLib.timeout_add(delay, self._format_timeout)

    def _format_timeout(self):
        self._format_source = None
        return self._do_format()

    def _do_format(self):
        if self._format_source:
            # called directly: the pending timer would only repeat this work
            GLib.source_remove(self._format_source)
            self._format_source = None
        big = self.buffer.get_char_count() > FORMAT_LIMIT
        if big and self.markdown and not self._too_big:
            self.win.toast("This file is too large for Markdown formatting.")
        self._too_big = big
        if self.formatting_wanted():
            self.formatter.apply(self.cursor_lines())
        elif self.formatter.active or self.formatter.markers:
            self.formatter.clear()
        return False

    def apply_tabs(self, later=True):
        """Draw a Tab as wide as `tab_width` spaces of the text's real font.

        A font or zoom change reaches the widget only when GTK next restyles
        it, so measure again shortly afterwards as well."""
        spaces = int(self.win.app.settings.get("tab_width", 8))
        self.view.set_tabs(tab_array(self.view.create_pango_layout(""), spaces))
        if later:
            GLib.timeout_add(80, lambda: (self.apply_tabs(later=False), False)[1])

    def apply_wrap(self, wrap):
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if wrap else Gtk.WrapMode.NONE)

    # -- edit helpers -----------------------------------------------------
    def undo(self):
        if self.buffer.get_can_undo():
            self.buffer.undo()

    def redo(self):
        if self.buffer.get_can_redo():
            self.buffer.redo()

    def selected_text(self):
        sel = self.buffer.get_selection_bounds()
        return self.buffer.get_text(sel[0], sel[1], True) if sel else ""

    def replace_range(self, start, end, text, sel_start=None, sel_end=None):
        b = self.buffer
        b.begin_user_action()
        b.delete(b.get_iter_at_offset(start), b.get_iter_at_offset(end))
        b.insert(b.get_iter_at_offset(start), text)
        b.end_user_action()
        if sel_start is not None:
            b.select_range(b.get_iter_at_offset(sel_start), b.get_iter_at_offset(sel_end))

    def toggle_wrap(self, mark):
        b = self.buffer
        sel = b.get_selection_bounds()
        if not sel:
            it = b.get_iter_at_mark(b.get_insert())
            ls = it.copy()
            ls.set_line_offset(0)
            le = it.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            line = b.get_text(ls, le, True)
            base = ls.get_offset()
            col = it.get_line_offset()
            span = find_wrap_span(line, mark, col)
            n = len(mark)
            if span and self.formatting_wanted() and col in (span[1] - n, span[1]):
                # at the end of styled text: stop the style for further typing
                self.pending = None
                b.place_cursor(b.get_iter_at_offset(base + span[1]))
                return
            if span:
                a, z = span
                inner = line[a + n:z - n]
                cur = it.get_offset() - n if it.get_offset() >= base + a + n else it.get_offset()
                cur = max(base + a, min(cur, base + a + len(inner)))
                self.replace_range(base + a, base + z, inner, cur, cur)
                return
            if it.inside_word() or it.ends_word():
                ws, we = it.copy(), it.copy()
                if not ws.starts_word():
                    ws.backward_word_start()
                if not we.ends_word():
                    we.forward_word_end()
                cur = it.get_offset()
                start, end, rep, _, _ = toggle_wrap_text(
                    self.get_text(), ws.get_offset(), we.get_offset(), mark)
                shift = (cur - ws.get_offset()) + (rep.find(self.get_text()[ws.get_offset():we.get_offset()])
                                                   if rep else 0)
                self.replace_range(start, end, rep)
                pos = max(start, min(start + shift, start + len(rep)))
                b.place_cursor(b.get_iter_at_offset(pos))
                return
            off = it.get_offset()
            if self.formatting_wanted():
                # like Windows: the style applies to what is typed next
                if self.pending and self.pending[0] == off and mark in self.pending[1]:
                    marks = self.pending[1].replace(mark, "", 1)
                    self.pending = (off, marks) if marks else None
                else:
                    prev = self.pending[1] if self.pending and self.pending[0] == off else ""
                    self.pending = (off, prev + mark)
                return
            self.replace_range(off, off, mark + mark, off + n, off + n)
            return
        full = self.get_text()
        start, end, rep, ns, ne = toggle_wrap_text(full, sel[0].get_offset(), sel[1].get_offset(), mark)
        self.replace_range(start, end, rep, ns, ne)

    def transform_lines(self, fn):
        b = self.buffer
        sel = b.get_selection_bounds()
        if sel:
            s, e = sel
        else:
            s = e = b.get_iter_at_mark(b.get_insert())
        l0, l1 = s.get_line(), e.get_line()
        if sel and e.get_line_offset() == 0 and l1 > l0:
            l1 -= 1
        _, a = b.get_iter_at_line(l0)
        _, z = b.get_iter_at_line(l1)
        if not z.ends_line():
            z.forward_to_line_end()
        ao, zo = a.get_offset(), z.get_offset()
        lines = b.get_text(a, z, True).split("\n")
        new = fn(lines)
        if new == lines:
            return
        text = "\n".join(new)
        if sel:
            self.replace_range(ao, zo, text, ao, ao + len(text))
        else:
            end = ao + len(new[0])
            self.replace_range(ao, zo, text, end, end)

    def insert_text(self, text):
        b = self.buffer
        b.begin_user_action()
        b.delete_selection(True, True)
        b.insert_at_cursor(text)
        b.end_user_action()
        self.view.scroll_mark_onscreen(b.get_insert())

    # -- signals ----------------------------------------------------------
    def on_changed(self, *_):
        self.formatter.stale = True
        if self.markdown:
            self.schedule_format()
        self.win.on_doc_changed(self)

    def on_modified_changed(self, *_):
        self.win.update_tab(self)

    def on_cursor(self, *_):
        if self.formatter.active:
            self.formatter.update_hidden(self.cursor_lines())
        self.win.update_status(self)

    def on_mark_set(self, buf, it, mark):
        if mark == buf.get_selection_bound() or mark == buf.get_insert():
            if mark == buf.get_insert():
                if self.pending and it.get_offset() != self.pending[0]:
                    self.pending = None
                self.skip_hidden_prefix(it)
            if self.formatter.active:
                self.formatter.update_hidden(self.cursor_lines())
            self.win.update_status(self)

    def skip_hidden_prefix(self, it):
        """Keep the cursor after a hidden line prefix ('# ', '> ', '\\'), so
        typing at the start of a heading stays in the heading."""
        if self._adjusting or not self.formatter.active:
            return
        # Compare the marks directly: during a select-all, get_has_selection()
        # is not updated yet when this runs, and moving the cursor here would
        # cancel the selection.
        if not it.equal(self.buffer.get_iter_at_mark(self.buffer.get_selection_bound())):
            return
        hidden = self.formatter.hidden
        if not it.starts_line() or not it.has_tag(hidden) or it.ends_line():
            return
        end = it.copy()
        end.forward_to_tag_toggle(hidden)
        if end.get_line() != it.get_line():
            return
        self._adjusting = True
        self.buffer.place_cursor(end)
        self._adjusting = False

    def on_delete_range(self, buf, start, end):
        self.formatter.note_delete(start.get_offset(), end.get_offset() - start.get_offset())

    def on_insert_text(self, buf, location, text, length):
        """Record the edit, and apply a pending style (Ctrl+B with nothing
        selected) to typed text."""
        self.formatter.note_insert(location.get_offset(), len(text))
        if not self.pending or self._adjusting or "\n" in text:
            return
        off, marks = self.pending
        if location.get_offset() != off:
            return
        self.pending = None
        GLib.idle_add(self._apply_pending, off, len(text), marks)

    def _apply_pending(self, off, n, marks):
        closing = marks[::-1]
        b = self.buffer
        if off + n > b.get_char_count():
            return False
        typed = b.get_text(b.get_iter_at_offset(off), b.get_iter_at_offset(off + n), True)
        self._adjusting = True
        self.replace_range(off, off + n, marks + typed + closing)
        cur = off + len(marks) + n
        b.place_cursor(b.get_iter_at_offset(cur))
        self._adjusting = False
        self.schedule_format(0)
        return False

    def _hidden_at(self, offset):
        b = self.buffer
        if offset < 0 or offset >= b.get_char_count():
            return False
        it = b.get_iter_at_offset(offset)
        return it.has_tag(self.formatter.hidden) and it.get_char() != "\n"

    def delete_visible(self, backward):
        """Backspace/Delete that skip hidden syntax and delete the visible
        character next to it.

        Returns (handled, offset): when not handled, `offset` is where the
        cursor effectively is once hidden syntax is skipped."""
        b = self.buffer
        text = self.get_text()
        o = b.get_iter_at_mark(b.get_insert()).get_offset()
        line_start = text.rfind("\n", 0, o) + 1
        if backward:
            p = o
            if o == line_start:
                # cursor parked before a hidden line prefix: treat it as after it
                while self._hidden_at(p):
                    p += 1
            q = p
            while q > line_start and self._hidden_at(q - 1):
                q -= 1
            if q == p:
                return False, o
            prefix = RE_LINE_PREFIX.match(text[q:p]) if q == line_start else None
            if prefix and prefix.end() > 0:
                # Backspace at the start of a heading/quote turns it into normal text
                self.replace_range(q, q + prefix.end(), "", q, q)
                self.schedule_format(0)
                return True, o
            if q == line_start:
                return False, line_start
            s = b.get_iter_at_offset(q)
            s.backward_cursor_position()
            a, z = s.get_offset(), q
        else:
            q = o
            while self._hidden_at(q):
                q += 1
            if q == o or q >= len(text) or text[q] == "\n":
                return False, q
            e = b.get_iter_at_offset(q)
            e.forward_cursor_position()
            a, z = q, e.get_offset()
        b.begin_user_action()
        b.delete(b.get_iter_at_offset(a), b.get_iter_at_offset(z))
        rest = self.get_text()
        pair = empty_pair_at(rest, a)
        if pair:
            b.delete(b.get_iter_at_offset(pair[0]), b.get_iter_at_offset(pair[1]))
            a = pair[0]
        b.end_user_action()
        b.place_cursor(b.get_iter_at_offset(a))
        self.schedule_format(0)
        return True, a

    def on_key(self, ctrl, keyval, keycode, state):
        """Windows 11 Notepad paragraph editing in Markdown formatted view.

        Enter writes a paragraph break (blank line) like Windows does, so files
        round-trip with Windows Notepad; Shift+Enter writes a plain line break.
        Backspace/Delete at a paragraph edge remove the whole break at once.
        The Tab key types spaces (Settings → Tab key), in every view."""
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab) and not (
                state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                         | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.SHIFT_MASK)):
            spaces = int(self.win.app.settings.get("tab_key", 4))
            if spaces > 0:
                self.insert_text(" " * spaces)
                return True
            return False
        if not self.formatting_wanted():
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                    | Gdk.ModifierType.SUPER_MASK):
            return False
        b = self.buffer
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self.insert_text("\n")
                return True
            return self.smart_enter()
        if keyval not in (Gdk.KEY_BackSpace, Gdk.KEY_Delete, Gdk.KEY_KP_Delete) or b.get_has_selection():
            return False
        it = b.get_iter_at_mark(b.get_insert())
        if it.has_tag(b.get_tag_table().lookup("codeblock")):
            return False
        backward = keyval == Gdk.KEY_BackSpace
        handled, o = self.delete_visible(backward)
        if handled:
            return True
        a, z = (o - 2, o) if backward else (o, o + 2)
        total = b.get_char_count()
        is_break = (0 <= a and z <= total and
                    b.get_text(b.get_iter_at_offset(a), b.get_iter_at_offset(z), True) == "\n\n")
        if not is_break:
            # delete one visible character (a whole emoji/accented letter)
            edge = b.get_iter_at_offset(o)
            if backward:
                if not edge.backward_cursor_position():
                    return True
                a, z = edge.get_offset(), o
            else:
                if not edge.forward_cursor_position():
                    return True
                a, z = o, edge.get_offset()
        b.begin_user_action()
        b.delete(b.get_iter_at_offset(a), b.get_iter_at_offset(z))
        if not is_break:
            # deleting the last letter of bold/italic text: drop the empty markers too
            pair = empty_pair_at(self.get_text(), a)
            if pair:
                b.delete(b.get_iter_at_offset(pair[0]), b.get_iter_at_offset(pair[1]))
                a = pair[0]
        b.end_user_action()
        b.place_cursor(b.get_iter_at_offset(a))
        self.view.scroll_mark_onscreen(b.get_insert())
        self.schedule_format(0)
        return True

    def smart_enter(self):
        b = self.buffer
        if b.get_has_selection():
            b.begin_user_action()
            b.delete_selection(True, True)
            b.end_user_action()
        it = b.get_iter_at_mark(b.get_insert())
        if it.has_tag(b.get_tag_table().lookup("codeblock")):
            return False
        ls = it.copy()
        ls.set_line_offset(0)
        le = it.copy()
        if not le.ends_line():
            le.forward_to_line_end()
        line = b.get_text(ls, le, True)
        if RE_FENCE.match(line):
            return False
        mb, mn = RE_BULLET.match(line), RE_NUMBER.match(line)
        m = mb or mn
        if m:
            if not line[m.end():].strip() and it.get_line_offset() >= m.end():
                # Enter on an empty list item ends the list
                b.begin_user_action()
                b.delete(ls, le)
                b.end_user_action()
                return True
            if mb:
                box = "[ ] " if mb.group(4) else ""
                prefix = f"{mb.group(1)}{mb.group(2)} {box}"
            else:
                num = re.match(r"\d+", mn.group(2)).group(0)
                prefix = f"{mn.group(1)}{int(num) + 1}{mn.group(2)[-1]} "
            self.insert_text("\n" + prefix)
            return True
        # keep bold/italic/strike text styled on both sides of the break
        ins, closers, openers = split_marks_at(line, it.get_line_offset())
        pos = ls.get_offset() + ins
        text = closers + "\n\n" + openers
        self.replace_range(pos, pos, text)
        b.place_cursor(b.get_iter_at_offset(pos + len(text)))
        self.view.scroll_mark_onscreen(b.get_insert())
        self.schedule_format(0)
        return True

    def on_scroll(self, ctrl, dx, dy):
        state = ctrl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.win.zoom_doc(self, -ZOOM_STEP if dy > 0 else ZOOM_STEP)
            return True
        return False

    def on_click(self, gesture, n, x, y):
        state = gesture.get_current_event_state()
        if not (state & Gdk.ModifierType.CONTROL_MASK) or self.buffer.get_has_selection():
            return
        bx, by = self.view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.view.get_iter_at_location(bx, by)
        if not ok:
            return
        ls = it.copy()
        ls.set_line_offset(0)
        le = it.copy()
        if not le.ends_line():
            le.forward_to_line_end()
        url = find_link_at(self.buffer.get_text(ls, le, True), it.get_line_offset())
        if url:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.win.open_uri(url)


# --------------------------------------------------------------------------
# Find / replace bar
# --------------------------------------------------------------------------

class FindBar(Gtk.Revealer):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.offsets = []
        self._refresh_source = None
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.set_halign(Gtk.Align.END)
        self.set_valign(Gtk.Align.START)
        self.set_margin_top(6)
        self.set_margin_end(18)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.add_css_class("findbar")
        for m in ("top", "bottom", "start", "end"):
            getattr(card, f"set_margin_{m}")(0)
        self.set_child(card)

        row1 = Gtk.Box(spacing=4)
        self.expand_btn = Gtk.ToggleButton(icon_name="pan-end-symbolic", tooltip_text="Toggle replace")
        self.expand_btn.add_css_class("flat")
        self.expand_btn.connect("toggled", self.on_expand)
        self.entry = Gtk.SearchEntry(placeholder_text="Find", width_chars=24)
        self.entry.connect("search-changed", lambda *_: self.refresh())
        self.entry.connect("activate", lambda *_: self.find(True))
        self.entry.connect("stop-search", lambda *_: self.close())
        self.entry.connect("next-match", lambda *_: self.find(True))
        self.entry.connect("previous-match", lambda *_: self.find(False))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.entry.add_controller(keys)
        self.count = Gtk.Label(width_chars=9, xalign=1)
        self.count.add_css_class("dim-label")
        self.count.add_css_class("caption")
        prev_btn = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Find previous (Shift+F3)")
        prev_btn.connect("clicked", lambda *_: self.find(False))
        next_btn = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Find next (F3)")
        next_btn.connect("clicked", lambda *_: self.find(True))
        self.case_btn = Gtk.ToggleButton(label="Aa", tooltip_text="Match case")
        self.case_btn.connect("toggled", lambda *_: self.refresh())
        self.wrap_btn = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic",
                                         tooltip_text="Wrap around", active=True)
        close_btn = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Close (Esc)")
        close_btn.connect("clicked", lambda *_: self.close())
        for w in (prev_btn, next_btn, self.case_btn, self.wrap_btn, close_btn):
            w.add_css_class("flat")
            w.set_focus_on_click(False)
        for w in (self.expand_btn, self.entry, self.count, prev_btn, next_btn,
                  self.case_btn, self.wrap_btn, close_btn):
            row1.append(w)
        card.append(row1)

        self.row2 = Gtk.Box(spacing=4, visible=False)
        spacer = Gtk.Box()
        spacer.set_size_request(34, -1)
        self.replace_entry = Gtk.Entry(placeholder_text="Replace", width_chars=24)
        self.replace_entry.connect("activate", lambda *_: self.replace_one())
        rk = Gtk.EventControllerKey()
        rk.connect("key-pressed", self.on_key)
        self.replace_entry.add_controller(rk)
        rep = Gtk.Button(label="Replace")
        rep.connect("clicked", lambda *_: self.replace_one())
        rep_all = Gtk.Button(label="Replace all")
        rep_all.connect("clicked", lambda *_: self.replace_all())
        for w in (spacer, self.replace_entry, rep, rep_all):
            self.row2.append(w)
        card.append(self.row2)

    # -- helpers ----------------------------------------------------------
    @property
    def doc(self):
        return self.win.current_doc()

    def flags(self):
        f = Gtk.TextSearchFlags.TEXT_ONLY
        if not self.case_btn.get_active():
            f |= Gtk.TextSearchFlags.CASE_INSENSITIVE
        return f

    def on_expand(self, btn):
        self.row2.set_visible(btn.get_active())
        btn.set_icon_name("pan-down-symbolic" if btn.get_active() else "pan-end-symbolic")

    def on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.SHIFT_MASK:
            self.find(False)
            return True
        return False

    def open(self, replace=False):
        doc = self.doc
        if doc:
            sel = doc.selected_text()
            if sel and "\n" not in sel:
                self.entry.set_text(sel)
        self.expand_btn.set_active(replace)
        self.set_reveal_child(True)
        self.entry.grab_focus()
        self.entry.select_region(0, -1)
        self.refresh()

    def close(self):
        self.set_reveal_child(False)
        for d in self.win.docs():
            self.clear_tags(d)
        if self.doc:
            self.doc.view.grab_focus()

    def clear_tags(self, doc):
        b = doc.buffer
        s, e = b.get_bounds()
        b.remove_tag_by_name("search", s, e)
        b.remove_tag_by_name("search-current", s, e)

    def schedule_refresh(self):
        if not self.get_reveal_child():
            return
        if self._refresh_source:
            GLib.source_remove(self._refresh_source)
        self._refresh_source = GLib.timeout_add(250, self._refresh_idle)

    def _refresh_idle(self):
        self._refresh_source = None
        self.refresh()
        return False

    def refresh(self):
        doc = self.doc
        self.offsets = []
        if not doc:
            return
        for d in self.win.docs():
            self.clear_tags(d)
        q = self.entry.get_text()
        if not q or not self.get_reveal_child():
            self.count.set_text("")
            return
        b = doc.buffer
        tag = b.get_tag_table().lookup("search")
        it = b.get_start_iter()
        flags = self.flags()
        while len(self.offsets) < 10000:
            r = it.forward_search(q, flags, None)
            if not r:
                break
            ms, me = r
            b.apply_tag(tag, ms, me)
            self.offsets.append(ms.get_offset())
            if me.equal(ms):
                break
            it = me
        self.update_count()

    def update_count(self, current=None):
        if not self.entry.get_text():
            self.count.set_text("")
        elif not self.offsets:
            self.count.set_text("No results")
        elif current is None:
            self.count.set_text(f"{len(self.offsets)} found")
        else:
            i = bisect.bisect_left(self.offsets, current)
            self.count.set_text(f"{i + 1} of {len(self.offsets)}")

    def find(self, forward=True):
        doc = self.doc
        q = self.entry.get_text()
        if not doc or not q:
            if not self.get_reveal_child():
                self.open()
            return False
        b = doc.buffer
        flags = self.flags()
        sel = b.get_selection_bounds()
        cur = b.get_iter_at_mark(b.get_insert())
        if forward:
            start = sel[1] if sel else cur
            r = start.forward_search(q, flags, None)
            if not r and self.wrap_btn.get_active():
                r = b.get_start_iter().forward_search(q, flags, None)
        else:
            start = sel[0] if sel else cur
            r = start.backward_search(q, flags, None)
            if not r and self.wrap_btn.get_active():
                r = b.get_end_iter().backward_search(q, flags, None)
        s, e = b.get_bounds()
        b.remove_tag_by_name("search-current", s, e)
        if not r:
            self.count.set_text("No results")
            self.win.toast(f'Cannot find "{q}"')
            return False
        ms, me = r
        b.select_range(ms, me)
        b.apply_tag_by_name("search-current", ms, me)
        doc.view.scroll_to_mark(b.get_insert(), 0.15, False, 0, 0)
        self.update_count(ms.get_offset())
        return True

    def _matches(self, text, q):
        if self.case_btn.get_active():
            return text == q
        return text.casefold() == q.casefold()

    def replace_one(self):
        doc = self.doc
        q = self.entry.get_text()
        if not doc or not q:
            return
        if self._matches(doc.selected_text(), q):
            doc.insert_text(self.replace_entry.get_text())
        self.find(True)
        self.refresh()

    def replace_all(self):
        doc = self.doc
        q = self.entry.get_text()
        if not doc or not q:
            return
        b = doc.buffer
        flags = self.flags()
        matches = []
        it = b.get_start_iter()
        while True:
            r = it.forward_search(q, flags, None)
            if not r or r[1].equal(r[0]):
                break
            matches.append((r[0].get_offset(), r[1].get_offset()))
            it = r[1]
        rep = self.replace_entry.get_text()
        b.begin_user_action()
        for a, z in reversed(matches):
            b.delete(b.get_iter_at_offset(a), b.get_iter_at_offset(z))
            b.insert(b.get_iter_at_offset(a), rep)
        b.end_user_action()
        self.refresh()
        self.win.toast(f"Replaced {len(matches)} occurrence{'s' if len(matches) != 1 else ''}")


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class NotepadWindow(Adw.ApplicationWindow):
    def __init__(self, app, size=None, maximized=False):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.closing_ok = False
        self.closed = False
        self._block_status = False
        w, h = size or (960, 680)
        self.set_default_size(w, h)
        if maximized:
            self.maximize()

        self.tab_view = Adw.TabView()
        self.tab_view.connect("close-page", self.on_close_page)
        self.tab_view.connect("notify::selected-page", self.on_selected_page)
        self.tab_view.connect("page-attached", self.on_page_attached)
        self.tab_view.connect("notify::n-pages", self.on_n_pages)
        self.tab_view.connect("create-window", self.on_create_window)

        tab_bar = Adw.TabBar(view=self.tab_view, autohide=False, hexpand=True)
        new_btn = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text="New tab (Ctrl+N)")
        new_btn.set_action_name("win.new-tab")
        new_btn.add_css_class("flat")
        tab_bar.set_end_action_widget(new_btn)

        header = Adw.HeaderBar()
        header.set_title_widget(tab_bar)

        # menu + formatting toolbar row
        row = Gtk.Box(spacing=2)
        row.add_css_class("toolbar")
        row.add_css_class("menu-row")
        menubar = Gtk.PopoverMenuBar.new_from_model(app.menubar_model)
        row.append(menubar)

        self.fmt_box = Gtk.Box(spacing=2)
        self.fmt_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL,
                                          margin_start=6, margin_end=6))
        heading = Gtk.MenuButton(label="Text", tooltip_text="Text style")
        heading.set_menu_model(app.heading_model)
        heading.add_css_class("flat")
        self.fmt_box.append(heading)

        def tbtn(icon, action, tip):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action)
            b.add_css_class("flat")
            b.set_focus_on_click(False)
            self.fmt_box.append(b)

        tbtn("view-list-bullet-symbolic", "win.bullets", "Bulleted list")
        tbtn("view-list-ordered-symbolic", "win.numbers", "Numbered list")
        tbtn("format-text-bold-symbolic", "win.bold", "Bold (Ctrl+B)")
        tbtn("format-text-italic-symbolic", "win.italic", "Italic (Ctrl+I)")
        tbtn("format-text-strikethrough-symbolic", "win.strike", "Strikethrough")
        tbtn("insert-link-symbolic", "win.link", "Link (Ctrl+K)")
        tbtn("edit-clear-all-symbolic", "win.clear-formatting", "Clear formatting")
        row.append(self.fmt_box)

        spacer = Gtk.Box(hexpand=True)
        row.append(spacer)
        self.syntax_btn = Gtk.ToggleButton(label="Markdown syntax",
                                           tooltip_text="Show Markdown syntax instead of formatted text")
        self.syntax_btn.set_action_name("win.syntax-view")
        self.syntax_btn.add_css_class("flat")
        self.syntax_btn.set_focus_on_click(False)
        row.append(self.syntax_btn)
        gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings (Ctrl+,)",
                          action_name="win.settings")
        gear.add_css_class("flat")
        row.append(gear)

        # status bar
        self.statusbar = Gtk.Box(spacing=0)
        self.statusbar.add_css_class("statusbar")
        self.pos_label = Gtk.Label(xalign=0)
        self.chars_label = Gtk.Label(xalign=0)
        self.zoom_label = Gtk.Label()
        self.eol_btn = Gtk.MenuButton(menu_model=app.eol_model)
        self.enc_btn = Gtk.MenuButton(menu_model=app.encoding_model)
        for b in (self.eol_btn, self.enc_btn):
            b.add_css_class("flat")
            b.set_direction(Gtk.ArrowType.UP)
            b.set_always_show_arrow(False)

        def sep():
            return Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=10, margin_end=10)

        self.statusbar.append(self.pos_label)
        self.statusbar.append(sep())
        self.statusbar.append(self.chars_label)
        self.statusbar.append(Gtk.Box(hexpand=True))
        self.statusbar.append(self.zoom_label)
        self.statusbar.append(sep())
        self.statusbar.append(self.eol_btn)
        self.statusbar.append(sep())
        self.statusbar.append(self.enc_btn)

        self.find_bar = FindBar(self)
        overlay = Gtk.Overlay()
        overlay.set_child(self.tab_view)
        overlay.add_overlay(self.find_bar)

        tv = Adw.ToolbarView()
        tv.add_top_bar(header)
        tv.add_top_bar(row)
        tv.set_content(overlay)
        tv.add_bottom_bar(self.statusbar)
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(tv)
        self.set_content(self.toasts)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.tab_view.add_controller(drop)

        self.setup_actions()
        self.connect("close-request", self.on_close_request)
        self.statusbar.set_visible(app.settings["status_bar"])

    # -- actions ----------------------------------------------------------
    def setup_actions(self):
        simple = {
            "new-tab": lambda *_: self.new_tab(focus=True),
            "open": lambda *_: self.open_dialog(),
            "save": lambda *_: self.save_doc(self.current_doc()),
            "save-as": lambda *_: self.save_doc(self.current_doc(), save_as=True),
            "save-all": lambda *_: self.save_all(),
            "page-setup": lambda *_: self.app.page_setup_dialog(self),
            "print": lambda *_: self.print_doc(),
            "close-tab": lambda *_: self.close_current_tab(),
            "close-window": lambda *_: self.close(),
            "undo": lambda *_: self.with_doc(lambda d: d.undo()),
            "redo": lambda *_: self.with_doc(lambda d: d.redo()),
            "cut": lambda *_: self.with_doc(lambda d: d.view.emit("cut-clipboard")),
            "copy": lambda *_: self.with_doc(lambda d: d.view.emit("copy-clipboard")),
            "paste": lambda *_: self.with_doc(lambda d: d.view.emit("paste-clipboard")),
            "delete": lambda *_: self.with_doc(self.delete_selection),
            "select-all": lambda *_: self.with_doc(lambda d: d.view.emit("select-all", True)),
            "search-web": lambda *_: self.search_web(),
            "find": lambda *_: self.find_bar.open(False),
            "replace": lambda *_: self.find_bar.open(True),
            "find-next": lambda *_: self.find_bar.find(True),
            "find-prev": lambda *_: self.find_bar.find(False),
            "goto": lambda *_: self.goto_dialog(),
            "time-date": lambda *_: self.with_doc(
                lambda d: d.insert_text(datetime.datetime.now().strftime("%-I:%M %p %-m/%-d/%Y"))),
            "font": lambda *_: self.app.show_settings(self),
            "settings": lambda *_: self.app.show_settings(self),
            "zoom-in": lambda *_: self.zoom_doc(self.current_doc(), ZOOM_STEP),
            "zoom-out": lambda *_: self.zoom_doc(self.current_doc(), -ZOOM_STEP),
            "zoom-reset": lambda *_: self.zoom_doc(self.current_doc(), None),
            "bold": lambda *_: self.fmt(lambda d: d.toggle_wrap("**")),
            "italic": lambda *_: self.fmt(lambda d: d.toggle_wrap("*")),
            "strike": lambda *_: self.fmt(lambda d: d.toggle_wrap("~~")),
            "link": lambda *_: self.fmt(self.link_dialog, refocus=False),
            "bullets": lambda *_: self.fmt(lambda d: d.transform_lines(toggle_bullet_lines)),
            "numbers": lambda *_: self.fmt(lambda d: d.transform_lines(toggle_number_lines)),
            "clear-formatting": lambda *_: self.fmt(lambda d: d.transform_lines(clear_formatting_lines)),
        }
        for name, cb in simple.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

        a = Gio.SimpleAction.new("heading", GLib.VariantType.new("i"))
        a.connect("activate", lambda act, p: self.fmt(
            lambda d: d.transform_lines(lambda ls: set_heading_lines(ls, p.get_int32()))))
        self.add_action(a)

        self.md_action = Gio.SimpleAction.new_stateful("markdown", None, GLib.Variant.new_boolean(True))
        self.md_action.connect("change-state", self.on_markdown_toggle)
        self.add_action(self.md_action)
        self.syntax_action = Gio.SimpleAction.new_stateful("syntax-view", None, GLib.Variant.new_boolean(False))
        self.syntax_action.connect("change-state", self.on_syntax_toggle)
        self.add_action(self.syntax_action)
        self.eol_action = Gio.SimpleAction.new_stateful("eol", GLib.VariantType.new("s"), GLib.Variant.new_string("lf"))
        self.eol_action.connect("change-state", self.on_eol_change)
        self.add_action(self.eol_action)
        self.enc_action = Gio.SimpleAction.new_stateful("encoding", GLib.VariantType.new("s"),
                                                        GLib.Variant.new_string("utf-8"))
        self.enc_action.connect("change-state", self.on_encoding_change)
        self.add_action(self.enc_action)

    def with_doc(self, fn):
        d = self.current_doc()
        if d:
            fn(d)

    def fmt(self, fn, refocus=True):
        d = self.current_doc()
        if not d:
            return
        if not d.markdown:
            d.markdown = True
            self.sync_doc_ui(d)
        fn(d)
        if refocus:
            d.view.grab_focus()
        d.schedule_format(0)

    def delete_selection(self, d):
        b = d.buffer
        if b.get_has_selection():
            b.delete_selection(True, True)
        else:
            it = b.get_iter_at_mark(b.get_insert())
            end = it.copy()
            if end.forward_cursor_position():
                b.begin_user_action()
                b.delete(it, end)
                b.end_user_action()

    # -- docs / tabs ------------------------------------------------------
    def docs(self):
        return [self.tab_view.get_nth_page(i).get_child() for i in range(self.tab_view.get_n_pages())]

    def current_doc(self):
        p = self.tab_view.get_selected_page()
        return p.get_child() if p else None

    def new_tab(self, focus=True, markdown=None):
        if markdown is None:
            markdown = self.app.settings["markdown_new"]
        doc = Document(self, markdown)
        page = self.tab_view.append(doc)
        doc.page = page
        self.app.update_css()
        self.update_tab(doc)
        if focus:
            self.tab_view.set_selected_page(page)
            doc.view.grab_focus()
        return doc

    def find_doc(self, path):
        real = os.path.realpath(path)
        for d in self.docs():
            if d.path and os.path.realpath(d.path) == real:
                return d
        return None

    def open_path(self, path, focus=True):
        path = os.path.abspath(path)
        for w in self.app.notepad_windows():
            d = w.find_doc(path)
            if d:
                w.tab_view.set_selected_page(d.page)
                w.present()
                self.app.add_recent(path)
                return d
        if os.path.isdir(path):
            self.toast(f"{os.path.basename(path)} is a folder")
            return None
        cur = self.current_doc()
        reuse = cur if cur and cur.is_blank() else None
        doc = reuse or self.new_tab(focus=False)
        try:
            doc.load(path)
        except OSError as e:
            if not reuse:
                self.remove_doc(doc)
            self.error_dialog(f"Could not open {os.path.basename(path)}", e.strerror or str(e))
            return None
        self.app.add_recent(path)
        self.update_tab(doc)
        if focus:
            self.tab_view.set_selected_page(doc.page)
            self.sync_doc_ui(doc)
            doc.view.grab_focus()
        self.app.schedule_session_save()
        return doc

    def remove_doc(self, doc):
        doc.buffer.set_modified(False)
        self._force_close = doc
        self.tab_view.close_page(doc.page)

    def close_current_tab(self):
        p = self.tab_view.get_selected_page()
        if p:
            self.tab_view.close_page(p)

    def on_close_page(self, view, page):
        doc = page.get_child()
        if getattr(self, "_force_close", None) is doc or not doc.modified or self.closing_ok:
            self._force_close = None
            view.close_page_finish(page, True)
            self.app.schedule_session_save()
            return True

        def done(ok):
            view.close_page_finish(page, ok)
            if ok:
                self.app.schedule_session_save()

        self.confirm_save(doc, done)
        return True

    def on_n_pages(self, *_):
        if self.tab_view.get_n_pages() == 0 and not self.closed:
            self.closing_ok = True
            self.close()

    def on_page_attached(self, view, page, pos):
        doc = page.get_child()
        doc.win = self
        doc.page = page
        self.update_tab(doc)

    def on_create_window(self, view):
        w = NotepadWindow(self.app)
        w.present()
        return w.tab_view

    def on_selected_page(self, *_):
        d = self.current_doc()
        if d:
            self.sync_doc_ui(d)
            if self.find_bar.get_reveal_child():
                self.find_bar.refresh()

    def sync_doc_ui(self, d):
        self._block_status = True
        self.md_action.set_state(GLib.Variant.new_boolean(d.markdown))
        self.syntax_action.set_state(GLib.Variant.new_boolean(d.syntax_view))
        self.eol_action.set_state(GLib.Variant.new_string(EOL_KEYS[d.eol]))
        self.enc_action.set_state(GLib.Variant.new_string(d.encoding))
        self._block_status = False
        self.fmt_box.set_visible(d.markdown)
        self.syntax_btn.set_visible(d.markdown)
        self.update_title(d)
        self.update_status(d)

    def update_tab(self, doc):
        if not doc.page:
            return
        title = doc.title()
        doc.page.set_title(("• " if doc.modified else "") + title)
        doc.page.set_tooltip(doc.path or title)
        if doc is self.current_doc():
            self.update_title(doc)

    def update_title(self, d):
        self.set_title(f"{d.title()} - {APP_NAME}")

    def update_status(self, d):
        if d is not self.current_doc():
            return
        b = d.buffer
        it = b.get_iter_at_mark(b.get_insert())
        col = it.get_line_offset()
        if d.formatter.active and col:
            # count only visible characters, like Windows Notepad
            ls = it.copy()
            ls.set_line_offset(0)
            before = b.get_text(ls, it, True)
            visible = b.get_text(ls, it, False)
            col = len(visible) if len(visible) <= len(before) else col
        self.pos_label.set_text(f"Ln {it.get_line() + 1}, Col {col + 1}")
        total = b.get_char_count()
        sel = b.get_selection_bounds()
        if sel:
            n = sel[1].get_offset() - sel[0].get_offset()
            self.chars_label.set_text(f"{n:,} of {total:,} characters")
        else:
            self.chars_label.set_text(f"{total:,} character{'s' if total != 1 else ''}")
        self.zoom_label.set_text(f"{d.zoom}%")
        self.eol_btn.set_label(EOL_LABELS[d.eol])
        self.enc_btn.set_label(ENCODING_LABELS.get(d.encoding, d.encoding))

    def on_doc_changed(self, doc):
        if doc.path is None:
            self.update_tab(doc)
        self.update_status(doc)
        if doc is self.current_doc():
            self.find_bar.schedule_refresh()
        self.app.schedule_session_save()

    # -- view toggles -----------------------------------------------------
    def on_markdown_toggle(self, action, value):
        action.set_state(value)
        d = self.current_doc()
        if d and not self._block_status:
            d.markdown = value.get_boolean()
            self.sync_doc_ui(d)
            d.schedule_format(0)

    def on_syntax_toggle(self, action, value):
        action.set_state(value)
        d = self.current_doc()
        if d and not self._block_status:
            d.syntax_view = value.get_boolean()
            d.schedule_format(0)

    def on_eol_change(self, action, value):
        action.set_state(value)
        d = self.current_doc()
        if d and not self._block_status:
            new = EOL_FROM_KEY[value.get_string()]
            if new != d.eol:
                d.eol = new
                d.buffer.set_modified(True)
            self.update_status(d)

    def on_encoding_change(self, action, value):
        action.set_state(value)
        d = self.current_doc()
        if d and not self._block_status:
            new = value.get_string()
            if new != d.encoding:
                d.encoding = new
                d.buffer.set_modified(True)
            self.update_status(d)

    def zoom_doc(self, d, delta):
        if not d:
            return
        d.zoom = 100 if delta is None else max(ZOOM_MIN, min(ZOOM_MAX, d.zoom + delta))
        self.app.update_css()
        self.update_status(d)

    # -- file operations --------------------------------------------------
    def file_filters(self):
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f_txt = Gtk.FileFilter(name="Text documents (*.txt)")
        f_txt.add_pattern("*.txt")
        f_md = Gtk.FileFilter(name="Markdown (*.md)")
        for p in ("*.md", "*.markdown"):
            f_md.add_pattern(p)
        f_all = Gtk.FileFilter(name="All files")
        f_all.add_pattern("*")
        for f in (f_txt, f_md, f_all):
            filters.append(f)
        return filters, f_txt, f_md, f_all

    def initial_folder(self):
        d = self.current_doc()
        if d and d.path:
            return os.path.dirname(d.path)
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        return docs if docs and os.path.isdir(docs) else os.path.expanduser("~")

    def open_dialog(self):
        dlg = Gtk.FileDialog(title="Open")
        filters, _, _, f_all = self.file_filters()
        dlg.set_filters(filters)
        dlg.set_default_filter(f_all)
        dlg.set_initial_folder(Gio.File.new_for_path(self.initial_folder()))

        def done(dialog, res):
            try:
                files = dialog.open_multiple_finish(res)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                path = files.get_item(i).get_path()
                if path:
                    self.open_path(path)

        dlg.open_multiple(self, None, done)

    def save_doc(self, doc, save_as=False, callback=None):
        callback = callback or (lambda ok: None)
        if not doc:
            return callback(False)
        if doc.path and not save_as:
            return callback(self.write_doc(doc, doc.path))
        dlg = Gtk.FileDialog(title="Save as")
        filters, f_txt, f_md, f_all = self.file_filters()
        dlg.set_filters(filters)
        name = doc.suggested_name()
        dlg.set_default_filter(f_md if name.endswith(".md") else f_txt if name.endswith(".txt") else f_all)
        folder = os.path.dirname(doc.path) if doc.path else self.initial_folder()
        dlg.set_initial_folder(Gio.File.new_for_path(folder))
        dlg.set_initial_name(name)

        def done(dialog, res):
            try:
                f = dialog.save_finish(res)
            except GLib.Error:
                return callback(False)
            path = f.get_path() if f else None
            if not path:
                return callback(False)
            callback(self.write_doc(doc, path))

        dlg.save(self, None, done)

    def write_doc(self, doc, path):
        was_md = doc.path is None and doc.markdown
        try:
            doc.write(path)
        except UnicodeEncodeError:
            self.error_dialog("Some characters can't be saved",
                              f"This document contains characters that {ENCODING_LABELS.get(doc.encoding)} "
                              "can't store. Change the encoding (bottom right) to UTF-8 and save again.")
            return False
        except OSError as e:
            self.error_dialog(f"Could not save {os.path.basename(path)}", e.strerror or str(e))
            return False
        if path.lower().endswith((".md", ".markdown")):
            doc.markdown = True
        elif not was_md and path.lower().endswith(".txt"):
            pass
        self.app.add_recent(path)
        self.update_tab(doc)
        if doc is self.current_doc():
            self.sync_doc_ui(doc)
        self.app.schedule_session_save()
        return True

    def save_all(self):
        pending = [d for d in self.docs() if d.modified]

        def step(ok=True):
            if not pending:
                return
            d = pending.pop(0)
            self.tab_view.set_selected_page(d.page)
            self.save_doc(d, callback=step)

        step()

    def confirm_save(self, doc, done):
        self.tab_view.set_selected_page(doc.page)
        dlg = Adw.AlertDialog.new(f"Do you want to save changes to {doc.title()}?", None)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("discard", "Don't save")
        dlg.add_response("save", "Save")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("save")
        dlg.set_close_response("cancel")

        def on_response(d, response):
            if response == "save":
                self.save_doc(doc, callback=done)
            else:
                done(response == "discard")

        dlg.connect("response", on_response)
        dlg.present(self)

    def confirm_docs(self, docs, done):
        docs = list(docs)

        def step(ok=True):
            if not ok:
                return done(False)
            if not docs:
                return done(True)
            self.confirm_save(docs.pop(0), step)

        step()

    def on_close_request(self, *_):
        if self.closing_ok:
            self.closed = True
            self.app.after_window_closed(self)
            return False
        others = [w for w in self.app.notepad_windows() if w is not self]
        if not others and self.app.settings["startup"] == "continue":
            self.app.save_session(final=True)
            self.closing_ok = True
            self.closed = True
            return False
        dirty = [d for d in self.docs() if d.modified]
        if not dirty:
            self.closed = True
            self.app.after_window_closed(self)
            return False

        def done(ok):
            if ok:
                self.closing_ok = True
                self.close()

        self.confirm_docs(dirty, done)
        return True

    def on_drop(self, target, value, x, y):
        for f in value.get_files():
            p = f.get_path()
            if p:
                self.open_path(p)
        return True

    # -- dialogs ----------------------------------------------------------
    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast.new(msg))

    def error_dialog(self, heading, body):
        dlg = Adw.AlertDialog.new(heading, body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    def goto_dialog(self):
        d = self.current_doc()
        if not d:
            return
        b = d.buffer
        total = b.get_line_count()
        cur = b.get_iter_at_mark(b.get_insert()).get_line() + 1
        spin = Gtk.SpinButton.new_with_range(1, max(1, total), 1)
        spin.set_value(cur)
        spin.set_activates_default(True)
        dlg = Adw.AlertDialog.new("Go to line", f"Line number (1 – {total:,})")
        dlg.set_extra_child(spin)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", "Go to")
        dlg.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("go")
        dlg.set_close_response("cancel")

        def on_response(_, r):
            if r != "go":
                return
            spin.update()
            line = int(spin.get_value()) - 1
            _, it = b.get_iter_at_line(line)
            b.place_cursor(it)
            d.view.scroll_to_mark(b.get_insert(), 0.2, False, 0, 0)
            d.view.grab_focus()

        dlg.connect("response", on_response)
        dlg.present(self)
        spin.grab_focus()

    def link_dialog(self, d):
        sel = d.selected_text().strip()
        is_url = bool(re.match(r"^(https?://|www\.)\S+$", sel))
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        text_row = Adw.EntryRow(title="Text to display")
        url_row = Adw.EntryRow(title="Link address")
        text_row.set_text("" if is_url else sel)
        url_row.set_text(sel if is_url else "https://")
        box.append(text_row)
        box.append(url_row)
        dlg = Adw.AlertDialog.new("Insert link", None)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("insert", "Insert")
        dlg.set_response_appearance("insert", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("insert")
        dlg.set_close_response("cancel")
        text_row.set_activates_default(True)
        url_row.set_activates_default(True)

        def on_response(_, r):
            if r != "insert":
                d.view.grab_focus()
                return
            url = url_row.get_text().strip().replace(" ", "%20")
            if not url or url == "https://":
                return
            text = text_row.get_text().strip() or url
            d.insert_text(f"[{text}]({url})")
            d.view.grab_focus()
            d.schedule_format(0)

        dlg.connect("response", on_response)
        dlg.present(self)
        (url_row if text_row.get_text() else text_row).grab_focus()

    def search_web(self):
        d = self.current_doc()
        q = d.selected_text().strip() if d else ""
        if not q:
            self.toast("Select some text to search for")
            return
        self.open_uri("https://www.bing.com/search?q=" + urllib.parse.quote_plus(q[:500]))

    def open_uri(self, uri):
        Gtk.UriLauncher.new(uri).launch(self, None, None, None)

    # -- printing ---------------------------------------------------------
    def print_doc(self):
        d = self.current_doc()
        if not d:
            return
        text = d.get_text()
        font = Pango.FontDescription.from_string(self.app.settings["font"])
        op = Gtk.PrintOperation()
        op.set_job_name(d.title())
        op.set_embed_page_setup(True)
        if self.app.page_setup:
            op.set_default_page_setup(self.app.page_setup)
        if self.app.print_settings:
            op.set_print_settings(self.app.print_settings)
        state = {}

        def begin(op, ctx):
            layout = ctx.create_pango_layout()
            layout.set_font_description(font)
            layout.set_width(int(ctx.get_width() * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_tabs(tab_array(layout, int(self.app.settings.get("tab_width", 8))))
            layout.set_text(text, -1)
            height = ctx.get_height()
            breaks, top, i = [0], 0.0, 0
            it = layout.get_iter()
            while True:
                _, logical = it.get_line_extents()
                ly = logical.y / Pango.SCALE
                lb = (logical.y + logical.height) / Pango.SCALE
                if lb - top > height and i > breaks[-1]:
                    breaks.append(i)
                    top = ly
                i += 1
                if not it.next_line():
                    break
            state.update(layout=layout, breaks=breaks, n=i)
            op.set_n_pages(len(breaks))

        def draw(op, ctx, page_nr):
            cr = ctx.get_cairo_context()
            layout, breaks = state["layout"], state["breaks"]
            start = breaks[page_nr]
            end = breaks[page_nr + 1] if page_nr + 1 < len(breaks) else state["n"]
            it = layout.get_iter()
            idx, top = 0, None
            while idx < end:
                if idx >= start:
                    _, logical = it.get_line_extents()
                    if top is None:
                        top = logical.y / Pango.SCALE
                    baseline = it.get_baseline() / Pango.SCALE
                    cr.move_to(logical.x / Pango.SCALE, baseline - top)
                    PangoCairo.show_layout_line(cr, it.get_line_readonly())
                idx += 1
                if not it.next_line():
                    break

        op.connect("begin-print", begin)
        op.connect("draw-page", draw)
        try:
            res = op.run(Gtk.PrintOperationAction.PRINT_DIALOG, self)
            if res == Gtk.PrintOperationResult.APPLY:
                self.app.print_settings = op.get_print_settings()
        except GLib.Error as e:
            self.error_dialog("Printing failed", str(e))

    # -- session ----------------------------------------------------------
    def session_state(self):
        tabs = []
        for d in self.docs():
            text = d.get_text()
            if d.is_blank():
                continue
            entry = {
                "path": d.path,
                "modified": d.modified,
                "encoding": d.encoding,
                "eol": EOL_KEYS[d.eol],
                "markdown": d.markdown,
                "syntax_view": d.syntax_view,
                "zoom": d.zoom,
                "cursor": d.buffer.get_iter_at_mark(d.buffer.get_insert()).get_offset(),
            }
            if d.modified or not d.path:
                entry["text"] = text
            tabs.append(entry)
        sel = self.tab_view.get_selected_page()
        active = self.tab_view.get_page_position(sel) if sel else 0
        return {"tabs": tabs, "active": active,
                "size": [self.get_width() or 960, self.get_height() or 680],
                "maximized": self.is_maximized()}

    def restore_state(self, state):
        restored = []
        for t in state.get("tabs", []):
            path = t.get("path")
            if "text" not in t and not (path and os.path.isfile(path)):
                continue
            doc = self.new_tab(focus=False)
            try:
                if "text" in t:
                    doc.set_text(t["text"])
                    doc.path = path
                    doc.buffer.set_modified(bool(t.get("modified", path is None)))
                else:
                    doc.load(path)
            except OSError:
                self.remove_doc(doc)
                continue
            doc.encoding = t.get("encoding", doc.encoding)
            doc.eol = EOL_FROM_KEY.get(t.get("eol"), doc.eol)
            doc.markdown = t.get("markdown", doc.markdown)
            doc.syntax_view = t.get("syntax_view", False)
            doc.zoom = t.get("zoom", 100)
            cur = min(t.get("cursor", 0), doc.buffer.get_char_count())
            doc.buffer.place_cursor(doc.buffer.get_iter_at_offset(cur))
            doc.schedule_format(0)
            self.update_tab(doc)
            restored.append(doc)
        if restored:
            idx = min(state.get("active", 0), len(restored) - 1)
            self.tab_view.set_selected_page(restored[idx].page)
            d = restored[idx]
            GLib.idle_add(lambda: (d.view.scroll_to_mark(d.buffer.get_insert(), 0.2, False, 0, 0), False)[1])
        self.app.update_css()
        return bool(restored)


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------

class NotepadApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        GLib.set_application_name(APP_NAME)
        self.settings = Settings()
        self.css = Gtk.CssProvider()
        self.page_setup = None
        self.print_settings = None
        self._session_source = None
        self.final_saved = False
        self.started = False

    # -- startup ----------------------------------------------------------
    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(ICON)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        static = Gtk.CssProvider()
        static.load_from_string("""
            .menu-row { padding: 0 6px; min-height: 34px; }
            .statusbar { padding: 0 12px; min-height: 26px; font-size: 0.86em; }
            .statusbar menubutton > button { min-height: 20px; padding: 0 6px; font-weight: normal; }
            .findbar { padding: 6px; }
            textview.notepad-doc { caret-color: @accent_color; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), static, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme()

        for name, cb in {
            "new-window": lambda *_: self.new_window(empty=True).present(),
            "quit": lambda *_: self.quit_all(),
            "clear-recent": lambda *_: self.clear_recent(),
        }.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        a = Gio.SimpleAction.new("open-recent", GLib.VariantType.new("s"))
        a.connect("activate", lambda act, p: self.open_files([p.get_string()]))
        self.add_action(a)

        accels = {
            "win.new-tab": ["<Control>n", "<Control>t"],
            "app.new-window": ["<Control><Shift>n"],
            "win.open": ["<Control>o"],
            "win.save": ["<Control>s"],
            "win.save-as": ["<Control><Shift>s"],
            "win.save-all": ["<Control><Alt>s"],
            "win.print": ["<Control>p"],
            "win.close-tab": ["<Control>w"],
            "win.close-window": ["<Control><Shift>w"],
            "app.quit": ["<Control>q"],
            "win.search-web": ["<Control>e"],
            "win.find": ["<Control>f"],
            "win.find-next": ["F3"],
            "win.find-prev": ["<Shift>F3"],
            "win.replace": ["<Control>h"],
            "win.goto": ["<Control>g"],
            "win.time-date": ["F5"],
            "win.zoom-in": ["<Control>plus", "<Control>equal", "<Control>KP_Add"],
            "win.zoom-out": ["<Control>minus", "<Control>KP_Subtract"],
            "win.zoom-reset": ["<Control>0", "<Control>KP_0"],
            "win.bold": ["<Control>b"],
            "win.italic": ["<Control>i"],
            "win.link": ["<Control>k"],
            "win.settings": ["<Control>comma"],
        }
        for action, keys in accels.items():
            self.set_accels_for_action(action, keys)

        self.build_menus()

    def build_menus(self):
        def item(label, action, accel=None, target=None):
            it = Gio.MenuItem.new(label, None)
            if target is not None:
                it.set_action_and_target_value(action, target)
            else:
                it.set_detailed_action(action)
            if accel:
                it.set_attribute_value("accel", GLib.Variant.new_string(accel))
            return it

        def section(*items):
            s = Gio.Menu()
            for i in items:
                s.append_item(i)
            return s

        self.recent_menu = Gio.Menu()
        self.refresh_recent()

        file_menu = Gio.Menu()
        file_menu.append_section(None, section(
            item("New tab", "win.new-tab"),
            item("New window", "app.new-window"),
            item("Open…", "win.open")))
        recent_sec = Gio.Menu()
        recent_sec.append_submenu("Recent", self.recent_menu)
        file_menu.append_section(None, recent_sec)
        file_menu.append_section(None, section(
            item("Save", "win.save"),
            item("Save as…", "win.save-as"),
            item("Save all", "win.save-all")))
        file_menu.append_section(None, section(
            item("Page setup…", "win.page-setup"),
            item("Print…", "win.print")))
        file_menu.append_section(None, section(
            item("Close tab", "win.close-tab"),
            item("Close window", "win.close-window"),
            item("Exit", "app.quit")))

        edit_menu = Gio.Menu()
        edit_menu.append_section(None, section(
            item("Undo", "win.undo", "<Control>z"),
            item("Redo", "win.redo", "<Control>y")))
        edit_menu.append_section(None, section(
            item("Cut", "win.cut", "<Control>x"),
            item("Copy", "win.copy", "<Control>c"),
            item("Paste", "win.paste", "<Control>v"),
            item("Delete", "win.delete", "Delete")))
        edit_menu.append_section(None, section(
            item("Search with Bing", "win.search-web"),
            item("Find", "win.find"),
            item("Find next", "win.find-next"),
            item("Find previous", "win.find-prev"),
            item("Replace", "win.replace"),
            item("Go to", "win.goto")))
        edit_menu.append_section(None, section(
            item("Select all", "win.select-all", "<Control>a"),
            item("Time/Date", "win.time-date")))
        edit_menu.append_section(None, section(item("Font", "win.font")))

        view_menu = Gio.Menu()
        zoom = Gio.Menu()
        zoom.append_item(item("Zoom in", "win.zoom-in"))
        zoom.append_item(item("Zoom out", "win.zoom-out"))
        zoom.append_item(item("Restore default zoom", "win.zoom-reset"))
        zsec = Gio.Menu()
        zsec.append_submenu("Zoom", zoom)
        view_menu.append_section(None, zsec)
        view_menu.append_section(None, section(
            item("Status bar", "app.status-bar"),
            item("Word wrap", "app.word-wrap")))
        view_menu.append_section(None, section(
            item("Markdown formatting", "win.markdown"),
            item("Show Markdown syntax", "win.syntax-view")))

        self.menubar_model = Gio.Menu()
        self.menubar_model.append_submenu("_File", file_menu)
        self.menubar_model.append_submenu("_Edit", edit_menu)
        self.menubar_model.append_submenu("_View", view_menu)

        self.heading_model = Gio.Menu()
        self.heading_model.append_item(item("Body text", "win.heading", target=GLib.Variant.new_int32(0)))
        for i in range(1, 7):
            self.heading_model.append_item(item(f"Heading {i}", "win.heading", target=GLib.Variant.new_int32(i)))

        self.eol_model = Gio.Menu()
        for eol, label in EOLS:
            self.eol_model.append_item(item(label, "win.eol", target=GLib.Variant.new_string(EOL_KEYS[eol])))
        self.encoding_model = Gio.Menu()
        for enc, label in ENCODINGS:
            self.encoding_model.append_item(item(label, "win.encoding", target=GLib.Variant.new_string(enc)))

        for key in ("status_bar", "word_wrap"):
            a = Gio.SimpleAction.new_stateful(key.replace("_", "-"), None,
                                              GLib.Variant.new_boolean(self.settings[key]))
            a.connect("change-state", self.on_bool_setting, key)
            self.add_action(a)

    def on_bool_setting(self, action, value, key):
        action.set_state(value)
        self.set_setting(key, value.get_boolean())

    # -- activation -------------------------------------------------------
    def notepad_windows(self):
        return [w for w in self.get_windows() if isinstance(w, NotepadWindow) and not w.closed]

    def active_notepad(self):
        w = self.get_active_window()
        if isinstance(w, NotepadWindow) and not w.closed:
            return w
        ws = self.notepad_windows()
        return ws[0] if ws else None

    def new_window(self, empty=False, size=None, maximized=False):
        w = NotepadWindow(self, size, maximized)
        if empty:
            w.new_tab(focus=True)
        return w

    def start(self):
        if self.started:
            return
        self.started = True
        restored = False
        if self.settings["startup"] == "continue":
            data = read_json(SESSION_FILE, {})
            for ws in data.get("windows", []) if isinstance(data, dict) else []:
                w = self.new_window(size=ws.get("size"), maximized=ws.get("maximized", False))
                if w.restore_state(ws):
                    w.present()
                    restored = True
                else:
                    w.closing_ok = True
                    w.closed = True
                    w.destroy()
        if not restored:
            self.new_window(empty=True).present()

    def do_activate(self):
        if not self.started:
            self.start()
            return
        w = self.active_notepad()
        if w:
            w.present()
        else:
            self.new_window(empty=True).present()

    def do_open(self, files, n_files, hint):
        paths = [f.get_path() for f in files if f.get_path()]
        if not self.started:
            self.start()
        self.open_files(paths)

    def open_files(self, paths):
        if not paths:
            return
        w = self.active_notepad()
        if self.settings["open_in"] == "window" or w is None:
            if not (w and len(w.docs()) == 1 and w.docs()[0].is_blank()):
                w = self.new_window()
        for p in paths:
            w.open_path(p)
        if not w.docs():
            w.new_tab()
        w.present()

    # -- settings ---------------------------------------------------------
    def set_setting(self, key, value):
        self.settings[key] = value
        self.settings.save()
        if key == "theme":
            self.apply_theme()
        elif key in ("font", "tab_width"):
            self.update_css()
        elif key == "word_wrap":
            for w in self.notepad_windows():
                for d in w.docs():
                    d.apply_wrap(value)
            act = self.lookup_action("word-wrap")
            if act:
                act.set_state(GLib.Variant.new_boolean(value))
        elif key == "status_bar":
            for w in self.notepad_windows():
                w.statusbar.set_visible(value)
            act = self.lookup_action("status-bar")
            if act:
                act.set_state(GLib.Variant.new_boolean(value))
        elif key == "recent_enabled":
            self.refresh_recent()

    def apply_theme(self):
        scheme = {"light": Adw.ColorScheme.FORCE_LIGHT, "dark": Adw.ColorScheme.FORCE_DARK}.get(
            self.settings["theme"], Adw.ColorScheme.DEFAULT)
        Adw.StyleManager.get_default().set_color_scheme(scheme)

    def update_css(self):
        fd = Pango.FontDescription.from_string(self.settings["font"])
        family = fd.get_family() or "Monospace"
        size = fd.get_size() / Pango.SCALE if fd.get_size() else 11
        weight = int(fd.get_weight())
        style = {Pango.Style.ITALIC: "italic", Pango.Style.OBLIQUE: "oblique"}.get(fd.get_style(), "normal")
        family = family.replace('"', "")
        # base rule first; per-document zoom rules come later so they win
        rules = [f'textview.notepad-doc {{ font-family: "{family}"; font-weight: {weight}; '
                 f'font-style: {style}; font-size: {size:.2f}pt; }}']
        for w in self.notepad_windows():
            for d in w.docs():
                rules.append(f"textview.doc-{d.uid} {{ font-size: {size * d.zoom / 100:.2f}pt; }}")
        self.css.load_from_string("\n".join(rules))
        for w in self.notepad_windows():
            for d in w.docs():
                d.apply_tabs()

    def show_settings(self, win):
        s = self.settings
        dlg = Adw.PreferencesDialog(title="Settings")
        page = Adw.PreferencesPage(title="Settings", icon_name="emblem-system-symbolic")
        dlg.add(page)

        def combo(title, subtitle, options, key):
            row = Adw.ComboRow(title=title, subtitle=subtitle or "")
            row.set_model(Gtk.StringList.new([o[1] for o in options]))
            keys = [o[0] for o in options]
            row.set_selected(keys.index(s[key]) if s[key] in keys else 0)
            row.connect("notify::selected", lambda r, _: self.set_setting(key, keys[r.get_selected()]))
            return row

        def switch(title, subtitle, key):
            row = Adw.SwitchRow(title=title, subtitle=subtitle or "")
            row.set_active(bool(s[key]))
            row.connect("notify::active", lambda r, _: self.set_setting(key, r.get_active()))
            return row

        g = Adw.PreferencesGroup(title="Appearance")
        g.add(combo("App theme", "Select which app theme to display",
                    [("system", "Use system setting"), ("light", "Light"), ("dark", "Dark")], "theme"))
        font_row = Adw.ActionRow(title="Font", subtitle="Family, style and size")
        fbtn = Gtk.FontDialogButton.new(Gtk.FontDialog(title="Font"))
        fbtn.set_font_desc(Pango.FontDescription.from_string(s["font"]))
        fbtn.set_valign(Gtk.Align.CENTER)
        fbtn.connect("notify::font-desc", lambda b, _: self.set_setting("font", b.get_font_desc().to_string()))
        font_row.add_suffix(fbtn)
        g.add(font_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Text formatting")
        g.add(switch("Word wrap", "Fit text within the window by default", "word_wrap"))
        g.add(switch("Formatting", "Use Markdown formatting (bold, headings, lists) in new tabs", "markdown_new"))
        g.add(switch("Status bar", "Show line, column and character count", "status_bar"))
        g.add(combo("Tab key", "What pressing the Tab key types",
                    [(4, "4 spaces"), (2, "2 spaces"), (8, "8 spaces"), (0, "A Tab character")], "tab_key"))
        g.add(combo("Tab width", "How wide existing Tab characters are drawn",
                    [(8, "8 spaces (like Windows)"), (4, "4 spaces"), (2, "2 spaces")], "tab_width"))
        g.add(combo("Line ending for new files", None,
                    [("\n", "Unix (LF)"), ("\r\n", "Windows (CRLF)")], "default_eol"))
        page.add(g)

        g = Adw.PreferencesGroup(title="Opening Notepad")
        g.add(combo("Opening files", "Choose where files open",
                    [("tab", "Open in a new tab"), ("window", "Open in a new window")], "open_in"))
        g.add(combo("When Notepad starts", None,
                    [("continue", "Open content from the previous session"),
                     ("new", "Start new session and discard unsaved changes")], "startup"))
        g.add(switch("Recent files", "Show recently opened files in the File menu", "recent_enabled"))
        page.add(g)

        g = Adw.PreferencesGroup(title="About this app")
        about = Adw.ActionRow(title=f"{APP_NAME} {VERSION}",
                              subtitle="A Linux text editor modelled on Windows 11 Notepad")
        g.add(about)
        page.add(g)
        dlg.present(win)

    # -- recent files -----------------------------------------------------
    def add_recent(self, path):
        if not self.settings["recent_enabled"]:
            return
        path = os.path.abspath(path)
        rec = [p for p in self.settings["recent"] if p != path]
        rec.insert(0, path)
        self.settings["recent"] = rec[:MAX_RECENT]
        self.settings.save()
        self.refresh_recent()

    def clear_recent(self):
        self.settings["recent"] = []
        self.settings.save()
        self.refresh_recent()

    def refresh_recent(self):
        m = self.recent_menu
        m.remove_all()
        rec = self.settings["recent"] if self.settings["recent_enabled"] else []
        files = Gio.Menu()
        home = os.path.expanduser("~")
        for p in rec:
            label = p.replace(home, "~", 1) if p.startswith(home) else p
            it = Gio.MenuItem.new(label.replace("_", "__"), None)
            it.set_action_and_target_value("app.open-recent", GLib.Variant.new_string(p))
            files.append_item(it)
        if not rec:
            files.append_item(Gio.MenuItem.new("No recent files", "app.none"))
        m.append_section(None, files)
        if rec:
            clear = Gio.Menu()
            clear.append("Clear recent files", "app.clear-recent")
            m.append_section(None, clear)

    # -- printing ---------------------------------------------------------
    def page_setup_dialog(self, win):
        if self.print_settings is None:
            self.print_settings = Gtk.PrintSettings()
        self.page_setup = Gtk.print_run_page_setup_dialog(win, self.page_setup, self.print_settings)

    # -- session ----------------------------------------------------------
    def schedule_session_save(self):
        if self.settings["startup"] != "continue" or self.final_saved:
            return
        if self._session_source:
            GLib.source_remove(self._session_source)
        self._session_source = GLib.timeout_add(1500, self._session_timeout)

    def _session_timeout(self):
        self._session_source = None
        self.save_session()
        return False

    def save_session(self, final=False):
        if self.final_saved:
            return
        if self._session_source:
            GLib.source_remove(self._session_source)
            self._session_source = None
        if self.settings["startup"] != "continue":
            return
        windows = [w.session_state() for w in self.notepad_windows()]
        windows = [w for w in windows if w["tabs"]]
        try:
            write_json(SESSION_FILE, {"version": 1, "windows": windows})
        except OSError as e:
            print("notepad: could not save session:", e, file=sys.stderr)
        if final:
            self.final_saved = True

    def after_window_closed(self, win):
        if self.notepad_windows():
            GLib.idle_add(lambda: (self.save_session(), False)[1])
        elif self.settings["startup"] == "continue":
            self.save_session(final=True)
        else:
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass

    def quit_all(self):
        wins = self.notepad_windows()
        if self.settings["startup"] == "continue":
            self.save_session(final=True)
            for w in wins:
                w.closing_ok = True
                w.close()
            return

        def step(ok=True):
            if not ok:
                return
            if not wins:
                return
            w = wins.pop(0)
            dirty = [d for d in w.docs() if d.modified]

            def done(ok2):
                if ok2:
                    w.closing_ok = True
                    w.close()
                    step()

            w.present()
            w.confirm_docs(dirty, done)

        step()


def main(argv):
    app = NotepadApp()
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))

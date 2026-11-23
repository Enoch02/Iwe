# window.py
#
# Copyright 2026 Enoch Adesanya
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import re
from gettext import gettext as _
from html import escape as escape_html
from html import unescape as unescape_html

import gi

gi.require_version("WebKit", "6.0")

from gi.repository import Adw
from gi.repository import Gdk
from gi.repository import Gio
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk
from gi.repository import Pango
from gi.repository import WebKit

try:
    import markdown as markdown_lib
except ImportError:
    markdown_lib = None

# HTML shell for a rendered document; the tokens are replaced when a
# document is shown (__CSS__ pulls in the bundled document.css, __DARK__
# follows the light/dark preference).
HTML_TEMPLATE = """<!DOCTYPE html>
<html __DARK__>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Documents are untrusted input: block anything they embed or script.
     JavaScript is enabled in the view so the app can read and restore the
     scroll position (WebKitGTK exposes no scroll API), but this policy stops
     a document's own scripts from running, as before. -->
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src file: data: http: https:; style-src 'unsafe-inline'">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
__BODY__
</body>
</html>
"""


def _load_stylesheet():
    """Read the bundled viewer stylesheet (document.css in the gresource)."""
    data = Gio.resources_lookup_data(
        "/com/enoch02/iwe/document.css", Gio.ResourceLookupFlags.NONE
    )
    return data.get_data().decode("utf-8")


MARKDOWN_EXTENSIONS = ["tables", "fenced_code", "toc", "sane_lists"]

# Files opened through the document portal (the sandboxed build) live on a
# FUSE mount that delivers no Gio.FileMonitor events when the real file is
# edited, so the open document is polled instead. One stat per second.
POLL_INTERVAL_S = 1
STAMP_ATTRIBUTES = "time::modified,time::modified-usec,standard::size"

# JavaScript used to keep the reader's place across re-renders.
SCROLL_GET_JS = "Math.round(window.scrollY)"
SCROLL_SET_JS = "window.scrollTo(0, %d)"


@Gtk.Template(resource_path="/com/enoch02/iwe/window.ui")
class IweWindow(Adw.ApplicationWindow):
    __gtype_name__ = "IweWindow"

    content_stack = Gtk.Template.Child()
    document_box = Gtk.Template.Child()
    sidebar_stack = Gtk.Template.Child()
    outline_list = Gtk.Template.Child()
    outline_empty_page = Gtk.Template.Child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._body_html = None
        self._document_title = None
        self._base_uri = None
        self._outline_anchors = []
        self._file = None
        self._file_stamp = None
        self._poll_source = None
        self._loading = False
        self._restore_scroll = None

        self._build_document_view()
        self._setup_open_action()
        self._setup_drop_target()
        self._setup_outline()

        # Stop polling the file when the window goes away
        self.connect("close-request", self._on_close_request)

        # Re-render the open document when the light/dark preference changes
        Adw.StyleManager.get_default().connect("notify::dark", self._on_dark_changed)

    def _build_document_view(self):
        """Create the read-only WebView showing the rendered document."""
        self.webview = WebKit.WebView()
        self.webview.set_hexpand(True)
        self.webview.set_vexpand(True)

        self.webview.get_settings().set_enable_javascript(True)

        self.webview.connect("decide-policy", self._on_decide_policy)
        self.webview.connect("load-changed", self._on_load_changed)
        self.document_box.append(self.webview)

    def _setup_open_action(self):
        """Register win.open-file, shared by both buttons and Ctrl+O."""
        open_action = Gio.SimpleAction.new("open-file", None)
        open_action.connect("activate", lambda *_: self._open_file_dialog())
        self.add_action(open_action)

    def _setup_drop_target(self):
        """Accept .md files dropped on the window or the document view."""
        for widget in (self, self.webview):
            drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop_target.connect("drop", self._on_file_dropped)
            widget.add_controller(drop_target)

    def _setup_outline(self):
        """Jump to a heading when its outline row is activated."""
        self.outline_list.connect("row-activated", self._on_outline_row_activated)

    def _open_file_dialog(self):
        """Show the file chooser, filtered to markdown files."""
        md_filter = Gtk.FileFilter()
        md_filter.set_name(_("Markdown files (*.md, *.markdown)"))
        md_filter.add_pattern("*.md")
        md_filter.add_pattern("*.markdown")

        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All files"))
        all_filter.add_pattern("*")

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(md_filter)
        filters.append(all_filter)

        dialog = Gtk.FileDialog(title=_("Open Markdown File"))
        dialog.set_filters(filters)
        dialog.set_default_filter(md_filter)
        dialog.open(self, None, self._on_open_dialog_done)

    def _on_open_dialog_done(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled or failed

        if file is not None:
            self._load_file(file)

    @property
    def is_empty(self):
        """Whether the window is still showing the welcome page.

        A load in progress counts as content, so opening several files
        at once does not funnel them into the same window.
        """
        return self._body_html is None and not self._loading

    def open_file(self, file):
        """Open a document supplied by the application.

        Used when the app is started with a file argument or when a
        running instance is asked to open a markdown document.
        """
        self._load_file(file)

    def _load_file(self, file, reload=False):
        """Read a file and render it in the document view.

        reload marks the read as triggered by a change on disk: a
        failure then keeps the document already on screen instead of
        interrupting the reader with an error dialog.
        """
        if markdown_lib is None:
            self._show_error(
                _("Markdown support unavailable"),
                _("The Python “markdown” package is required to render documents."),
            )
            return

        # Stamp the file before reading it: if it changes in between, the
        # next poll sees a different stamp and reloads once more.
        self._loading = True
        stamp = self._stamp(file)
        file.load_contents_async(
            None, lambda f, result: self._on_file_loaded(f, result, reload, stamp)
        )

    def _on_file_loaded(self, file, result, reload=False, stamp=None):
        self._loading = False
        try:
            loaded = file.load_contents_finish(result)
        except GLib.Error as error:
            if not reload:
                self._show_error(_("Could not open file"), str(error))
            return

        contents = next(value for value in loaded if isinstance(value, bytes))
        text = contents.decode("utf-8-sig", errors="replace")

        # A Markdown instance (not the module-level helper) so we can read
        # toc_tokens afterwards for the sidebar outline
        md = markdown_lib.Markdown(extensions=MARKDOWN_EXTENSIONS)
        self._body_html = md.convert(text)
        self._document_title = file.get_basename()

        parent = file.get_parent()
        self._base_uri = parent.get_uri() if parent is not None else None

        self.set_title(self._document_title)
        self._render_document(keep_position=reload)
        self._populate_outline(getattr(md, "toc_tokens", []))
        self._file_stamp = stamp
        self._watch_file(file)

    @staticmethod
    def _stamp(file):
        """Fingerprint of the file's identity on disk (mtime and size)."""
        try:
            info = file.query_info(STAMP_ATTRIBUTES, Gio.FileQueryInfoFlags.NONE, None)
        except GLib.Error:
            return None

        return (
            info.get_attribute_uint64("time::modified"),
            info.get_attribute_uint32("time::modified-usec"),
            info.get_attribute_uint64("standard::size"),
        )

    def _watch_file(self, file):
        """Poll the open document so edits on disk get picked up.

        A poll is used instead of Gio.FileMonitor: files opened through
        the document portal (as the sandboxed build does) sit on a FUSE
        mount that reports no monitor events when the real file changes.
        """
        self._file = file
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
        self._poll_source = GLib.timeout_add_seconds(POLL_INTERVAL_S, self._poll_file)

    def _poll_file(self):
        """Reload the document if its file changed on disk."""
        if self._file is not None and not self._loading:
            stamp = self._stamp(self._file)
            if stamp is not None and stamp != self._file_stamp:
                self._load_file(self._file, reload=True)
        return GLib.SOURCE_CONTINUE

    def _on_close_request(self, *_args):
        """Stop polling the file when the window goes away."""
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        return False

    def _render_document(self, keep_position=False):
        """Build the HTML page for the current document and load it.

        keep_position keeps the reader at the same scroll offset, for
        re-renders of the document already on screen (a change on disk or
        the light/dark preference flipping). Opening another document, or
        jumping to a heading, starts from wherever that load lands.
        """
        if self._body_html is None:
            return

        dark = Adw.StyleManager.get_default().get_dark()
        page = HTML_TEMPLATE
        page = page.replace("__DARK__", 'class="dark"' if dark else "")
        page = page.replace("__TITLE__", escape_html(self._document_title or ""))
        page = page.replace("__CSS__", _load_stylesheet())
        page = page.replace("__BODY__", self._body_html)

        self._load_page(page, keep_position)
        self.content_stack.set_visible_child_name("document")

    def _load_page(self, page, keep_position):
        """Load a rendered page, reading the scroll offset first if needed.

        load_html() replaces the document, so the offset has to be captured
        before the load starts; _on_load_changed() puts it back once the new
        page has finished loading.
        """
        if not keep_position:
            self._restore_scroll = None
            self.webview.load_html(page, self._base_uri)
            return

        def captured(view, result, _data):
            self._restore_scroll = self._js_number(view, result)
            self.webview.load_html(page, self._base_uri)

        self.webview.evaluate_javascript(
            SCROLL_GET_JS, -1, None, None, None, captured, None
        )

    @staticmethod
    def _js_number(view, result):
        """Numeric value of a finished evaluate_javascript() call, or None."""
        try:
            return int(view.evaluate_javascript_finish(result).to_double())
        except (GLib.Error, AttributeError, TypeError, ValueError):
            return None  # no usable value: reload without keeping the place

    def _on_load_changed(self, _webview, event):
        """Restore the reader's scroll position after a re-render."""
        if event != WebKit.LoadEvent.FINISHED:
            return

        scroll = self._restore_scroll
        self._restore_scroll = None
        if scroll is not None:
            self.webview.evaluate_javascript(
                SCROLL_SET_JS % scroll, -1, None, None, None, None, None
            )

    def _on_dark_changed(self, _style_manager, _pspec):
        if self._body_html is not None:
            self._render_document(keep_position=True)

    def _populate_outline(self, tokens):
        """Fill the sidebar with the heading tree of the open document."""
        while self.outline_list.get_first_child() is not None:
            self.outline_list.remove(self.outline_list.get_first_child())
        self._outline_anchors.clear()

        if self._append_outline_rows(tokens, 0):
            self.sidebar_stack.set_visible_child_name("outline")
        else:
            self.outline_empty_page.set_description(_("This document has no headings"))
            self.sidebar_stack.set_visible_child_name("empty")

    def _append_outline_rows(self, nodes, depth):
        """Append one row per heading (recursing into children); return count."""
        added = 0
        for node in nodes:
            text = self._outline_label(node)

            label = Gtk.Label(label=text, xalign=0, hexpand=True)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_margin_start(6 + 10 * depth)
            label.set_margin_end(8)
            label.set_margin_top(3)
            label.set_margin_bottom(3)
            label.set_tooltip_text(text)

            row = Gtk.ListBoxRow()
            row.set_child(label)
            self.outline_list.append(row)

            # row-activated only hands us the row, so remember its anchor;
            # rows are appended in order, so row.get_index() maps back
            self._outline_anchors.append(node.get("id") or "")
            added += 1
            added += self._append_outline_rows(node.get("children") or (), depth + 1)
        return added

    @staticmethod
    def _outline_label(node):
        """Plain text for a heading: toc names keep tags and entities."""
        text = re.sub(r"<[^>]+>", "", node.get("name") or "")
        text = unescape_html(text).strip()
        return text or _("Untitled heading")

    def _on_outline_row_activated(self, _listbox, row):
        """Scroll the document to the heading the row stands for."""
        index = row.get_index()
        if not 0 <= index < len(self._outline_anchors):
            return

        anchor = self._outline_anchors[index]
        if not anchor or self._base_uri is None:
            return

        # Jumping to a heading wins over a scroll restore still pending from
        # a re-render that has not finished loading yet
        self._restore_scroll = None

        # Fragment navigation into our own document: WebKit scrolls to the
        # anchor natively (decide-policy allows both API loads,
        # NavigationType.OTHER, and base + '#…' URIs)
        self.webview.load_uri(f"{self._base_uri}#{anchor}")

    def _on_file_dropped(self, _target, value, _x, _y):
        for file in value.get_files():
            name = (file.get_basename() or "").lower()
            if name.endswith((".md", ".markdown")):
                self._load_file(file)
                return True
        return False

    def _on_decide_policy(self, _webview, decision, decision_type):
        """Allow our own loads and anchors, open web links externally,
        and block everything else so the reader stays on the document."""
        if decision_type not in (
            WebKit.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False

        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ""

        if uri.startswith(("http://", "https://")):
            # Web links open in the default browser
            launcher = Gtk.UriLauncher()
            launcher.set_uri(uri)
            launcher.launch(self, None, None)
            decision.ignore()
        elif action.get_navigation_type() == WebKit.NavigationType.OTHER:
            # API-initiated loads: our load_html() would otherwise be
            # cancelled here, leaving the view blank
            decision.use()
        elif self._base_uri and (
            uri == self._base_uri or uri.startswith(self._base_uri + "#")
        ):
            # In-page anchor jumps (e.g. from the table of contents)
            decision.use()
        else:
            decision.ignore()

        return True

    def _show_error(self, heading, body):
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

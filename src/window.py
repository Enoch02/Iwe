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

gi.require_version('WebKit', '6.0')

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
    data = Gio.resources_lookup_data('/com/enoch02/iwe/document.css',
                                     Gio.ResourceLookupFlags.NONE)
    return data.get_data().decode('utf-8')


MARKDOWN_EXTENSIONS = ['tables', 'fenced_code', 'toc', 'sane_lists']


@Gtk.Template(resource_path='/com/enoch02/iwe/window.ui')
class IweWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'IweWindow'

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

        self._build_document_view()
        self._setup_open_action()
        self._setup_drop_target()
        self._setup_outline()

        # Re-render the open document when the light/dark preference changes
        Adw.StyleManager.get_default().connect('notify::dark', self._on_dark_changed)

    def _build_document_view(self):
        """Create the read-only WebView showing the rendered document."""
        self.webview = WebKit.WebView()
        self.webview.set_hexpand(True)
        self.webview.set_vexpand(True)

        self.webview.get_settings().set_enable_javascript(False)

        self.webview.connect('decide-policy', self._on_decide_policy)
        self.document_box.append(self.webview)

    def _setup_open_action(self):
        """Register win.open-file, shared by both buttons and Ctrl+O."""
        open_action = Gio.SimpleAction.new('open-file', None)
        open_action.connect('activate', lambda *_: self._open_file_dialog())
        self.add_action(open_action)

    def _setup_drop_target(self):
        """Accept .md files dropped on the window or the document view."""
        for widget in (self, self.webview):
            drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop_target.connect('drop', self._on_file_dropped)
            widget.add_controller(drop_target)

    def _setup_outline(self):
        """Jump to a heading when its outline row is activated."""
        self.outline_list.connect('row-activated', self._on_outline_row_activated)

    def _open_file_dialog(self):
        """Show the file chooser, filtered to markdown files."""
        md_filter = Gtk.FileFilter()
        md_filter.set_name(_('Markdown files (*.md, *.markdown)'))
        md_filter.add_pattern('*.md')
        md_filter.add_pattern('*.markdown')

        all_filter = Gtk.FileFilter()
        all_filter.set_name(_('All files'))
        all_filter.add_pattern('*')

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(md_filter)
        filters.append(all_filter)

        dialog = Gtk.FileDialog(title=_('Open Markdown File'))
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

    def _load_file(self, file):
        """Read a file and render it in the document view."""
        if markdown_lib is None:
            self._show_error(
                _('Markdown support unavailable'),
                _('The Python “markdown” package is required to render documents.'))
            return

        file.load_contents_async(None, self._on_file_loaded)

    def _on_file_loaded(self, file, result):
        try:
            loaded = file.load_contents_finish(result)
        except GLib.Error as error:
            self._show_error(_('Could not open file'), str(error))
            return

        contents = next(value for value in loaded if isinstance(value, bytes))
        text = contents.decode('utf-8-sig', errors='replace')

        # A Markdown instance (not the module-level helper) so we can read
        # toc_tokens afterwards for the sidebar outline
        md = markdown_lib.Markdown(extensions=MARKDOWN_EXTENSIONS)
        self._body_html = md.convert(text)
        self._document_title = file.get_basename()

        parent = file.get_parent()
        self._base_uri = parent.get_uri() if parent is not None else None

        self.set_title(self._document_title)
        self._render_document()
        self._populate_outline(getattr(md, 'toc_tokens', []))

    def _render_document(self):
        """Build the HTML page for the current document and load it."""
        if self._body_html is None:
            return

        dark = Adw.StyleManager.get_default().get_dark()
        page = HTML_TEMPLATE
        page = page.replace('__DARK__', 'class="dark"' if dark else '')
        page = page.replace('__TITLE__', escape_html(self._document_title or ''))
        page = page.replace('__CSS__', _load_stylesheet())
        page = page.replace('__BODY__', self._body_html)

        self.webview.load_html(page, self._base_uri)
        self.content_stack.set_visible_child_name('document')

    def _on_dark_changed(self, _style_manager, _pspec):
        if self._body_html is not None:
            self._render_document()

    def _populate_outline(self, tokens):
        """Fill the sidebar with the heading tree of the open document."""
        while self.outline_list.get_first_child() is not None:
            self.outline_list.remove(self.outline_list.get_first_child())
        self._outline_anchors.clear()

        if self._append_outline_rows(tokens, 0):
            self.sidebar_stack.set_visible_child_name('outline')
        else:
            self.outline_empty_page.set_description(_('This document has no headings'))
            self.sidebar_stack.set_visible_child_name('empty')

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
            self._outline_anchors.append(node.get('id') or '')
            added += 1
            added += self._append_outline_rows(node.get('children') or (), depth + 1)
        return added

    @staticmethod
    def _outline_label(node):
        """Plain text for a heading: toc names keep tags and entities."""
        text = re.sub(r'<[^>]+>', '', node.get('name') or '')
        text = unescape_html(text).strip()
        return text or _('Untitled heading')

    def _on_outline_row_activated(self, _listbox, row):
        """Scroll the document to the heading the row stands for."""
        index = row.get_index()
        if not 0 <= index < len(self._outline_anchors):
            return

        anchor = self._outline_anchors[index]
        if not anchor or self._base_uri is None:
            return

        # Fragment navigation into our own document: WebKit scrolls to the
        # anchor natively — no JavaScript needed (decide-policy allows both
        # API loads, NavigationType.OTHER, and base + '#…' URIs)
        self.webview.load_uri(f'{self._base_uri}#{anchor}')

    def _on_file_dropped(self, _target, value, _x, _y):
        for file in value.get_files():
            name = (file.get_basename() or '').lower()
            if name.endswith(('.md', '.markdown')):
                self._load_file(file)
                return True
        return False

    def _on_decide_policy(self, _webview, decision, decision_type):
        """Allow our own loads and anchors, open web links externally,
        and block everything else so the reader stays on the document."""
        if decision_type not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION,
                                 WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False

        action = decision.get_navigation_action()
        uri = action.get_request().get_uri() or ''

        if uri.startswith(('http://', 'https://')):
            # Web links open in the default browser
            launcher = Gtk.UriLauncher()
            launcher.set_uri(uri)
            launcher.launch(self, None, None)
            decision.ignore()
        elif action.get_navigation_type() == WebKit.NavigationType.OTHER:
            # API-initiated loads: our load_html() would otherwise be
            # cancelled here, leaving the view blank
            decision.use()
        elif self._base_uri and (uri == self._base_uri or
                                 uri.startswith(self._base_uri + '#')):
            # In-page anchor jumps (e.g. from the table of contents)
            decision.use()
        else:
            decision.ignore()

        return True

    def _show_error(self, heading, body):
        dialog = Adw.AlertDialog.new(heading, body)
        dialog.add_response('ok', _('OK'))
        dialog.set_default_response('ok')
        dialog.set_close_response('ok')
        dialog.present(self)

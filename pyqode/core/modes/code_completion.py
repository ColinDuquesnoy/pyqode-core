# -*- coding: utf-8 -*-
"""
This module contains the code completion mode and the related classes.
"""
import logging
import re
import sys
from pyqode.core.api.mode import Mode
from pyqode.core.backend import NotConnected
from pyqode.core.qt import QtWidgets, QtCore, QtGui
from pyqode.core.managers.backend import BackendManager
from pyqode.core.api.utils import DelayJobRunner, memoized, TextHelper
from pyqode.core import backend
# pylint: disable=too-many-instance-attributes, missing-docstring


def _logger():
    return logging.getLogger(__name__)


class CodeCompletionMode(Mode, QtCore.QObject):
    """
    This mode provides code completion system wich is extensible. It takes care
    of running the completion request in a background process using one or more
    completion provider(s).

    To implement a code completion for a specific language, you only need to
    implement new
    :class:`pyqode.core.backend.workers.CodeCompletionWorker.Provider`

    The completion popup is shown the user press **ctrl+space** or
    automatically while the user is typing some code (this can be configured
    using a series of properties).

    """
    @property
    def trigger_key(self):
        return self._trigger_key

    @trigger_key.setter
    def trigger_key(self, value):
        self._trigger_key = value

    @property
    def trigger_length(self):
        return self._trigger_len

    @trigger_length.setter
    def trigger_length(self, value):
        self._trigger_len = value

    @property
    def trigger_symbols(self):
        return self._trigger_symbols

    @trigger_symbols.setter
    def trigger_symbols(self, value):
        self._trigger_symbols = value

    @property
    def show_tooltips(self):
        return self._show_tooltips

    @show_tooltips.setter
    def show_tooltips(self, value):
        self._show_tooltips = value

    @property
    def case_sensitive(self):
        return self._case_sensitive

    @case_sensitive.setter
    def case_sensitive(self, value):
        self._case_sensitive = value

    @property
    def completion_prefix(self):
        """
        Returns the current completion prefix
        """
        return self._helper.word_under_cursor(
            select_whole_word=False).selectedText().strip()
    def __init__(self):
        Mode.__init__(self)
        QtCore.QObject.__init__(self)
        self._current_completion = ""
        # use to display a waiting cursor if completion provider takes too much
        # time
        self._job_runner = DelayJobRunner(delay=1000)
        self._tooltips = {}
        self._cursor_line = -1
        self._cancel_next = False
        self._request_cnt = 0
        self._last_completion_prefix = ""
        self._trigger_key = None
        self._trigger_len = None
        self._trigger_symbols = None
        self._show_tooltips = None
        self._case_sensitive = None
        self._data = None
        self._completer = None
        self._col = 0
        self._init_settings()

    def _init_settings(self):
        self._trigger_key = QtCore.Qt.Key_Space
        self._trigger_len = 1
        self._trigger_symbols = ['.']
        self._show_tooltips = True
        self._case_sensitive = False

    def request_completion(self):
        """
        Requests a code completion at the current cursor position.
        """
        self._col = self.editor.textCursor().positionInBlock() - len(self.completion_prefix)
        helper = TextHelper(self.editor)
        if not self._request_cnt:
            # only check first byte
            disabled_zone = TextHelper(self.editor).is_comment_or_string(
                self.editor.textCursor())
            if disabled_zone:
                _logger().debug(
                    "cc: cancel request, cursor is in a disabled zone")
                return False
            self._request_cnt += 1
            self._collect_completions(self.editor.toPlainText(),
                                      helper.current_line_nbr(),
                                      helper.current_column_nbr(),
                                      self.editor.file.path,
                                      self.editor.file.encoding,
                                      self.completion_prefix)
            return True
        return False

    def on_install(self, editor):
        self._completer = QtWidgets.QCompleter([""], editor)
        self._completer.setCompletionMode(self._completer.PopupCompletion)
        self._completer.activated.connect(self._insert_completion)
        self._completer.highlighted.connect(
            self._on_selected_completion_changed)
        self._completer.setModel(QtGui.QStandardItemModel())
        self._helper = TextHelper(editor)
        Mode.on_install(self, editor)

    def on_uninstall(self):
        self._completer.popup().hide()
        self._completer = None

    def on_state_changed(self, state):
        if state:
            self.editor.focused_in.connect(self._on_focus_in)
            self.editor.key_pressed.connect(self._on_key_pressed)
            self.editor.post_key_pressed.connect(self._on_key_released)
            self._completer.highlighted.connect(
                self._display_completion_tooltip)
            self.editor.cursorPositionChanged.connect(
                self._on_cursor_position_changed)
        else:
            self.editor.focused_in.disconnect(self._on_focus_in)
            self.editor.key_pressed.disconnect(self._on_key_pressed)
            self.editor.post_key_pressed.disconnect(self._on_key_released)
            self._completer.highlighted.disconnect(
                self._display_completion_tooltip)
            self.editor.cursorPositionChanged.disconnect(
                self._on_cursor_position_changed)

    def _on_focus_in(self, event):
        """
        Resets completer widget

        :param event: QFocusEvents
        """
        # pylint: disable=unused-argument
        self._completer.setWidget(self.editor)

    def _on_results_available(self, status, results):
        _logger().debug("cc: got completion results")
        self.editor.set_mouse_cursor(QtCore.Qt.IBeamCursor)
        all_results = []
        if status:
            for res in results:
                all_results += res
        self._request_cnt -= 1
        self._show_completions(all_results)

    def _on_key_pressed(self, event):
        QtWidgets.QToolTip.hideText()
        is_shortcut = self._is_shortcut(event)
        # handle completer popup events ourselves
        if self._completer.popup().isVisible():
            self._handle_completer_events(event)
            if is_shortcut:
                event.accept()
        if is_shortcut:
            self.request_completion()
            event.accept()

    @staticmethod
    def _is_navigation_key(event):
        return (event.key() == QtCore.Qt.Key_Backspace or
                event.key() == QtCore.Qt.Key_Back or
                event.key() == QtCore.Qt.Key_Delete or
                event.key() == QtCore.Qt.Key_Left or
                event.key() == QtCore.Qt.Key_Right or
                event.key() == QtCore.Qt.Key_Up or
                event.key() == QtCore.Qt.Key_Down or
                event.key() == QtCore.Qt.Key_Space)

    @staticmethod
    def _is_end_of_word_char(event, is_printable, symbols, seps):
        ret_val = False
        if is_printable and symbols:
            k = event.text()
            ret_val = (k in seps and k not in symbols)
        return ret_val

    def _update_prefix(self, event, is_end_of_word, is_navigation_key):
        self._completer.setCompletionPrefix(self.completion_prefix)
        cnt = self._completer.completionCount()
        n = len(self.editor.textCursor().block().text())
        c = self.editor.textCursor().positionInBlock()
        if (not cnt or ((self.completion_prefix == "" and n == 0) and is_navigation_key) or
                is_end_of_word or
                c < self._col or
                (int(event.modifiers()) and
                 event.key() == QtCore.Qt.Key_Backspace)):
            self._hide_popup()
        else:
            self._show_popup()

    def _on_key_released(self, event):
        if self._is_shortcut(event):
            return
        if (event.key() == QtCore.Qt.Key_Home or
            event.key() == QtCore.Qt.Key_End):
            return
        is_printable = self._is_printable_key_event(event)
        is_navigation_key = self._is_navigation_key(event)
        symbols = self._trigger_symbols
        is_end_of_word = self._is_end_of_word_char(
            event, is_printable, symbols, self.editor.word_separators)
        if self._completer.popup().isVisible():
            # Update completion prefix
            self._update_prefix(event, is_end_of_word, is_navigation_key)
        if is_printable:
            if event.text() == " ":
                self._cancel_next = self._request_cnt
            else:
                # trigger symbols
                if symbols:
                    cursor = self._helper.word_under_cursor()
                    cursor.setPosition(cursor.position())
                    cursor.movePosition(cursor.StartOfLine, cursor.KeepAnchor)
                    text_to_cursor = cursor.selectedText()
                    for symbol in symbols:
                        if text_to_cursor.endswith(symbol):
                            _logger().debug("cc: symbols trigger")
                            self._hide_popup()
                            self.request_completion()
                            return
                # trigger length
                if not self._completer.popup().isVisible():
                    prefix_len = len(self.completion_prefix)
                    if prefix_len >= self._trigger_len:
                        _logger().debug("cc: Len trigger")
                        self.request_completion()
                        return
            if self.completion_prefix == "":
                return self._hide_popup()

    def _on_selected_completion_changed(self, completion):
        self._current_completion = completion

    def _on_cursor_position_changed(self):
        current_line = TextHelper(self.editor).current_line_nbr()
        if current_line != self._cursor_line:
            self._cursor_line = current_line
            self._hide_popup()
            self._job_runner.cancel_requests()

    @QtCore.Slot()
    def _set_wait_cursor(self):
        self.editor.set_mouse_cursor(QtCore.Qt.WaitCursor)

    def _is_last_char_end_of_word(self):
        try:
            cursor = self._helper.word_under_cursor()
            cursor.setPosition(cursor.position())
            cursor.movePosition(cursor.StartOfLine, cursor.KeepAnchor)
            line = cursor.selectedText()
            last_char = line[len(line) - 1]
            if last_char != ' ':
                symbols = self._trigger_symbols
                seps = self.editor.word_separators
                return last_char in seps and last_char not in symbols
            return False
        except (IndexError, TypeError):
            return False

    def _show_completions(self, completions):
        _logger().debug("show %d completions" % len(completions))
        self._job_runner.cancel_requests()
        # user typed too fast: end of word char has been inserted
        if (not self._cancel_next and not self._is_last_char_end_of_word() and
                not (len(completions) == 1 and
                     completions[0]['name'] == self.completion_prefix)):
            # we can show the completer
            self._update_model(completions, self._completer.model())
            _logger().debug("model updated")
            self._show_popup()
            _logger().debug("popup shown")
        self._cancel_next = False

    def _handle_completer_events(self, event):
        # complete
        if (event.key() == QtCore.Qt.Key_Enter or
                event.key() == QtCore.Qt.Key_Return or
                event.key() == QtCore.Qt.Key_Tab):
            self._insert_completion(self._current_completion)
            self._hide_popup()
            event.accept()
        # hide
        elif (event.key() == QtCore.Qt.Key_Escape or
              event.key() == QtCore.Qt.Key_Backtab):
            self._hide_popup()
            event.accept()
        elif event.key() == QtCore.Qt.Key_Home:
            self._show_popup(index=0)
            event.accept()
        elif event.key() == QtCore.Qt.Key_End:
            self._show_popup(index=self._completer.completionCount() - 1)
            event.accept()

    def _hide_popup(self):
        # self.editor.viewport().setCursor(QtCore.Qt.IBeamCursor)
        if self._completer.popup() is not None:
            self._completer.popup().hide()
        self._job_runner.cancel_requests()
        QtWidgets.QToolTip.hideText()

    def _show_popup(self, index=0):
        cnt = self._completer.completionCount()
        full_prefix = self._helper.word_under_cursor(
            select_whole_word=True).selectedText()
        if (full_prefix == self._current_completion) and cnt == 1:
            self._hide_popup()
        else:
            if self._case_sensitive:
                self._completer.setCaseSensitivity(QtCore.Qt.CaseSensitive)
            else:
                self._completer.setCaseSensitivity(
                    QtCore.Qt.CaseInsensitive)
            # set prefix
            self._completer.setCompletionPrefix(self.completion_prefix)
            # compute size and pos
            cursor_rec = self.editor.cursorRect()
            char_width = self.editor.fontMetrics().width('A')
            prefix_len = (len(self.completion_prefix) * char_width)
            cursor_rec.translate(self.editor.panels.margin_size() - prefix_len,
                                 # top
                                 self.editor.panels.margin_size(0))
            popup = self._completer.popup()
            width = popup.verticalScrollBar().sizeHint().width()
            cursor_rec.setWidth(self._completer.popup().sizeHintForColumn(0) +
                                width)
            # show the completion list
            if self.editor.isVisible():
                if not self._completer.popup().isVisible():
                    self._on_focus_in(None)
                self._completer.complete(cursor_rec)
                self._completer.popup().setCurrentIndex(
                    self._completer.completionModel().index(index, 0))
            else:
                _logger().debug('cannot show popup, editor is unvisible')

    def _insert_completion(self, completion):
        cursor = self._helper.word_under_cursor(select_whole_word=False)
        cursor.insertText(completion)
        self.editor.setTextCursor(cursor)

    def _is_shortcut(self, event):
        """
        Checks if the event's key and modifiers make the completion shortcut
        (Ctrl+Space)

        :param event: QKeyEvent

        :return: bool
        """
        modifier = QtCore.Qt.MetaModifier if sys.platform == 'darwin' else QtCore.Qt.ControlModifier
        valid_modifier = int(event.modifiers() & modifier) == modifier
        valid_key = event.key() == self._trigger_key
        _logger().debug("CC: Valid Mofifier: %r, Valid Key: %r" % (valid_modifier, valid_key))
        return valid_key and valid_modifier

    @staticmethod
    def strip_control_characters(input_txt):
        if input_txt:
            # unicode invalid characters
            re_illegal = \
                '([\u0000-\u0008\u000b-\u000c\u000e-\u001f\ufffe-\uffff])' + \
                '|' + \
                '([%s-%s][^%s-%s])|([^%s-%s][%s-%s])|([%s-%s]$)|(^[%s-%s])' % \
                (chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff),
                 chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff),
                 chr(0xd800), chr(0xdbff), chr(0xdc00), chr(0xdfff))
            input_txt = re.sub(re_illegal, "", input_txt)
            # ascii control characters
            input_txt = re.sub(r"[\x01-\x1F\x7F]", "", input_txt)
        return input_txt

    @staticmethod
    def _is_printable_key_event(event):
        return len(CodeCompletionMode.strip_control_characters(
            event.text())) == 1

    @staticmethod
    @memoized
    def _make_icon(icon):
        return QtGui.QIcon(icon)

    def _update_model(self, completions, cc_model):
        """
        Creates a QStandardModel that holds the suggestion from the completion
        models for the QCompleter

        :param completionPrefix:
        """
        # build the completion model
        cc_model.clear()
        displayed_texts = []
        self._tooltips.clear()
        for completion in completions:
            name = completion['name']
            # skip redundant completion
            if (name and name != self.completion_prefix and
                    name not in displayed_texts):
                displayed_texts.append(name)
                item = QtGui.QStandardItem()
                item.setData(name, QtCore.Qt.DisplayRole)
                if 'tooltip' in completion and completion['tooltip']:
                    self._tooltips[name] = completion['tooltip']
                if 'icon' in completion:
                    item.setData(self._make_icon(completion['icon']),
                                 QtCore.Qt.DecorationRole)
                cc_model.appendRow(item)
        return cc_model

    def _display_completion_tooltip(self, completion):
        if not self._show_tooltips:
            return
        if completion not in self._tooltips:
            QtWidgets.QToolTip.hideText()
            return
        tooltip = self._tooltips[completion].strip()
        pos = self._completer.popup().pos()
        pos.setX(pos.x() + self._completer.popup().size().width())
        pos.setY(pos.y() - 15)
        QtWidgets.QToolTip.showText(pos, tooltip, self.editor)

    def _collect_completions(self, code, line, column, path, encoding,
                             completion_prefix):
        # pylint: disable=too-many-arguments
        _logger().debug("cc: completion requested")
        data = {'code': code, 'line': line, 'column': column,
                'path': path, 'encoding': encoding,
                'prefix': completion_prefix}
        try:
            self.editor.backend.send_request(
                backend.CodeCompletionWorker, args=data,
                on_receive=self._on_results_available)
        except NotConnected:
            self._data = data
            QtCore.QTimer.singleShot(100, self._retry_collect)
        else:
            self._set_wait_cursor()

    def _retry_collect(self):
        _logger().debug('retry work request')
        try:
            self.editor.backend.send_request(
                backend.CodeCompletionWorker, args=self._data,
                on_receive=self._on_results_available)
        except NotConnected:
            QtCore.QTimer.singleShot(100, self._retry_collect)
        else:
            self._set_wait_cursor()

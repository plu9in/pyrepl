#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import copy
import struct
import sys
import os
import atexit
import re
import time

try:
    import termios
except ImportError:
    pass
try:
    import fcntl
except ImportError:
    pass

HISTORY_MAX_LENGTH = 100
UNSUPPORTED_TERM = {"dumb", "cons25", "emacs"}
is_atexit_registered = False
origin_termios = None
rawmode = False
mlmode = False
maskmode = False
history = []
completion_callback = None
hints_callback = None
history_index = 0


class TTYState(object):
    __slots__ = [
        'ifd'  # Terminal stdin file descriptor.
        , 'ofd'  # Terminal stdout file descriptor.
        , 'buf'  # Edited line buffer.
        , 'prompt'  # Prompt to display.
        , 'pos'  # Current cursor position.
        , 'oldpos'  # Previous refresh cursor position.
        , 'cols'  # Number of columns in terminal.
        , 'maxrows'  # Maximum num of rows used so far (multiline mode)
        , 'history_index'  # The history index we are currently editing.
        , 'first'
    ]


class TTYCompletions(object):
    def __init__(self):
        self._cvec = []

    def len(self):
        return len(self._cvec)

    @property
    def cvec(self):
        return self._cvec

    def append(self, ln):
        self._cvec.append(ln)


class ABuf(object):
    __slots__ = [
        'b'
    ]

    def __init__(self):
        self.b = ''

    def ab_append(self, s):
        self.b = self.b + s

    def ab_free(self):
        self.b = None


class GotoException(Exception):
    pass


def is_unsupported_term():
    term = os.getenv('TERM')
    return term is None or term in UNSUPPORTED_TERM


def no_tty():
    return ''.join([line for line in sys.stdin]), True


def unsupported_term(prompt):
    write_flush(sys.stdout, '{}'.format(prompt))

    line = sys.stdin.readline()
    while line[-1] in ['\n', '\r']:
        line = line[:-1]
    return line, True


def disable_raw_mode(fd):
    global rawmode
    if rawmode:
        termios.tcsetattr(fd, termios.TCSAFLUSH, origin_termios)
    rawmode = 0


def tty_set_completion_callback(fn):
    global completion_callback
    completion_callback = fn


def tty_set_hints_callback(fn):
    global hints_callback
    hints_callback = fn


def tty_add_completion(struct, strg):
    struct.append(strg)
    return struct


def tty_exit():
    disable_raw_mode(sys.stdin)


def enable_raw_mode(fd):
    global is_atexit_registered, origin_termios, rawmode
    try:
        if not fd.isatty():
            raise GotoException()
        if not is_atexit_registered:
            atexit.register(tty_exit)
            is_atexit_registered = True
        origin_termios = termios.tcgetattr(fd)
        if not origin_termios:
            raise GotoException()
        raw = copy.deepcopy(origin_termios)
        [iflag, oflag, cflag, lflag, ispeed, ospeed, cc] = raw
        # input modes: no break, no CR to NL, no parity check, no strip char,
        # no start/stop output control.
        iflag &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
        oflag &= ~(termios.OPOST)  # output modes - disable post processing
        cflag |= (termios.CS8)  # control modes - set 8 bit chars
        # local modes - choing off, canonical off, no extended functions,
        # no signal chars (^Z,^C)
        lflag &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        # control chars - set return condition: min number of bytes and timer.
        # We want read to return every single byte, without timeout.
        cc[termios.VMIN] = 1
        cc[termios.VTIME] = 0  # 1 byte, no timer
        raw = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
        termios.tcsetattr(fd, termios.TCSAFLUSH, raw)
        rawmode = True
        return 0
    except GotoException:
        return -1


def get_cursor_position(ifd, ofd):
    if write_flush(ofd, "\x1b[6n") != 4:
        return -1
    buf = ""

    # Read the response: ESC [ rows ; cols R
    char = read_decode(ifd, 1)

    while char and char != 'R':
        buf = buf + char
        char = read_decode(ifd, 1)

    if buf == "":
        return -1

    if buf[0] != chr(27) or buf[1] != '[':
        return -1

    rows_cols = re.findall(r'\d+', buf)
    if len(rows_cols) != 2:
        return -1

    return rows_cols[1]


def write_flush(fd, buf):
    k = fd.write(buf)
    fd.flush()
    return k


def read_decode(fd, length):
    esc1 = os.read(fd.fileno(), length)
    return esc1.decode('utf8')


def get_columns(ifd, ofd):
    width = 0
    try:
        ioctl_ret = fcntl.ioctl(ofd, termios.TIOCGWINSZ, "\000" * 8)
        if ioctl_ret == 0:
            height, width = struct.unpack("hhhh", ioctl_ret)[0:2]

        if width == 0:
            start = get_cursor_position(ifd, ofd)
            if start == -1:
                raise GotoException()
            # Go to right margin and get position.
            if write_flush(ofd, "\x1b[999C") != 6:
                raise GotoException()

            width = get_cursor_position(ifd, ofd)
            if width == -1:
                raise GotoException()
            # Restore position
            if width > start:
                if write_flush(ofd, "\x1b[{}D".format(width - start)) != len("\x1b[{}D".format(width - start)):
                    raise GotoException()

    except GotoException():
        width = 80
    finally:
        return int(width)


def tty_edit_insert(state, char):
    global maskmode, mlmode, hints_callback

    if state.pos == len(state.buf):
        state.buf = state.buf + char
        state.pos = state.pos + 1
        if not mlmode \
                and len(state.prompt) + len(state.buf) < state.cols \
                and not hints_callback:

            d = '*' if maskmode else char
            if write_flush(state.ofd, d) == -1:
                return state, False
            else:
                return state, True
        else:
            refresh_line(state)
            return state, True
    else:
        state.buf = state.buf[:state.pos] + char + state.buf[state.pos:]
        state.pos = state.pos + 1
        refresh_line(state)
        return state, True


def tty_beep():
    write_flush(sys.stderr, '\x07')


def refresh_multi_lines(state):
    pass


def refresh_single_line(state):
    global maskmode

    length = len(state.buf)
    i = 0
    pos = state.pos
    while len(state.prompt) + pos >= state.cols:
        i = i + 1
        length = length - 1
        pos = pos - 1

    while len(state.prompt) + length > state.cols:
        length = length - 1

    ab = ABuf()
    ab.ab_append('\r')
    ab.ab_append(state.prompt)
    ab.ab_append("*" * length) if maskmode else ab.ab_append(state.buf[i:i + length])
    # refresh_show_hints(ab, state)
    ab.ab_append("\x1b[0K")
    ab.ab_append("\r\x1b[{}C".format(pos + len(state.prompt)))
    write_flush(state.ofd, ab.b)


def refresh_line(state):
    global mlmode

    if mlmode:
        refresh_multi_lines(state)
    else:
        refresh_single_line(state)


def complete_line(state):
    global completion_callback

    tty_completions = TTYCompletions()
    char = True

    if len(state.buf) == 0:
        tty_beep()
        return True

    # We have called this function because completion_callback was not None
    tty_completions = completion_callback(state.buf, tty_completions)

    if len(tty_completions.cvec) == 0 or len(state.buf) == 0:
        tty_beep()
        return True
    else:
        stop, i = False, 0
        while not stop:
            if i < len(tty_completions.cvec):
                saved = TTYState()
                saved.buf = state.buf
                saved.pos = state.pos

                state.buf = tty_completions.cvec[i]
                state.pos = len(tty_completions.cvec[i])
                refresh_line(state)
                state.buf = saved.buf
                state.pos = saved.pos
            else:
                refresh_line(state)
            char = read_decode(state.ifd, 1)
            if not char:
                return ''
            if char == chr(9):  # TAB
                i = (i + 1) % (tty_completions.len() + 1)
                if i == tty_completions.len():
                    tty_beep()
            elif char == chr(27):  # ESC / show original buffer
                if i < tty_completions.len():
                    refresh_line(state)
                stop = True
            else:
                if i < tty_completions.len():
                    state.buf = tty_completions.cvec[i]
                    state.pos = len(state.buf)
                stop = True
    return char


def tty_edit_move_left(state):
    if state.pos > 0:
        state.pos = state.pos - 1
        refresh_line(state)
    return state, True


def tty_edit_move_right(state):
    if state.pos != len(state.buf):
        state.pos = state.pos + 1
        refresh_line(state)
    return state, True


def tty_edit_backspace(state):
    if state.pos > 0 and len(state.buf) > 0:
        state.buf = state.buf[:state.pos - 1] + state.buf[state.pos:]
        state.pos = state.pos - 1
        refresh_line(state)
    return state, True


def tty_edit_delete(state):
    length = len(state.buf)
    if length > 0 and length > state.pos:
        state.buf = state.buf[:state.pos] + state.buf[state.pos + 1:]
        refresh_line(state)
    return state, True


def tty_edit_move_home(state):
    if state.pos > 0:
        state.pos = 0
        refresh_line(state)
    return state, True


def tty_edit_move_end(state):
    if state.pos != len(state.buf):
        state.pos = len(state.buf)
        refresh_line(state)
    return state, True


def tty_swap_current_with_previous_character(state):
    if 0 < state.pos < len(state.buf):
        char1 = state.buf[state.pos - 1]
        char2 = state.buf[state.pos]
        state.buf = state.buf[:state.pos - 1] + char2 + char1 + state.buf[state.pos + 1:]
        if state.pos != len(state.buf) - 1:
            state.pos = state.pos + 1
        refresh_line(state)
    return state, True


def tty_delete_whole_line(state):
    state.buf = ''
    state.pos = 0
    refresh_line(state)
    return state, True


def tty_delete_from_position_to_end(state):
    state.buf = state.buf[:state.pos]
    refresh_line(state)
    return state, True


def tty_clear_screen(state):
    write_flush(state.ofd, "\x1b[H\x1b[2J")
    refresh_line(state)
    return state, True


def tty_edit_delete_previous_word(state):
    old = state.pos
    while state.pos > 0 and state.buf[state.pos - 1] == ' ':
        state.pos = state.pos - 1
    while state.pos > 0 and state.buf[state.pos - 1] != ' ':
        state.pos = state.pos - 1
    state.buf = state.buf[:state.pos] + state.buf[old:]
    refresh_line(state)
    return state, True


def tty_edit_history_next(state, previous):
    global history, history_index

    if len(history) > 1:
        if not previous:
            if history_index > 0:
                history_index = history_index - 1
            else:
                history_index = 0
        else:
            if history_index < len(history) - 1:
                history_index = history_index + 1
            else:
                history_index = len(history) - 1

        state.buf = history[history_index]
        state.pos = len(state.buf)
        refresh_line(state)
    return state, True


def tty_add_history(line):
    global history

    if len(history) > 0 and history[0] == line:
        return True

    if len(history) == HISTORY_MAX_LENGTH:
        history = [line] + history[1:]
    else:
        history = [line] + history
    return True


def tty_edit(stdin_fd, stdout_fd, buf, prompt):
    global completion_callback, history, mlmode
    state = TTYState()
    state.ifd = stdin_fd
    state.ofd = stdout_fd
    state.buf = buf
    state.prompt = prompt
    state.oldpos = state.pos = 0
    state.cols = get_columns(stdin_fd, stdout_fd)
    state.maxrows = 0
    state.history_index = 0
    state.first = True

    tty_add_history('')
    write_flush(sys.stdout, u"\u001b[1000D")
    if write_flush(stdout_fd, prompt) != len(prompt):
        return '', False

    while True:
        char = read_decode(stdin_fd, 1)
        if len(char) <= 0:
            return '', False

        if char == chr(9) and completion_callback:
            char = complete_line(state)
            if not char:
                return '', True
            if char:
                continue
        elif char == chr(13):  # ENTER
            if mlmode:
                state, result = tty_edit_move_end(state)
            if hints_callback:
                pass
            state.first = True
            history = history[1:]
            tty_add_history(state.buf)
            return '', True
        elif char == chr(3):  # CTRL+C
            disable_raw_mode(sys.stdin)
            exit(0)
        elif char == chr(127) or char == chr(8):  # BACKSPACE / CTRL+H
            state, result = tty_edit_backspace(state)
        elif char == chr(4):  # CTRL+D
            if len(state.buf) > 0:
                state, result = tty_edit_delete(state)
            else:
                return '', False
        elif char == chr(20):  # CTRL+T
            state, result = tty_swap_current_with_previous_character(state)
        elif char == chr(2):  # CTRL+B
            state, result = tty_edit_move_left(state)
        elif char == chr(6):  # CTRL+F
            state, result = tty_edit_move_right(state)
        elif char == chr(16):  # CTRL+P
            state, result = tty_edit_history_next(state, previous=True)
        elif char == chr(14):  # CTRL+N
            state, result = tty_edit_history_next(state, previous=False)
        elif char == chr(27):  # ESC
            esc1 = read_decode(state.ifd, 1)
            esc2 = read_decode(state.ifd, 1)
            if esc1 == '[':
                if '0' <= esc2 <= '9':
                    esc3 = read_decode(state.ifd, 1)
                    if esc3 == '~':
                        if esc1 == '3':
                            state, result = tty_edit_delete(state)
                elif esc2 == 'A':  # UP
                    if state.first:
                        state.first = False
                        history[0] = state.buf
                    state, result = tty_edit_history_next(state, previous=True)
                elif esc2 == 'B':  # DOWN
                    if state.first:
                        state.first = False
                        history[0] = state.buf
                    state, result = tty_edit_history_next(state, previous=False)
                elif esc2 == 'C':  # Right
                    state, result = tty_edit_move_right(state)
                elif esc2 == 'D':  # Left
                    state, result = tty_edit_move_left(state)
                elif esc2 == 'H':  # Home
                    state, result = tty_edit_move_home(state)
                elif esc2 == 'F':  # End
                    state, result = tty_edit_move_end(state)
            elif esc1 == 'O':
                if esc2 == 'H':
                    state, result = tty_edit_move_home(state)
                elif esc2 == 'F':
                    state, result = tty_edit_move_end(state)
        elif char == chr(21):  # CTRL+U
            state, result = tty_delete_whole_line(state)
        elif char == chr(11):  # CTRL+K
            state, result = tty_delete_from_position_to_end(state)
        elif char == chr(1):  # CTRL+A
            state, result = tty_edit_move_home(state)
        elif char == chr(5):  # CTRL+E
            state, result = tty_edit_move_end(state)
        elif char == chr(12):  # CTRL+L
            state, result = tty_clear_screen(state)
        elif char == chr(23):  # CTRL+W
            state, result = tty_edit_delete_previous_word(state)
        else:
            state.first = True
            state, result = tty_edit_insert(state, char)
            if not result:
                return state.buf, result


def tty_raw(prompt):
    buf = ""
    if enable_raw_mode(sys.stdin) == -1:
        return buf, False
    buf, result = tty_edit(sys.stdin, sys.stdout, buf, prompt)
    disable_raw_mode(sys.stdin)
    return buf, result


def command_line(prompt=''):
    if not sys.stdin.isatty():
        return no_tty()
    elif is_unsupported_term():
        return unsupported_term(prompt)
    else:
        return tty_raw(prompt)


if __name__ == '__main__':
    prompt = "$> "
    line, result = command_line(prompt)
    while line is not None and result:
        print("{}".format(line))
        line, result = command_line(prompt)
    # try:
    #  command_line('> ')
    # finally:
    #  fd = sys.stdin.fileno()
    #  old_settings = termios.tcgetattr(fd)
    #  termios.tcsetattr(fd, termios.TCSAFLUSH, old_settings)
    #  sys.stdout.flush()

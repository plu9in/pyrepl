#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from repl import *


def completion(buffer, tty_completions):
    if buffer[0] == 'h':
        tty_add_completion(tty_completions, "hello")
        tty_add_completion(tty_completions, "hello there !")
    return tty_completions


if __name__ == '__main__':
    tty_set_completion_callback(completion)
    prompt = "$> "
    line, result = command_line(prompt)
    while line is not None and result:
        print("{}".format(line))
        line, result = command_line(prompt)
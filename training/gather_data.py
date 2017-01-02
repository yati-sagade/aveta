# coding: utf-8
"""
gather_data.py

Usage:
    python gather_data.py RAW_DATA_DIR OUTPUT_DIR

RAW_DATA_DIR must have a specific structure, like the one generated by
aveta-bastion's movedata.pl (github.com/yati-sagade/aveta-bastion)

An example:

    $ ls ~/aveta-training-data-final
    /home/ys/aveta-training-data-final/
    ├── cochlea
    │   ├── 0
    │   │   ├── commands.txt
    │   │   ├── original-name
    │   │   ├── sync.txt
    │   │   └── video.avi
    │   └── 1
    │       ├── commands.txt
    │       ├── original-name
    │       ├── sync.txt
    │       └── video.avi
    └── simple
        ├── 0
        │   ├── commands.txt
        │   ├── original-name
        │   ├── sync.txt
        │   └── video.avi
        ├── 1
        │   ├── commands.txt
        │   ├── original-name
        │   ├── sync.txt
        │   └── video.avi
        ├── 2
        │   ├── commands.txt
        │   ├── original-name
        │   ├── sync.txt
        │   └── video.avi
        ├── 3
        │   ├── commands.txt
        │   ├── original-name
        │   ├── sync.txt
        │   └── video.avi
        └── 4
            ├── commands.txt
            ├── original-name
            ├── sync.txt
            └── video.avi

OUTPUT
------

The output directory contains a subdirectory each for the 7 commands (including
nop, code=0). Each of the command subdirectories contains frames at which the
user gave the command in question. There's also a file called speeds.txt in each
of the command subdirectories, which contains <frame-filename,speedleft,speedright>
lines. E.g., the entry ('foo.jpeg', 23, 42) means that the speeds of the left
and the right wheels when the frame in foo.jpeg was recorded was 23 and 42,
respectively. The speed values range from -255 to 255 inclusive, and are
translated to actual torque by the Adafruit motor hat library for raspberry pi.

"""
import re
import cv2
import sys
import argparse
import os
from collections import defaultdict
from itertools import izip, chain
try:
    import cPickle as pkl
except ImportError:
    import pickle as pkl


import numpy as np

from common import (command_mapping, command_rev_mapping,
                    command_readable_mapping)


def main(input_dir, output_dir, verbose, frame_size=(100,100)):
    if not os.path.exists(input_dir) or not os.path.isdir(input_dir):
        print("{} does not name a directory.".format(input_dir))
        return 1

    if os.path.exists(output_dir) and not os.path.isdir(output_dir):
        print("{} does not name a directory.".format(output_dir))
        return 1
    elif not os.path.exists(output_dir):
        os.makedirs(output_dir)

    mappings = []
    for tagname in os.listdir(input_dir):
        if tagname.startswith("."):
            continue
        tagdir = os.path.join(input_dir, tagname)
        if not os.path.isdir(tagdir):
            continue
        _process_tagdir(tagdir, output_dir, frame_size)
    return 0

def _process_tagdir(dirname, output_dir, frame_size):
    """Process a single tagdir."""
    mappings = []
    for idx in os.listdir(dirname):
        datadir = os.path.join(dirname, idx)
        if not idx.isdigit() or not os.path.isdir(datadir):
            continue
        vidfile, syncfile, cmdfile = [
            os.path.join(datadir, fname)
            for fname in ("video.avi", "sync.txt", "commands.txt")
        ]
        _process_files(vidfile, syncfile, cmdfile, output_dir, frame_size)


def _get_next_usable_integer_index(dirname, extn):
    pat = re.compile(r'^(\d+)\.{}$'.format(extn))
    max_num = -1
    for name in os.listdir(dirname):
        m = pat.match(name)
        if m is not None:
            max_num = max(int(m.groups()[0]), max_num)
    return max_num + 1


class DataWriteHelper(object):
    def __init__(self, command_code, output_dir):
        self._cmd_outdir = os.path.join(output_dir, str(command_code))

        if not os.path.exists(self._cmd_outdir):
            os.makedirs(self._cmd_outdir)

        self._speedsfile = open(os.path.join(self._cmd_outdir, "speeds.txt"),
                                "a+b")
        self._next_idx = _get_next_usable_integer_index(self._cmd_outdir, "jpeg")

    def write(self, frame, left_speed, right_speed):
        filename = "{}.jpeg".format(self._next_idx)
        self._next_idx += 1
        path = os.path.join(self._cmd_outdir, filename)
        cv2.imwrite(path, frame)
        self._speedsfile.write("{},{},{}\n".format(filename, left_speed,
                                                   right_speed))
        self._speedsfile.flush()

    def close(self):
        try:
            self._speedsfile.close()
        except:
            pass


def _process_files(video_filename, sync_filename, cmd_filename, output_dir, frame_size):
    """Process a single set of files datafiles. `output_dir` is populated with
    actual stuff here."""
    sync = _read_file(sync_filename, value_mapper=lambda vs: (int(vs[0]),))
    cmds = _read_file(cmd_filename)
    frames = izip(weighted_iter(sync), _video_frame_iter(video_filename))

    writers = {command: DataWriteHelper(command, output_dir)
               for command in range(len(command_mapping))}

    try:
        for frame, command, left_speed, right_speed in _cmd_frame_iter(
            frames, iter(cmds)
        ):
            out_frame = cv2.resize(frame, frame_size,
                                   interpolation=cv2.INTER_AREA)
            cmd_code = command_mapping[command]
            writers[cmd_code].write(out_frame, left_speed, right_speed)
    finally:
        for w in writers.values():
            w.close()


def _video_frame_iter(video_filename):
    cap = cv2.VideoCapture(video_filename)
    if cap is None:
        raise Exception("Could not read video {}".format(video_filename))
    while cap.isOpened():
        flag, frame = cap.read()
        if not flag:
            break
        yield cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _cmd_frame_iter(frames, cmds):
    """Match timestamped frames and commands.

    Args:
        frames: An iterator yielding (str, numpy.ndarray), or timestamped
        video frames.

        cmds: An iterator yielding (str, str, str, str), or timestamped commands.

    Returns:
        An iterator yielding (numpy.array, str), for a video frame and the
        (best-effort) matching command.

    NOTE: Command can be None when there was no command seen for a given frame.
    """
    # NOTE 1: This assignment of "no command" to certain frames is incorrect,
    # strictly speaking, since frames are classified "no command" only when
    # the command stream for a given second has been exhausted.
    def _next_frame(it):
        t, x = next(it)
        return (int(float(t)), x)

    def _next_cmd(it):
        t, c, l, r = next(it)
        return (int(float(t)), c, float(l), float(r))

    ret = []
    read_frame, read_cmd = True, True
    while True:
        if read_frame:
            frame_time, frame = _next_frame(frames)
        if read_cmd:
            cmd_time, cmd, left_speed, right_speed = _next_cmd(cmds)

        if cmd_time < frame_time:
            # Drop this command
            read_cmd, read_frame = True, False
            continue
        elif cmd_time > frame_time:
            read_cmd, read_frame = False, True
            # No command; see `NOTE 1` above.
            yield frame, None, left_speed, right_speed
        else:
            read_frame, read_cmd = True, True
            yield frame, cmd, left_speed, right_speed

    for _, frame in frames:
        yield frame, None, left_speed, right_speed


def _read_file(filename, value_mapper=lambda x: x):
    """Read a file with comma separated <key,val1,val2...> lines.

    Returns a list of (key, val1, val2..) tuples sorted by the key ascending. If
    `value_mapper` is given, it is passed a tuple of length `num_fields-1`
    containing the `num_fields-1` values on line.
    """
    retmap = {}
    with open(filename) as fp:
        for line in fp:
            split_line = line.strip().split(",")
            key = split_line[0]
            vals = tuple(split_line[1:])
            # For command files, there *might* be multiple commands per tick, but
            # we'll just pick the last one in the file.
            retmap[key] = value_mapper(vals)

    for key, vals in sorted(retmap.items(), key=lambda t: t[0]):
        yield tuple(chain([key], vals))



def weighted_iter(buckets):
    """Given a sequence of (key, count) items, return an iterator yielding keys in agreement with the counts."""
    for key, count in buckets:
        for _ in xrange(count):
            yield key


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_dir", help="Input directory")
    parser.add_argument("output_dir", help="Output directory")
    parser.add_argument("--frame_size", type=str, default="100x100")
    parser.add_argument("--verbose", action="store_true", help="Give verbose output")
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        frame_w, frame_h = map(int, args.frame_size.split("x"))
    except:
        print("--frame_size must be of the form 'MxN', where M, N are both integers.")
        sys.exit(1)
    sys.exit(main(args.input_dir, args.output_dir, args.verbose,
                  (frame_w, frame_h)))

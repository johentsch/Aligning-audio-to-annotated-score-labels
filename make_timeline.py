#!/usr/bin/env python
# coding: utf-8
"""This script (currently) processes notes TSV files that have been aligned using the aligner.py script
and turns them into symbolic timelines in the sense of measure/beat grid.

To be precise, it reads a notes TSV file extracted from a MuseScore using the ms3 parser, to which aligner.py has
added a "start" column in real time, according to the chosen recording. This script discards the notes that do not
fall on a downbeat, maintaining those positions where on or several notes fall on a downbeat.

Degrees of freedom
------------------

* Downbeats are computed using a fairly standard inference of metrical beats based on measure-wise quarternote offsets
  and the time signature in question (see :func:`onset2beat`). This could be adapted to other criteria.
* Downbeats on which no note occurs are not included in the timeline and (probably) need to be filled in/guessed
  (e.g., using a linear fill).
* Several notes co-occurring on the same downbeat may have different timestamps (as inherent to the recording or as an
    algorithmic artefact). There are several ways of inferring a definite position in these cases:

    - average of the timestamps
        - Gaussian
    - linear fill between preceding and consequent downbeats
        - linear fill but weighted by the timestamps
"""

import argparse
import os
from fractions import Fraction
from functools import cache
from typing import Optional, Literal, overload

import ms3
import numpy as np
import pandas as pd

from utils import get_quarterbeats_column_name


def resolve_dir(d):
    """Resolves '~' to HOME directory and turns ``d`` into an absolute path.
    Copied from ms3 v2.5.3
    """
    if d is None:
        return None
    d = str(d)
    if "~" in d:
        return os.path.expanduser(d)
    return os.path.abspath(d)


def check_dir(d):
    """Copied from ms3 v2.5.3"""
    if not os.path.isdir(d):
        d = resolve_dir(os.path.join(os.getcwd(), d))
        if not os.path.isdir(d):
            raise argparse.ArgumentTypeError(d + " needs to be an existing directory")
    return resolve_dir(d)

def recurse_directory(rootdir, targetdir, recursive=True, preview=False):
    for subdir, dirs, files in os.walk(rootdir):
        print(rootdir, targetdir)
        if not recursive:
            dirs[:] = []
        else:
            dirs.sort()
        for file in sorted(files):
            if not file.endswith(".notes.tsv"):
                continue
            filepath = os.path.join(subdir, file)
            rel_dir = os.path.relpath(subdir, rootdir)
            target_subdir = os.path.join(targetdir, rel_dir)
            main(aligned_notes=filepath, targetdir=target_subdir, preview=preview)


def _ts_beat_size(numerator: int, denominator: int) -> Fraction:
    beat_numerator = 3 if numerator % 3 == 0 and numerator > 3 else 1
    result = Fraction(beat_numerator, denominator)
    return result

@cache
def ts_beat_size(ts: str) -> Fraction:
    """Pass a time signature to get the beat size which is based on the fraction's
    denominator ('2/2' => 1/2, '4/4' => 1/4, '4/8' => 1/8). If the nominator is
    a higher multiple of 3, the threefold beat size is returned
    ('12/8' => 3/8, '6/4' => 3/4).
    """
    numerator, denominator = str(ts).split("/")
    result = _ts_beat_size(int(numerator), int(denominator))
    return result

@overload
def onset2beat(
        onset: Fraction, timesig: str, beat_decimals: Literal[None], first_beat: float | int
) -> Fraction: ...


@overload
def onset2beat(onset: Fraction, timesig: str, beat_decimals: int, first_beat: float | int) -> float: ...



@cache
def onset2beat(
    onset: Fraction,
    timesig: str,
    beat_decimals: Optional[int] = None,
    first_beat: float | int = 1,
) -> float | Fraction:
    """Turn an offset in whole notes into a beat based on the time signature.
        Uses: ts_beat_size()

    Args:
        onset:
            Offset from the measure's beginning as fraction of a whole note.
        timesig:
            Time signature, i.e., a string representing a fraction.
        beat_decimals:
            If None (default) the beat is returned as Fraction, otherwise as float rounded to beat_decimals decimals.
        first_beat:
            Value of each measure's first beat, defaulting to 1. All other beats are incremented accordingly.
    """
    size = ts_beat_size(timesig)
    beat, remainder = divmod(onset, size)
    subbeat = remainder / size
    result = beat + first_beat + subbeat
    return result if beat_decimals is None else round(float(result), beat_decimals)

def float_is_integer(f: float) -> bool:
    try:
        return f.is_integer()
    except Exception:
        print(f"Unable to evaluate whether {f!r} is an integer.")
        return False


def timesig2n_beats(timesig: "str") -> int:
    timesig_frac = Fraction(timesig)
    result = float(onset2beat(timesig_frac, timesig, first_beat=0))
    if not result.is_integer():
        raise ValueError(f"Timesig {timesig!r} resulted in a non-integer number of beats: {result}")
    return int(result)


def interpolate_missing_beats(tl: pd.DataFrame):
    mn_column = "mn_playthrough" if "mn_playthrough" in tl.columns else "mn"
    gpb = tl.groupby(mn_column, sort=False) # this is important for mn_playthrough which are strings that would get
    # sorted lexigraphically, giving wrong results. It is therefore important that the timeline comes correctly sorted
    if ((n_timesigs := gpb.timesig.nunique()) > 1).any():
        faulty = n_timesigs[n_timesigs > 1]
        raise ValueError(f"Multiple timesigs found:\n{faulty}")
    mn2timesig = gpb.timesig.unique().map(lambda x: x[0])
    mn2n_beats_timesig = mn2timesig.map(timesig2n_beats)
    mn2present_beats = gpb.beat.unique().map(set)
    mn2n_present_beats = mn2present_beats.map(len)
    mn2max_present_beat = mn2present_beats.map(max)
    mn2n_expected_beats = np.maximum(mn2n_beats_timesig, mn2max_present_beat).rename("beats")
    if mn_column == "mn":
        # do not fill anacrusis measure 0
        anacrusis_mask =  mn2n_expected_beats.index == 0
    else:
        # do not fill anacrusis measure (such as "0a", "0b", etc.)
        anacrusis_mask = mn2n_expected_beats.index.str.startswith("0")
    if anacrusis_mask.any():
        mn2n_expected_beats[anacrusis_mask] = 0
    mn2n_missing_beats = mn2n_expected_beats - mn2n_present_beats
    to_be_corrected_mask = mn2n_missing_beats > 0
    if not to_be_corrected_mask.any():
        return tl
    mn2complete_beatgrid = mn2n_expected_beats.map(lambda n: set(range(1, n + 1))).explode().rename(
        "beat"
        ).reset_index() # df with columns <mn_column> and "beat"
    merge_columns = mn2complete_beatgrid.columns.tolist()
    result = pd.merge(mn2complete_beatgrid, tl, how="left", on=merge_columns)
    result.start = result.start.interpolate(method='linear')
    return result


def aligned_notes2timeline(notes: pd.DataFrame) -> pd.DataFrame:
    beat_float = ms3.transform(
        notes,
        onset2beat,
        ["mn_onset", "timesig"],
        first_beat=1,
        beat_decimals=3,
    )
    downbeat_mask = beat_float.map(float_is_integer)
    downbeat = beat_float.where(downbeat_mask, 0).astype("Int64").rename("beat")
    notes = pd.concat([notes, downbeat], axis=1)
    drop_columns = (
        "duration_qb", "staff", "voice", "duration", "nominal_duration", "scalar", "tied", "tpc", "midi", "name",
        "octave", "chord_id", "end"
    )
    keep_columns = [col for col in notes.columns if col not in drop_columns]
    mn_column = "mn_playthrough" if "mn_playthrough" in notes.columns else "mn"  # playthrough -> expanded repeats
    result = notes.loc[downbeat_mask, keep_columns].drop_duplicates(subset=[mn_column, "beat"])
    return interpolate_missing_beats(result)


def aligned_beats2tilia_format(aligned_beats, minimal_column_set=False):
    if "mn_playthrough" in aligned_beats.columns:
        measure = aligned_beats.mn_playthrough.str.extract("^(\d+)", expand=False).astype("Int64")
    else:
        measure = aligned_beats.mn

    start = aligned_beats.start
    if isinstance(start, pd.DataFrame):
        raise ValueError("There are more than one 'start' columns, so I don't know which one to use.")
    data = dict(
        time=start,
        measure=measure,
        is_first_in_measure=(aligned_beats.beat == 1)
    )
    if not minimal_column_set:
        data["beat"] = aligned_beats.beat
        if "mn_playthrough" in aligned_beats.columns:
            data["mn_playthrough"] = aligned_beats.mn_playthrough
    result = pd.DataFrame(
        data
    ).reset_index(drop=True)
    return result.sort_values("time")


def aligned_notes2tilia_format(notes):
    aligned_beats = aligned_notes2timeline(notes)
    return aligned_beats2tilia_format(aligned_beats)

def aligned_notes2qb_warp_map(aligned_notes):
    qb_column = get_quarterbeats_column_name(aligned_notes) # qb_playthrough => expanded repeats
    start_instants = aligned_notes.set_index(qb_column).start
    end_instants = aligned_notes.end.copy()
    end_instants.index = aligned_notes[qb_column] + (aligned_notes.duration * 4)
    unique_start_instants = start_instants[~start_instants.index.duplicated(keep='first')]
    unique_end_instants = end_instants[~end_instants.index.duplicated(keep='first')]
    only_in_ends = unique_end_instants.index.difference(unique_start_instants.index)
    if len(only_in_ends) == 0:
        warp_map_values = unique_start_instants  # will be copied upon renaming
    else:
        warp_map_values = pd.concat([unique_start_instants, unique_end_instants.loc[only_in_ends]])
    warp_map_values = warp_map_values.sort_index().reset_index().set_axis([qb_column, "seconds"], axis=1)
    return warp_map_values

def aligned_notes_tsv2timeline(aligned_notes_tsv: str) -> pd.DataFrame:
    """Not used, kept for completeness."""
    notes = ms3.load_tsv(aligned_notes_tsv)
    return aligned_notes2timeline(notes)


def aligned_notes_tsv2tilia_format(aligned_notes_tsv: str) -> pd.DataFrame:
    notes = ms3.load_tsv(aligned_notes_tsv)
    return aligned_notes2tilia_format(notes)



def main(
        aligned_notes: str,
        targetdir: str,
        preview=False
):
    try:
        df = aligned_notes_tsv2tilia_format(aligned_notes)
    except (AttributeError, KeyError) as e:
        print(f"Converting {aligned_notes!r} to tilia format failed with error {e!r}.")
        return
    file = os.path.basename(aligned_notes)
    fname, fext = os.path.splitext(file)
    new_path = os.path.join(targetdir, f"{fname}.csv")
    if preview:
        print(new_path)
        print(df.head().to_string())
    else:
        os.makedirs(targetdir, exist_ok=True)
        df.to_csv(new_path, index=False)





def parse_args():
    parser = argparse.ArgumentParser(
        description="Rename files from scheme op3n2-04.mscx to op03n02d.mscx , disregarding the file ending.")
    parser.add_argument('src', metavar='SRC', nargs='?', type=resolve_dir, default=os.getcwd(),
                        help='Path to a .beats file or a folder containing .beats files. '
                             'Defaults to the current working directory.')
    parser.add_argument('-o', '--output', metavar='DIR', nargs='?', type=resolve_dir, default=os.getcwd(),
                        help='(optional) path to output folder; default is dir')
    parser.add_argument('-r', '--recursive', action='store_true', help='Also rename files in subdirectories.')
    parser.add_argument('-p', '--preview', action='store_true', help='Only preview, don\'t rename.')
    args = parser.parse_args()
    return args

def run():
    args = parse_args()
    if os.path.isdir(args.src):
        recurse_directory(
        rootdir=args.src,
        targetdir=args.output,
        recursive=args.recursive,
        preview=args.preview
    )
    elif os.path.isfile(args.src):
        main(
            aligned_notes=args.src,
            targetdir=args.output,
            preview=args.preview
        )
    else:
        raise FileNotFoundError(args.src)

if __name__ == '__main__':
    run()


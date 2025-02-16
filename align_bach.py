import os
import re
from typing import Literal

import pandas as pd

from utils import align_notes_labels_audio


def resolve_dir(d):
    """Resolves '~' to HOME directory and turns ``d`` into an absolute path."""
    if d is None:
        return None
    d = str(d)
    if "~" in d:
        return os.path.expanduser(d)
    return os.path.abspath(d)


def get_audio_filenames(audio_path):
    files = pd.DataFrame({"audio_filename": [f for f in os.listdir(audio_path) if f.endswith(".wav")]})
    track_info = files.audio_filename.str.extract(r"^0(?P<cd>\d)-(?P<track>\d+)", expand=True)
    files = pd.concat([track_info.astype(int), files], axis=1)
    return files


def get_notes_filenames(notes_path):
    regex = r"^B(\d+)_unfolded.notes.tsv$"
    files = sorted(
        (int(match.group(1)), match.group(0))
        for f in os.listdir(notes_path)
        if (match := re.match(regex, f))
    )
    files = pd.DataFrame(files, columns=["chorale", "notes_filename"]).set_index("chorale").notes_filename
    return files


def make_file_mapping(audio_path, mapping_tsv_file, notes_path):
    mapping = pd.read_csv(mapping_tsv_file, sep="\t", index_col="chorale")
    audio_files = get_audio_filenames(audio_path)
    notes_files = get_notes_filenames(notes_path)
    mapping = mapping.join(notes_files, how="left")
    mapping = pd.merge(mapping, audio_files, how="left", on=["cd", "track"])
    mapping.index = notes_files.index
    return mapping


def main(
        audio_path,
        notes_path,
        mapping_tsv_file,
        output_folder=None,
        mode: Literal["extended", "compact"] = "extended"
):
    audio_path = resolve_dir(audio_path)
    notes_path = resolve_dir(notes_path)
    if output_folder:
        output_folder = resolve_dir(output_folder)
    mapping = make_file_mapping(audio_path, mapping_tsv_file, notes_path)
    for no, _, _, notes_filename, audio_filename in mapping.itertuples():
        audio_filepath = os.path.join(audio_path, audio_filename)
        notes_filepath = os.path.join(notes_path, notes_filename)
        align_notes_labels_audio(
            audio_path=audio_filepath,
            notes_path=notes_filepath,
            labels_path=None,
            store=True,
            store_path=output_folder if output_folder else notes_filepath,
            verbose=True,
            visualize=False,
            evaluate=False,
            mode=mode
        )


def run():
    audio_path = "/mnt/DATA/Music/Choral Classics_ Bach (Chorales)/"
    notes_path = "~/git/Chorale-Corpus/data/Bach_JS/389_chorale_settings/vocal_parts_only/note_alignments/"
    mapping_tsv_file = "chorale2track.tsv"
    output_folder = None  # overwrite notes files
    mode = "extended"
    main(
        audio_path=audio_path,
        notes_path=notes_path,
        mapping_tsv_file=mapping_tsv_file,
        output_folder=output_folder,
        mode=mode
    )


if __name__ == "__main__":
    run()

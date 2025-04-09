import os
import re
from typing import Literal, Optional

import pandas as pd


def resolve_dir(d):
    """Resolves '~' to HOME directory and turns ``d`` into an absolute path."""
    if d is None:
        return None
    d = str(d)
    if "~" in d:
        return os.path.expanduser(d)
    return os.path.abspath(d)


def get_audio_filenames(
        audio_path,
        extension=".wav"
):
    audio_files = sorted(f for f in os.listdir(audio_path) if f.endswith(extension))
    return audio_files


def get_notes_filenames(notes_path):
    regex = r"^(.+)_unfolded.notes.tsv$"
    files = sorted(
        (match.group(1), match.group(0))
        for f in os.listdir(notes_path)
        if (match := re.match(regex, f))
    )
    column_names = ["ID", "notes_filename"]
    files = pd.DataFrame(files, columns=column_names).set_index(column_names[0]).notes_filename
    return files


def make_file_mapping(audio_path, notes_path):
    notes_files = get_notes_filenames(notes_path)
    notes_ids = sorted(notes_files.index)
    audio_files = get_audio_filenames(audio_path)
    mapping = {}
    for audio_file in audio_files:
        for i, notes_id in enumerate(notes_ids):
            if not audio_file.startswith(notes_id):
                continue
            # this is a match
            mapping[notes_id] = audio_file
            notes_ids = notes_ids[i+1:]  # since this is a match, all future matches will come after this position
            break  # continue with the next audio file;
        else:
            print(f"{audio_file}: No ID found in {notes_ids}")
    mapping = pd.DataFrame(dict(
        ID = mapping.keys(),
        audio_filename = mapping.values()
    )).set_index("ID")
    mapping = mapping.join(notes_files, how="left")
    return mapping


def main(
        audio_path: str,
        notes_path: str,
        output_folder: Optional[str] = None,
        mode: Literal["extended", "compact"] = "extended"
):
    audio_path = resolve_dir(audio_path)
    notes_path = resolve_dir(notes_path)
    if output_folder:
        output_folder = resolve_dir(output_folder)
    mapping = make_file_mapping(audio_path, notes_path)
    mapping.audio_filename = mapping.audio_filename.apply(lambda x: os.path.join(audio_path, x))
    mapping.notes_filename = mapping.notes_filename.apply(lambda x: os.path.join(notes_path, x))
    mapping = mapping.rename(columns=dict(audio_filename="audio", notes_filename="notes")).reset_index(names="name")
    mapping.to_csv("bach_batch.csv", index=False)
    # for row in mapping.itertuples():
    #     audio_filepath = os.path.join(audio_path, row.audio_filename)
    #     notes_filepath = os.path.join(notes_path, row.notes_filename)
    #     align_notes_labels_audio(
    #         audio_path=audio_filepath,
    #         notes_path=notes_filepath,
    #         labels_path=None,
    #         store=True,
    #         store_path=output_folder if output_folder else notes_filepath,
    #         verbose=True,
    #         visualize=False,
    #         evaluate=False,
    #         mode=mode
    #     )


def run():
    audio_path = "~/git/annotation_pilot/recordings/"
    notes_path = "~/git/annotation_pilot/notes/"
    output_folder = None  # overwrite notes files
    mode = "extended"
    main(
        audio_path=audio_path,
        notes_path=notes_path,
        output_folder=output_folder,
        mode=mode
    )


if __name__ == "__main__":
    run()

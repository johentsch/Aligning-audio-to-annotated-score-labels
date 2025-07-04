###################################################################

""" This module contains the main audio-to-annotations functionality and helper functions."""
import os
import warnings
from typing import Optional, Literal, Tuple

import librosa.display
import ms3
import numpy as np
import pandas as pd
import scipy.interpolate
from libfmp.b import plot_chromagram
# import sys
# sys.path.insert(0, '../sync_toolbox/synctoolbox/')
from synctoolbox.dtw.mrmsdtw import sync_via_mrmsdtw
from synctoolbox.dtw.utils import compute_optimal_chroma_shift, shift_chroma_vectors, make_path_strictly_monotonic
from synctoolbox.feature.chroma import pitch_to_chroma, quantize_chroma, quantized_chroma_to_CENS
from synctoolbox.feature.csv_tools import df_to_pitch_features, df_to_pitch_onset_features
from synctoolbox.feature.dlnco import pitch_onset_features_to_DLNCO
from synctoolbox.feature.pitch import audio_to_pitch_features
from synctoolbox.feature.pitch_onset import audio_to_pitch_onset_features
from synctoolbox.feature.utils import estimate_tuning

############################# IMPORTS #############################


############################# GLOBALS #############################

Fs = 22050
feature_rate = 50
step_weights = np.array([1.5, 1.5, 2.0])
threshold_rec = 10 ** 6



############################# UTILS #############################

def to_start(quarterbeat):
    """
    Converts "quarterbeat" element from DCML mozart-sontatas corpus from Fraction type to float
    """
    # Note: keep +1 to ensure first note is not attached to 0 even after warping calculation
    return float(quarterbeat.numerator)/float(quarterbeat.denominator)+1


def to_end(start, duration):
    """
    Computes "end" from "start" and "duration"
    """
    return start + duration


def get_quarterbeats_column_name(notes_df: pd.DataFrame) -> str:
    if "quarterbeats_playthrough" in notes_df.columns:
        qb_column = "quarterbeats_playthrough"
    elif "quarterbeats" in notes_df.columns:
        qb_column = "quarterbeats"
    else:
        raise ValueError("The notes file must contain either 'quarterbeats' or 'quarterbeats_playthrough' column")
    return qb_column

def corpus_to_df_musical_time(notes_path):
    """
    Converts notes TSV file as in DCML mozart-sonatas corpus [1] to a dataframe used for warping path synchronization

    Parameters:
        notes_path: str
            Location of the TSV file containing notes information
                Note: the file must at least contain 'quarterbeats', 'duration_qb' and 'midi'

    Returns:
        df_annotation: DataFrame object
            Dataframe with columns ['start', 'duration', 'pitch', 'velocity', 'instrument']
                Note: 'start', 'duration', 'pitch' derive from the original file,
                    'velocity' and 'instrument' are artificially infered
    """
    # Load TSV into dataframe
    notes_df = ms3.load_tsv(notes_path) 
    
    # Select and rename columns of interest
    qb_column = get_quarterbeats_column_name(notes_df)
    df_annotation = notes_df[[qb_column, 'duration_qb', 'midi']].rename(
        columns={qb_column:'start', 'duration_qb':'duration', 'midi': 'pitch'})
    
    # Create "start" column
    df_annotation['start'] = df_annotation['start'].apply(lambda x: to_start(x))
    # Create "end" column
    
    df_annotation['end'] = df_annotation.apply(lambda x: to_end(x['start'], x['duration']), axis=1)
    
    # Re-order columns
    df_annotation = df_annotation[['start', 'duration', 'pitch', 'end']] 
    
    # Infer velocity because needed for synctoolbox processing
    df_annotation['velocity']=1.0 
    
    # Infer instrument, idem
    df_annotation['instrument']="piano"
    
    return df_annotation

def align_corpus_notes_and_labels(notes_path, labels_path):
    """
    Merges notes and labels corpus dataframes together from their TSV file 
    from DCML mozart-sonatas corpus [1], using quarterbeats as the key for an union-based merge.
    
    Parameters:
       notes_path: str
            Location of the TSV file containing notes information
                Note: the file must at least contain 'quarterbeats', 'duration_qb' and 'midi' 
       labels_path: str
            Location of the TSV file containing labels information
                Note: the file must at least contain 'quarterbeats' and 'duration_qb' 
            
    Returns:
        notes_extended: DataFrame object
            DataFrame containing notes and labels information, aligned on notes index
            
    """
    notes_qb = ms3.load_tsv(notes_path)
    labels_qb = ms3.load_tsv(labels_path)
    notes_qb_column = get_quarterbeats_column_name(notes_qb)
    labels_qb_column = get_quarterbeats_column_name(labels_qb)
    if notes_qb_column != labels_qb_column:
        raise ValueError(
f"""It looks like between notes and labels, one of them is unfolded while the other isn't:
Notes: {notes_qb_column!r}
Labels: {labels_qb_column!r}"""
        )

    notes_extended = pd.merge(notes_qb, labels_qb.drop(columns=
                                                        ['duration_qb', 'mc', 'mn', 'mc_onset',
                                                        'mn_onset', 'timesig', 'staff', 'voice']), 
                                left_on=[notes_qb_column], right_on=[notes_qb_column], how='outer')
    
    return notes_extended

def align_warped_notes_labels(
        df_annotation_warped,
        notes_labels_extended,
        last_end: float,
        mode='compact',
):
    """
    After warping path is computed and synchronization of notes dataframe with audio is performed,
    align the labels based using notes index as a key. 
        Note: Labels must have been aligned with notes beforehand.
    
    Parameters:
        df_annotation_warped: DataFrame object
            Dataframe resulting from warping process
        notes_labels_extended:DataFrame object
            Dataframe containing corpus information
        mode: str (optional)
            Level of details the result should keep.
            Can take value between: ['compact', 'labels', 'extended']
                Compact: only outputs labels aligned with timestamps
                Labels details: outputs labels and additional label information aligned with timestamps
                Extended: outputs merged notes and labels datasets with additional information, aligned with timestamps
            Defaults to 'compact'.

    Returns:
        aligned_timestamps_labels: DataFrame object
            Labels and optional information, aligned with timestamps
    """
    # the following columns are included on both sides and will be dropped on one
    duplicate_columns = ['start', 'end', 'mc_playthrough', 'mn_playthrough', 'quarterbeats_all_endings']

    if mode == 'compact':
        aligned_timestamps_labels = pd.merge(
            left=df_annotation_warped.drop(
                columns=[
                    'velocity',
                    'instrument',
                    'duration',
                    'pitch',
                    'end'
                ]
            ),
            how="outer",
            right=notes_labels_extended[['label']],
            right_index=True,
            left_index=True
        )
        aligned_timestamps_labels = aligned_timestamps_labels.dropna(subset='label').drop_duplicates(
            subset=['start', 'label']
        )

    elif mode == 'labels':

        notes_related_columns = ['staff', 'voice', 'duration', 'nominal_duration', 'scalar', 'gracenote'
                                                                                             'tied', 'tpc', 'midi',
                                 'chord_id', ]

        drop_cols = [col for col in notes_related_columns + duplicate_columns if col in notes_labels_extended.columns]
        aligned_timestamps_labels = pd.merge(
            left=df_annotation_warped.drop(
                columns=[
                    'velocity',
                    'instrument',
                    'pitch'
                ]
            ).rename(
                columns={'duration': 'duration_seconds'}
            ),
            right=notes_labels_extended.drop(columns=drop_cols),
            how="outer",
            right_index=True,
            left_index=True
        )
        aligned_timestamps_labels = aligned_timestamps_labels.dropna(subset='label').drop_duplicates(
            subset=['start', 'label']
        )

    elif mode == 'extended':
        drop_cols = [col for col in duplicate_columns if col in notes_labels_extended.columns]
        aligned_timestamps_labels = pd.merge(
            left=df_annotation_warped.drop(
                columns=[
                    'velocity',
                    'instrument',
                    'pitch'
                ]
            ).rename(
                columns={'duration': 'duration_time'}
            ),
            right=notes_labels_extended.drop(columns=drop_cols),
            how="outer",
            right_index=True,
            left_index=True
        )

    else:
        raise ValueError(f"'mode' parameter should bei either 'compact', 'labels' or 'extended', got {mode}")

    update_duration_and_end_from_start(aligned_timestamps_labels, last_end)
    # aligned_timestamps_labels = aligned_timestamps_labels.rename(columns={'start':'timestamp'})
    
    return aligned_timestamps_labels


def update_duration_and_end_from_start(aligned_timestamps_labels, last_end):
    aligned_timestamps_labels.end = aligned_timestamps_labels.start.shift(-1).fillna(last_end)
    aligned_timestamps_labels.duration_seconds = aligned_timestamps_labels.end - aligned_timestamps_labels.start


def get_features_from_audio(audio, tuning_offset, Fs, feature_rate, visualize=False):
    """ 
    Adapted from synctoolbox [2] tutorial: `sync_audio_score_full.ipynb`
    
    Takes as input the audio file and computes quantized chroma and DLNCO features.
    
    Parameters:
        audio: str
            Path to file in .wav format
        tuning_offset: int
            Tuning offset used to shift the filterbank (in cents)
        Fs: float
            Sampling rate of f_audio (in Hz)
        feature_rate: int
            Features per second
        visualize: bool (optional)
            Whether to visualize chromagram of audio quantized chroma features. Defaults to False.
            
    Returns:
    f_chroma_quantized: np.ndarray
        Quantized chroma representation
    f_DLNCO : np.array
        Decaying locally adaptive normalized chroma onset (DLNCO) features
    
    """
    
    f_pitch = audio_to_pitch_features(f_audio=audio, Fs=Fs, tuning_offset=tuning_offset, feature_rate=feature_rate, verbose=visualize)
    f_chroma = pitch_to_chroma(f_pitch=f_pitch)
    f_chroma_quantized = quantize_chroma(f_chroma=f_chroma)
    
    if visualize:
        plot_chromagram(f_chroma_quantized, title='Quantized chroma features - Audio', Fs=feature_rate, figsize=(9,3))

    f_pitch_onset = audio_to_pitch_onset_features(f_audio=audio, Fs=Fs, tuning_offset=tuning_offset, verbose=visualize)
    f_DLNCO = pitch_onset_features_to_DLNCO(f_peaks=f_pitch_onset, feature_rate=feature_rate, feature_sequence_length=f_chroma_quantized.shape[1], visualize=visualize)
    return f_chroma_quantized, f_DLNCO


def get_features_from_annotation(df_annotation, feature_rate, visualize=False):
    """ Adapted from synctoolbox [2] tutorial: `sync_audio_score_full.ipynb`
    
    Takes as input symbolic annotations dataframe and computes quantized chroma and DLNCO features.
    
    Parameters:
        df_annotation: DataFrame object
            Dataframe of notes annotations containing ['start', 'duration', 'pitch', 'velocity', 'instrument']        
        feature_rate: int
            Features per second
        visualize: bool (optional)
            Whether to visualize chromagram of audio quantized chroma features. Defaults to False.
            
    Returns:
    f_chroma_quantized: np.ndarray
        Quantized chroma representation
    f_DLNCO : np.array
        Decaying locally adaptive normalized chroma onset (DLNCO) features
    
    """
       
    f_pitch = df_to_pitch_features(df_annotation, feature_rate=feature_rate)
    f_chroma = pitch_to_chroma(f_pitch=f_pitch)
    f_chroma_quantized = quantize_chroma(f_chroma=f_chroma)
    
    if visualize:
        plot_chromagram(f_chroma_quantized, title='Quantized chroma features - Annotation', Fs=feature_rate, figsize=(9, 3))
    f_pitch_onset = df_to_pitch_onset_features(df_annotation)
    f_DLNCO = pitch_onset_features_to_DLNCO(f_peaks=f_pitch_onset,
                                            feature_rate=feature_rate,
                                            feature_sequence_length=f_chroma_quantized.shape[1],
                                            visualize=visualize)
    
    return f_chroma_quantized, f_DLNCO

def warp_annotations(df_annotation, warping_path, feature_rate):
    """ Adapted from synctoolbox [2] tutorial: `sync_audio_score_full.ipynb`
    
    Warp timestamps to annotations after having computed the warping path between audio and annotations.

    Parameters:
        df_annotation: DataFrame object
            Dataframe of notes annotations containing ['start', 'duration', 'pitch', 'velocity', 'instrument']    
        warping_path: np.ndarray(2:)
            Warping path
        feature_rate: int
            Features per second

    Returns:
        Notes annotations warped with corresponding timestamps
    """
    df_annotation_warped = df_annotation.copy(deep=True)
    df_annotation_warped["end"] = df_annotation_warped["start"] + df_annotation_warped["duration"]
    df_annotation_warped[['start', 'end']] = scipy.interpolate.interp1d(warping_path[1] / feature_rate, 
                               warping_path[0] / feature_rate, kind='linear', fill_value="extrapolate")(df_annotation[['start', 'end']])
    df_annotation_warped["duration"] = df_annotation_warped["end"] - df_annotation_warped["start"]
    return df_annotation_warped




def evaluate_matching(df_original_notes, df_warped_notes, verbose=True):
    """ 
    Evaluates matching of original corpus notes list with warped notes.
    If DTW runs correctly, all notes should have been matched.
    
    Global evaluation can be performed if groundtruth annotations of the audio are known,
    with synctoolbox function `evaluate_synchronized_positions`.
    
    Parameters:
        df_original_notes (DataFrame object): original notes list from corpus
        df_warped_notes (DataFrame object): 
        verbose (bool, optional): Prints evaluation. Defaults to True.

    Returns:
        Matching score, i.e. percentage of original notes that have found a match
        
    Note: 
        Using Bio library is a powerful tool to find sequences alignment. The function
        so far only returns the matching score, that could be easily calculated as:
        >> len(df_warped_notes)/len(df_original_notes)
        assuming every warped note corresponds to an original event. Using the Bio
        library accounts for cases where warped notes show events with no correspondances
        in the original events list.
    
        
    """
    diff_matched_notes = len(df_original_notes) - len(df_warped_notes)
    matching_score = len(df_warped_notes)/len(df_original_notes)
    if verbose:
        print("Matching percentage: {:.4}%".format(matching_score*100))
        print("Number of unmachted notes: {:}".format(diff_matched_notes))

    return matching_score


def align_notes_labels_audio(
        audio_path: str,
        notes_path: str,
        labels_path: Optional[str] = None,
        store: bool = True,
        store_path: Optional[str] = None,
        verbose: bool = False,
        visualize: bool = False,
        evaluate: bool = False,
        mode: Literal['compact', 'labels', 'extended'] = 'compact'
) -> Tuple[pd.DataFrame, pd.DataFrame, float]:
    """This function performs the whole pipeline of aligning an audio recording of a piece and its
    corresponding labels annotations from DCML's Mozart sonatas corpus [1], using synctoolbox dynamic
    time warping (DTW) tools [2]. It takes as input the paths to the audio file, to the labels TSV file
    and the notes TSV file as well, for DTW to align single notes events specifically. It returns a
    dataframe containing labels and their corresponding timestamps within the audio.
    
    Depending on the `mode` passed, the result can contain the minimal information (labels and timestamps)
    that are useful for live visualization (e.g. with SonicVisualiser) or more detailed information about 
    labels (and possibly notes) as in the original TSV files.

    Parameters:
        notes_path: str
            Location of the TSV file containing notes information
                Note: the file must at least contain 'quarterbeats', 'duration_qb' and 'midi'
        labels_path: str
            Location of the TSV file containing labels information
                Note: the file must at least contain 'quarterbeats' and 'duration_qb'
        audio_path: str
            Location of the audio file. The audio should be in .wav format.
        store: bool (optional)
            Stores the alignment result. Defaults to False.
        store_path: str (optional)
            If store is set to True, path to store the result in. Defaults to current working directory.
        verbose: bool (optional)
            Prints information. Defaults to False.
        visualize: bool (optional)
            Prints DTW process visualizations, to be used if function called in notebook for example.
            Defaults to False.
        evaluate: bool (optional)
            Prints DTW matching score. Defaults to False.
        mode: str (default: 'compact')
            compact:
                - without labels: columns ["start", "end", "pitch"]
                - with labels: columns ["timestamp", "label"]
            extended:
                - without labels: fully aligned original notes TSV
                - with labels: fully aligned original notes TSV with original label columns merged
            labels:
                Original label columns preceded by columns ["timestamp", "duration_time", "end"].
                Please note that the durations and end points to not currently reflect those of the
                labels but those of one of the notes they co-occur with.

    Returns:
        aligned_notes:
            The first DataFrame always corresponds to the result of align_notes_labels_audio() without ``labels_path``
            and with mode "extended".
        result: DataFrame object
            Dataframe containing aligned notes or labels, according to the parameters.
            
            
    References: 
    [1] Hentschel, J., Neuwirth, M. and Rohrmeier, M., 2021. 
        The Annotated Mozart Sonatas: Score, Harmony, and Cadence. 
        Transactions of the International Society for Music Information Retrieval, 4(1), pp.67–80. 
        DOI: http://doi.org/10.5334/tismir.63
        [https://github.com/DCMLab/mozart_piano_sonatas]
     
    [2] Meinard Müller, Yigitcan Özer, Michael Krause, Thomas Prätzlich, and Jonathan Driedger. and Frank Zalkow. 
        Sync Toolbox: A Python Package for Efficient, Robust, and Accurate Music Synchronization. 
        Journal of Open Source Software (JOSS), 6(64), 2021.
        [https://github.com/meinardmueller/synctoolbox]
    
    """
    # Prepare annotation format
    df_annotation = corpus_to_df_musical_time(notes_path)
    # Keep track of notes annotations and labels correspondances
    if labels_path:
        df_annotation_extended = align_corpus_notes_and_labels(notes_path, labels_path)
    else:
        df_annotation_extended = None
        if mode == "labels":
            raise ValueError("When 'mode' is set to 'labels', labels_path must be provided")

    
    # Load audio
    audio, _ = librosa.load(audio_path, sr=Fs)
    audio_duration = librosa.get_duration(y=audio, sr=Fs)

    # Estimate tuning deviation
    tuning_offset = estimate_tuning(audio, Fs)
    if verbose:
        print('Estimated tuning deviation for recording: %d cents' % (tuning_offset))
    
    # Compute features from audio
    f_chroma_quantized_audio, f_DLNCO_audio = get_features_from_audio(audio, tuning_offset, Fs, feature_rate, visualize=visualize)
    
    # Compute features from annotations
    f_chroma_quantized_annotation, f_DLNCO_annotation = get_features_from_annotation(df_annotation, feature_rate, visualize=visualize)
    
    # Calculate chroma shift between audio and annotations
    f_cens_1hz_audio = quantized_chroma_to_CENS(f_chroma_quantized_audio, 201, 50, feature_rate)[0]
    f_cens_1hz_annotation = quantized_chroma_to_CENS(f_chroma_quantized_annotation, 201, 50, feature_rate)[0]
    opt_chroma_shift = compute_optimal_chroma_shift(f_cens_1hz_audio, f_cens_1hz_annotation)
    
    if verbose:
        print('Pitch shift between the audio recording and score, determined by DTW:', opt_chroma_shift, 'bins')

    # Apply potential shift to audio and annotations features
    f_chroma_quantized_annotation = shift_chroma_vectors(f_chroma_quantized_annotation, opt_chroma_shift)
    # f_DLNCO_annotation = shift_chroma_vectors(f_DLNCO_annotation, opt_chroma_shift)
    
    # Compute warping path
    wp = sync_via_mrmsdtw(f_chroma1=f_chroma_quantized_audio, 
                      #f_onset1=f_DLNCO_audio, 
                      f_chroma2=f_chroma_quantized_annotation, 
                      #f_onset2=f_DLNCO_annotation, 
                      input_feature_rate=feature_rate, 
                      step_weights=step_weights, 
                      threshold_rec=threshold_rec, 
                      verbose=visualize)
    
    # Make warping path monotonic
    wp = make_path_strictly_monotonic(wp)
    
    # Use warping path to align annotations to real timestamps 
    df_annotation_warped = warp_annotations(df_annotation, wp, feature_rate)
    
    # Evaluate matching score after warping
    if evaluate:
        _ = evaluate_matching(df_annotation, df_annotation_warped, verbose=True)

    aligned_notes = get_original_notes_warped(notes_path, df_annotation_warped)
    if df_annotation_extended is not None:  # there are labels to be aligned
        result = align_warped_notes_labels(
            df_annotation_warped,
            df_annotation_extended,
            last_end=audio_duration,
            mode=mode
        )
    elif mode == 'compact':
        result = df_annotation_warped[['start', 'end', 'pitch']]
    elif mode == 'extended':
        result = aligned_notes
    else:
        raise ValueError(f"'mode' parameter should bei either 'compact', 'labels' or 'extended', got {mode}")

    
    # Store
    if store:
        store_and_report_result(result, store_path, audio_path, "_aligned.csv", "alignment result")
    return aligned_notes, result, audio_duration


def get_original_notes_warped(notes_path: str, df_annotation_warped: pd.DataFrame) -> pd.DataFrame:
    notes_df = ms3.load_tsv(notes_path)
    if "start" in notes_df.columns:
        warnings.warn(
            f"{notes_path!r} already came with a 'start' column, so I've left it as it was. If you want to add "
            f"alignments for several recordings to the same note table, you will need to rename the column 'start' "
            f"after creating it and before adding a new one."
        )
        return notes_df
    assert notes_df.midi.equals(df_annotation_warped.pitch), "MIDI values do not match"
    result = pd.concat([notes_df, df_annotation_warped[['start', 'end']]], axis=1)
    return result


def store_and_report_result(
        df: pd.DataFrame,
        store_path: str,
        original_path: str,
        suffix: str,
        what: str
) -> None:
    fname_if_dir = make_filename_from_path(original_path, suffix)
    store_path = write_csv(df, store_path, fname_if_dir)
    print(f"\nStored {what} to {store_path!r}")


def make_filename_from_path(path, suffix):
    audio_fname, _ = os.path.splitext(os.path.basename(path))
    fname_if_dir = audio_fname + suffix
    return fname_if_dir


def write_csv(
        df: pd.DataFrame,
        store_path: Optional[str] = None,
        fname_if_dir: Optional[str] = None
) -> str:
    """

    Args:
        df: DataFrame to be stored in CSV format.
        store_path:
            Where to store. If None, defaults to current working directory. If directory, ``fname_if_dir`` needs to be
            defined; if filepath, ``fname_if_dir`` is ignored.
        fname_if_dir:
            File name (or relative filepath) to be combined with ``store_path``. If the extension is
            ".tsv", output is written as tab-separated values, otherwise as comma-separated values.
    Returns:
        Returns
    """
    if store_path is None:
        store_path = os.getcwd()
    if os.path.isdir(store_path):
        assert fname_if_dir is not None, f"If store_path is a directory, a filename need to be specified."
        store_path = os.path.join(store_path, fname_if_dir)
    csv_args = dict(sep="\t") if store_path.endswith(".tsv") else {}
    df.to_csv(store_path, index=False, **csv_args)
    return store_path

# Author: Denis A. Engemann <denis.engemann@gmail.com>
# License: BSD (3-clause)

import os.path as op
import itertools as itt
import re

import numpy as np
import scipy.io as scio
from scipy import linalg

from mne import EpochsArray, pick_info
from mne.transforms import apply_trans
from mne.io.bti.bti import _get_bti_info, read_raw_bti
from mne.io import _loc_to_coil_trans

from .file_mapping import get_file_paths


def _parse_trans(string):
    """helper to parse transforms"""
    return np.array(string.replace('\n', '')
                          .strip('[] ')
                          .split(' '), dtype=float).reshape(4, 4)


def _parse_hcp_trans(fid, transforms, convert_to_meter):
    """" another helper """
    contents = fid.read()
    for trans in contents.split(';'):
        if 'filename' in trans or trans == '\n':
            continue
        key, trans = trans.split(' = ')
        key = key.lstrip('\ntransform.')
        transforms[key] = _parse_trans(trans)
        if convert_to_meter:
            transforms[key][:3, 3] *= 1e-3  # mm to m
    if not transforms:
        raise RuntimeError('Could not parse the transforms.')


def _read_trans_hcp(fname, convert_to_meter):
    """Read + parse transforms
    subject_MEG_anatomy_transform.txt
    """
    transforms = dict()
    with open(fname) as fid:
        _parse_hcp_trans(fid, transforms, convert_to_meter)
    return transforms


def _read_landmarks_hcp(fname):
    """ XXX parse landmarks currently not used """
    out = dict()
    with open(fname) as fid:
        for line in fid:
            kind, data = line.split(' = ')
            kind = kind.split('.')[1]
            if kind == 'coordsys':
                out['coord_frame'] = data.split(';')[0].replace("'", "")
            else:
                data = data.split()
                for c in ('[', '];'):
                    if c in data:
                        data.remove(c)
                out[kind] = np.array(data, dtype=int) * 1e-3  # mm to m
    return out


def _get_head_model(head_model_fname):
    """ helper to parse head model from matfile """
    head_mat = scio.loadmat(head_model_fname, squeeze_me=False)
    pnts = head_mat['headmodel']['bnd'][0][0][0][0][0]
    faces = head_mat['headmodel']['bnd'][0][0][0][0][1]
    faces -= 1  # correct for Matlab's 1-based index
    return pnts, faces


def _read_bti_info(zf, config):
    """ helper to only access bti info from pdf file """
    raw_fid = None
    info, bti_info = _get_bti_info(
        pdf_fname=raw_fid, config_fname=config, head_shape_fname=None,
        rotation_x=0.0, translation=(0.0, 0.02, 0.11),
        ecg_ch='E31', eog_ch=('E63', 'E64'),
        convert=False,  # no conversion to neuromag coordinates
        rename_channels=False,  # keep native channel names
        sort_by_ch_name=False)  # do not change native order
    return info


def _read_raw_bti(raw_fid, config_fid, convert):
    """Convert and raw file from HCP input"""
    raw = read_raw_bti(
        raw_fid, config_fid, convert=convert, head_shape_fname=None,
        sort_by_ch_name=False, rename_channels=False, preload=True)

    return raw


def _check_raw_config_runs(raws, configs):
    """XXX this goes to tests later, currently not used """
    for raw, config in zip(raws, configs):
        assert op.split(raw)[0] == op.split(config)[0]
    run_str = set([configs[0].split('/')[-3]])
    for config in configs[1:]:
        assert set(configs[0].split('/')) - set(config.split('/')) == run_str


def _check_infos_trans(infos):
    """XXX this goes to tests later, currently not used"""
    chan_max_idx = np.argmax([c['nchan'] for c in infos])
    chan_template = infos[chan_max_idx]['ch_names']
    channels = [c['ch_names'] for c in infos]
    common_channels = set(chan_template).intersection(*channels)

    common_chs = [[c['chs'][c['ch_names'].index(ch)] for ch in common_channels]
                  for c in infos]
    dev_ctf_trans = [i['dev_ctf_t']['trans'] for i in infos]
    cns = [[c['ch_name'] for c in cc] for cc in common_chs]
    for cn1, cn2 in itt.combinations(cns, 2):
        assert cn1 == cn2
    # BTI stores data in head coords, as a consequence the coordinates
    # change across run, we apply the ctf->ctf_head transform here
    # to check that all transforms are correct.
    cts = [np.array([linalg.inv(_loc_to_coil_trans(c['loc'])).dot(t)
                    for c in cc])
           for t, cc in zip(dev_ctf_trans, common_chs)]
    for ct1, ct2 in itt.combinations(cts, 2):
        np.testing.assert_array_almost_equal(ct1, ct2, 12)


def read_raw_hcp(subject, data_type, run_index=0, hcp_path=op.curdir):
    """ Read HCP raw data

    Parameters
    ----------
    subject : str, file_map
        The subject
    data_type : str
        The kind of data to read. The following options are supported:
        'rest'
        'task_motor'
        'task_story_math'
        'task_working_memory'
        'noise_empty_room'
        'noise_subject'
    run_index : int
        The run index. For the first run, use 0, for the second, use 1.
        Also see HCP documentation for the number of runs for a given data
        type.
    hcp_path : str
        The HCP directory, defaults to op.curdir.

    Returns
    -------
    raw : instance of mne.io.Raw
        The MNE raw object.
    """
    pdf, config = get_file_paths(
        subject=subject, data_type=data_type,
        output='meg_data',
        run_index=run_index, processing='unprocessed', hcp_path=hcp_path)

    raw = _read_raw_bti(pdf, config, convert=False)
    return raw


def read_info_hcp(subject, data_type, run_index=0, hcp_path=op.curdir):
    """Read info from unprocessed data

    Parameters
    ----------
    subject : str, file_map
        The subject
    data_type : str
        The kind of data to read. The following options are supported:
        'rest'
        'task_motor'
        'task_story_math'
        'task_working_memory'
        'noise_empty_room'
        'noise_subject'
    run_index : int
        The run index. For the first run, use 0, for the second, use 1.
        Also see HCP documentation for the number of runs for a given data
        type.
    hcp_path : str
        The HCP directory, defaults to op.curdir.

    Returns
    -------
    info : instance of mne.io.meas_info.Info
        The MNE channel info object.
    """
    _, config = get_file_paths(
        subject=subject, data_type=data_type,
        output='meg_data',
        run_index=run_index, processing='unprocessed', hcp_path=hcp_path)

    meg_info = _read_bti_info(None, config)
    return meg_info


def read_epochs_hcp(subject, data_type, onset='TIM', run_index=0,
                    hcp_path=op.curdir):
    """Read HCP processed data

    Parameters
    ----------
    subject : str, file_map
        The subject
    data_type : str
        The kind of data to read. The following options are supported:
        'rest'
        'task_motor'
        'task_story_math'
        'task_working_memory'
    onset : str
        The T0 of the time-locked window. Depends on task data, e.g., 'TRESP'
        or 'TIM'. Defaults to 'TIM'.
    run_index : int
        The run index. For the first run, use 0, for the second, use 1.
        Also see HCP documentation for the number of runs for a given data
        type.
    hcp_path : str
        The HCP directory, defaults to op.curdir.

    Returns
    -------
    epochs : instance of mne.Epochs
        The MNE epochs. Note, these are pseudo-epochs in the case of
        onset == 'rest'.
    """
    info = read_info_hcp(subject=subject, data_type='data_type',
                         run_index=run_index)

    epochs_mat_fname = get_file_paths(
        subject=subject, data_type=data_type,
        output='meg_data', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)[0]

    epochs = _read_epochs(epochs_mat_fname=epochs_mat_fname, info=info)

    return epochs


def _read_epochs(epochs_mat_fname, info):
    """ read the epochs from matfile """
    data = scio.loadmat(epochs_mat_fname,
                        squeeze_me=True)['data']
    ch_names = [ch for ch in data['label'].tolist()]
    info['sfreq'] = data['fsample'].tolist()
    data = np.array([data['trial'].tolist()][0].tolist())
    events = np.zeros((len(data), 3), dtype=np.int)
    events[:, 0] = np.arange(len(data))
    events[:, 2] = 99
    this_info = pick_info(
        info, [info['ch_names'].index(ch) for ch in ch_names],
        copy=True)
    return EpochsArray(data=data, info=this_info, events=events, tmin=0)


def read_trial_info_hcp(subject, data_type, run_index=0, hcp_path=op.curdir):
    """ read trial info """

    trial_info_mat_fname = get_file_paths(
        subject=subject, data_type=data_type,
        output='meg_data', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)[0]

    trl_infos = _read_trial_info(trial_info_mat_fname=trial_info_mat_fname)
    return trl_infos


def _read_trial_info(trial_info_mat_fname):
    """ helper to read trial info """

    data = scio.loadmat(trial_info_mat_fname, squeeze_me=True)['trlInfo']
    out = dict()

    for idx, lock_name in enumerate(data['lockNames'].tolist()):
        out[lock_name] = dict(
            comments=data['trlColDescr'].tolist()[idx],
            codes=data['lockTrl'].tolist().tolist()[idx])

    return out


def _check_sorting_runs(candidates, id_char):
    """helper to ensure correct run-parsing and mapping"""
    run_idx = [f.find(id_char) for f in candidates]
    for config, idx in zip(candidates, run_idx):
        assert config[idx - 1].isdigit()
        assert not config[idx - 2].isdigit()
    runs = [int(f[idx - 1]) for f, idx in zip(candidates, run_idx)]
    return runs, candidates


def _parse_annotations_segments(segment_strings):
    """Read bad segments defintions from text file"""
    for char in '}]':  # multi line array definitions
        segment_strings = segment_strings.replace(
            char + ';', 'splitme'
        )
    split = segment_strings.split('splitme')
    out = dict()
    for entry in split:
        if len(entry) == 1 or entry == '\n':
            continue
        key, rest = entry.split(' = ')
        val = np.array(
            [k for k in [''.join([c for c in e if c.isdigit()])
             for e in rest.split()] if k.isdigit()], dtype=int)
        # reindex and reshape
        val = val.reshape(-1, 2) - 1
        out[key.split('.')[1]] = val
    return out


def read_annot_hcp(subject, data_type, run_index=0, hcp_path=op.curdir):
    """ Read annotations for bad data and ICA.

    Parameters
    ----------
    subject : str, file_map
        The subject
    data_type : str
        The kind of data to read. The following options are supported:
        'rest'
        'task_motor'
        'task_story_math'
        'task_working_memory'
    run_index : int
        The run index. For the first run, use 0, for the second, use 1.
        Also see HCP documentation for the number of runs for a given data
        type.
    hcp_path : str
        The HCP directory, defaults to op.curdir.

    Returns
    -------
    out : dict
        The annotations.
    """
    bads_files = get_file_paths(
        subject=subject, data_type=data_type,
        output='bads', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)
    segments_fname = [k for k in bads_files if
                      k.endswith('baddata_badsegments.txt')][0]
    bads_fname = [k for k in bads_files if
                  k.endswith('baddata_badchannels.txt')][0]

    ica_files = get_file_paths(
        subject=subject, data_type=data_type,
        output='ica', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)
    ica_fname = [k for k in ica_files if k.endswith('icaclass_vs.txt')][0]

    out = dict()
    iter_fun = [
        ('channels', _parse_annotations_bad_channels, bads_fname),
        ('segments', _parse_annotations_segments, segments_fname),
        ('ica', _parse_annotations_ica, ica_fname)]

    for subtype, fun, fname in iter_fun:
        with open(fname, 'r') as fid:
            out[subtype] = fun(fid.read())

    return out


def read_ica_hcp(subject, data_type, run_index=0, hcp_path=op.curdir):
    """
    Parameters
    ----------
    subject : str, file_map
        The subject
    hcp_path : str
        The HCP directory
    kind : str
        the data type

    Returns
    -------
    out : numpy structured array
        The ICA mat struct.
    """

    ica_files = get_file_paths(
        subject=subject, data_type=data_type,
        output='ica', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)
    ica_fname_mat = [k for k in ica_files if k.endswith('icaclass.mat')][0]

    mat = scio.loadmat(ica_fname_mat, squeeze_me=True)['comp_class']
    return mat


def _parse_annotations_bad_channels(bads_strings):
    """Read bad channel definitions from text file"""
    for char in '}]':
        bads_strings = bads_strings.replace(
            char + ';', 'splitme'
        )
    split = bads_strings.split('splitme')
    out = dict()
    for entry in split:
        if len(entry) == 1 or entry == '\n':
            continue
        key, rest = entry.split(' = ')
        val = [ch for ch in rest.split("'") if ch.isalnum()]
        out[key.split('.')[1]] = val
    return out


def _parse_annotations_ica(ica_strings):
    """Read bad channel definitions from text file"""
    # prepare splitting
    for char in '}]':  # multi line array definitions
        ica_strings = ica_strings.replace(
            char + ';', 'splitme'
        )
    # scalar variables
    match_inds = list()
    for match in re.finditer(';', ica_strings):
        ii = match.start()
        if ica_strings[ii - 1].isalnum():
            match_inds.append(ii)

    ica_strings = list(ica_strings)
    for ii in match_inds:
        ica_strings[ii] = 'splitme'
    ica_strings = ''.join(ica_strings)

    split = ica_strings.split('splitme')
    out = dict()
    for entry in split:
        if len(entry) == 1 or entry == '\n':
            continue
        key, rest = entry.split(' = ')
        if '[' in rest:
            sep = ' '
        else:
            sep = "'"
        val = [(int(ch) if ch.isdigit() else ch) for ch in
               rest.split(sep) if ch.isalnum()]
        out[key.split('.')[1]] = val
    return out

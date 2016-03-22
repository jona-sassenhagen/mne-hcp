# Author: Denis A. Engemann <denis.engemann@gmail.com>
# License: BSD (3-clause)

import os.path as op
import os
import itertools as itt
import zipfile
from StringIO import StringIO

import numpy as np
import scipy.io as scio
from scipy import linalg

from mne import write_trans, write_surface, EpochsArray, pick_info
from mne.transforms import Transform, apply_trans
from mne.io.bti.bti import _get_bti_info, read_raw_bti
from mne.io import _loc_to_coil_trans
from mne.utils import logger
from mne.externals import six

from .file_mapping import get_files_subject


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


def read_trans_hcp(fname, convert_to_meter):
    """Read + parse transforms

    subject_MEG_anatomy_transform.txt
    """

    transforms = dict()
    with open(fname) as fid:
        _parse_hcp_trans(fid, transforms, convert_to_meter)
    return transforms


def read_landmarks_hcp(fname):
    """ parse landmarks """
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


def _get_head_model(head_model_fid, hcp_trans, ras_trans):
    """ read head model """
    head_mat = scio.loadmat(head_model_fid, squeeze_me=False)
    pnts = head_mat['headmodel']['bnd'][0][0][0][0][0]
    faces = head_mat['headmodel']['bnd'][0][0][0][0][1]
    faces -= 1  # correct matlab index

    pnts = apply_trans(
        linalg.inv(ras_trans).dot(hcp_trans['bti2spm']), pnts)
    return pnts, faces


def extract_anatomy(subject, hcp_path, anatomy_path, recordings_path=None):
    """Extract relevant anatomy and create MNE friendly directory layout"""
    if isinstance(subject, six.string_types):
        _, records = get_file_maps(
            hcp_path=hcp_path, include_subjects=[subject])[0]
    elif isinstance(subject, dict):
        records, subject = subject, subject.values()[0]['subject']
    else:
        raise ValueError('subject must be dict or str.')
    this_anatomy_path = op.join(anatomy_path, subject)
    _recursive_create_dir(this_anatomy_path, op.split(anatomy_path)[0])
    if not recordings_path:
        recordings_path = anatomy_path

    this_recordings_path = op.join(recordings_path, subject)
    _recursive_create_dir(this_recordings_path, op.split(recordings_path)[0])

    rec = records.get('3t-structural-preproc-extended', None)
    ras_trans_fname = ''
    if rec:
        logger.info('reading extended structural processing ...')
        with open(op.join(rec['root'], rec['file'])) as fid:
            zf = zipfile.ZipFile(fid)
            for info in zf.infolist():  # let's extract data!

                if not info.file_size:  # test if there is data == a file.
                    print 'skiping %s' % info.filename
                    continue

                fname = info.filename
                if fname.endswith('c_ras.mat'):
                    ras_trans_fname += fname

                sel = np.where(  # check + handle anatomy paths
                    # test if '/subpatath/' is in fname, that is how you filter
                    ['/' + k + '/' in fname for k in HCP.FS_ANATOMY_DIR])[0]
                if len(sel):  # hit!
                    start_path = HCP.FS_ANATOMY_DIR[sel[0]]
                    logger.debug(fname)
                    _write_target(
                        fname=fname, data=zf.read(fname),
                        start_path=start_path, out_path=this_anatomy_path)
            logger.info('done.')
            if not ras_trans_fname:
                raise ValueError(
                    'Could not find the Freesurfer RAS transform for'
                    'subject %s' % subject)
            logger.info('reading RAS freesurfer transform')
            ras_trans = np.array([
                r.split() for r in zf.read(ras_trans_fname)
                                     .split('\n') if r], dtype=np.float64)

    else:
        ValueError('Could not find extended anatomy for %s' % subject)

    rec = records.get('meg-anatomy', None)
    if rec:
        logger.info('reading MEG anatomy')
        with open(op.join(rec['root'], rec['file'])) as fid:
            zf = zipfile.ZipFile(fid)
            head_model_fname = _zip_get_fnames(
                zf=zf, kind='headmodel', extension='.mat', run=[1],
                max_runs=1)[0]
            transforms_fname = _zip_get_fnames(
                zf=zf, kind='anatomy_transform', extension='.txt', run=[1],
                max_runs=1)[0]

            hcp_trans = dict()
            _parse_hcp_trans(
                fid=StringIO(zf.read(transforms_fname)),
                transforms=hcp_trans, convert_to_meter=False)

            pnts, faces = _get_head_model(
                head_model_fid=StringIO(zf.read(head_model_fname)),
                hcp_trans=hcp_trans,
                ras_trans=ras_trans)

        tri_fname = op.join(this_anatomy_path, 'bem', 'inner_skull.surf')

        logger.info('extracting head model')
        _recursive_create_dir(
            op.join(this_anatomy_path, 'bem'), this_anatomy_path)
        write_surface(tri_fname, pnts, faces)

        logger.info('Combining RAS transform and coregistration')
        ras_trans_m = ras_trans.copy()
        ras_trans_m = linalg.inv(ras_trans_m)  # and the inversion

        # now convert to meter too here
        ras_trans_m[:3, 3] *= 1e-3
        bti2spm = hcp_trans['bti2spm']
        bti2spm[:3, 3] *= 1e-3
        head_mri_t = Transform(
            'ctf_head', 'mri', np.dot(ras_trans_m, bti2spm))

        logger.info('extracting coregistration')
        write_trans(op.join(
            this_recordings_path, '%s-head_mri-trans.fif') % subject,
            head_mri_t)
    else:
        logger.info('done.')
        ValueError('Could not find MEG anatomy for %s' % subject)


def _read_bti_info(zf, config):
    """ helper to only access bti info from pdf file """
    raw_fid = None
    info, bti_info = _get_bti_info(
        pdf_fname=raw_fid, config_fname=config, head_shape_fname=None,
        rotation_x=0.0, translation=(0.0, 0.02, 0.11), convert=False,
        ecg_ch='E31', eog_ch=('E63', 'E64'),
        rename_channels=False, sort_by_ch_name=False)
    return info


def _read_raw_bti(raw_fid, config_fid, convert):
    """Convert and raw file from HCP input"""
    raw = read_raw_bti(
        raw_fid, config_fid, convert=convert, head_shape_fname=None,
        sort_by_ch_name=False, rename_channels=False, preload=True)

    return raw


def _check_raw_config_runs(raws, configs):
    # XXX still needed?
    for raw, config in zip(raws, configs):
        assert op.split(raw)[0] == op.split(config)[0]
    run_str = set([configs[0].split('/')[-3]])
    for config in configs[1:]:
        assert set(configs[0].split('/')) - set(config.split('/')) == run_str


def _check_infos_trans(infos):
    """check info extraction"""
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


def read_raw_hcp(subject, data_type, run_index=0, hcp_path='.'):
    """ Read HCP raw data
    """

    pdf, config = get_files_subject(
        subject=subject, data_type=data_type,
        step='meg_data',
        run_index=run_index, processing='unprocessed', hcp_path=hcp_path)

    raw = _read_raw_bti(pdf, config, convert=False)
    return raw


def read_info_hcp(subject, data_type, run_index=0, hcp_path='.'):
    """ Read info from unprocessed data """
    _, config = get_files_subject(
        subject=subject, data_type=data_type,
        step='meg_data',
        run_index=run_index, processing='unprocessed', hcp_path=hcp_path)

    meg_info = _read_bti_info(None, config)
    return meg_info


def read_epochs_hcp(subject, data_type, onset='TIM', run_index=0,
                    hcp_path='.'):
    """Read HCP processed data

    Parameters
    ----------
    subject : str, dict
        The subject or the record from the directory parser
    hcp_path : str
        The directory containing the HCP data.
    data_type : str
        The type of epoched data, e.g. 'rest' or 'meg-motor', see
        `required_fields` in `parse_hcp_dir`.
    onset : str
        Depends on task data, e.g., 'TRESP' or 'TIM'. Defaults to 'TIM'.
    run : int
        The run number (not an index). Defaults to the first run.
    """
    info = read_info_hcp(subject=subject, data_type='data_type',
                         run_index=run_index)

    epochs_mat_fname = get_files_subject(
        subject=subject, data_type=data_type,
        step='meg_data', run_index=run_index, processing='preprocessed',
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


def read_trial_info_hcp(subject, data_type, run_index=0, hcp_path='.'):
    """ read trial info """

    trial_info_mat_fname = get_files_subject(
        subject=subject, data_type=data_type,
        step='meg_data', run_index=run_index, processing='preprocessed',
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
    split = segment_strings.split(';')
    out = dict()
    for entry in split:
        if len(entry) == 1 or entry == '\n':
            continue
        key, rest = entry.split(' = ')
        val = np.array([e.split(' ') for e in rest.split() if
                        e.replace(' ', '').isdigit()], dtype=int)
        # reindex and reshape
        val = val.reshape(-1, 2) - 1
        out[key.split('.')[1]] = val
    return out


def read_annot_hcp(subject, data_type, run_index=0, hcp_path='.'):
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
    out : dict
        The annotations.
    """
    bads_files = get_files_subject(
        subject=subject, data_type=data_type,
        step='bads', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)
    segments_fname = [k for k in bads_files if
                      k.endswith('baddata_badsegments.txt')][0]
    bads_fname = [k for k in bads_files if
                  k.endswith('baddata_badchannels.txt')][0]

    ica_files = get_files_subject(
        subject=subject, data_type=data_type,
        step='ica', run_index=run_index, processing='preprocessed',
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


def read_ica_hcp(subject, data_type, run_index=0, hcp_path='.'):
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

    ica_files = get_files_subject(
        subject=subject, data_type=data_type,
        step='ica', run_index=run_index, processing='preprocessed',
        hcp_path=hcp_path)
    ica_fname_mat = [k for k in ica_files if k.endswith('icaclass.mat')][0]

    mat = scio.loadmat(ica_fname_mat, squeeze_me=True)['comp_class']
    return mat


def _parse_annotations_bad_channels(bads_strings):
    """Read bad channel definitions from text file"""
    split = bads_strings.split(';')
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
    split = ica_strings.split(';')
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
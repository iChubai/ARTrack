from lib.test.evaluation.environment import EnvSettings

def local_env_settings():
    settings = EnvSettings()

    # Set your local paths here.

    settings.davis_dir = ''
    settings.got10k_lmdb_path = '/mnt/sda/hjd/ARTrack/data/got10k_lmdb'
    settings.got10k_path = '/mnt/sda/hjd/ARTrack/data/got10k'
    settings.got_packed_results_path = ''
    settings.got_reports_path = ''
    settings.itb_path = '/mnt/sda/hjd/ARTrack/data/itb'
    settings.lasot_extension_subset_path_path = '/mnt/sda/hjd/ARTrack/data/lasot_extension_subset'
    settings.lasot_lmdb_path = '/mnt/sda/hjd/ARTrack/data/lasot_lmdb'
    settings.lasot_path = '/mnt/sda/hjd/ARTrack/data/lasot'
    settings.network_path = '/mnt/sda/hjd/ARTrack/output/test/networks'    # Where tracking networks are stored.
    settings.nfs_path = '/mnt/sda/hjd/ARTrack/data/nfs'
    settings.otb_path = '/mnt/sda/hjd/ARTrack/data/otb'
    settings.prj_dir = '/mnt/sda/hjd/ARTrack'
    settings.result_plot_path = '/mnt/sda/hjd/ARTrack/output/test/result_plots'
    settings.results_path = '/mnt/sda/hjd/ARTrack/output/test/tracking_results'    # Where to store tracking results
    settings.save_dir = '/mnt/sda/hjd/ARTrack/output'
    settings.segmentation_path = '/mnt/sda/hjd/ARTrack/output/test/segmentation_results'
    settings.tc128_path = '/mnt/sda/hjd/ARTrack/data/TC128'
    settings.tn_packed_results_path = ''
    settings.tnl2k_path = '/mnt/sda/hjd/ARTrack/data/tnl2k'
    settings.tpl_path = ''
    settings.trackingnet_path = '/mnt/sda/hjd/ARTrack/data/trackingnet'
    settings.uav_path = '/mnt/sda/hjd/ARTrack/data/uav'
    settings.vot18_path = '/mnt/sda/hjd/ARTrack/data/vot2018'
    settings.vot22_path = '/mnt/sda/hjd/ARTrack/data/vot2022'
    settings.vot_path = '/mnt/sda/hjd/ARTrack/data/VOT2019'
    settings.youtubevos_dir = ''

    return settings


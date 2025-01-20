class EnvironmentSettings:
    def __init__(self):
        self.workspace_dir = '/mnt/sda/hjd/ARTrack'    # Base directory for saving network checkpoints.
        self.tensorboard_dir = '/mnt/sda/hjd/ARTrack/tensorboard'    # Directory for tensorboard files.
        self.pretrained_networks = '/mnt/sda/hjd/ARTrack/pretrained_networks'
        self.lasot_dir = '/mnt/sda/hjd/ARTrack/data/lasot'
        self.got10k_dir = '/mnt/sda/hjd/ARTrack/data/got10k/train'
        self.got10k_val_dir = '/mnt/sda/hjd/ARTrack/data/got10k/val'
        self.lasot_lmdb_dir = '/mnt/sda/hjd/ARTrack/data/lasot_lmdb'
        self.got10k_lmdb_dir = '/mnt/sda/hjd/ARTrack/data/got10k_lmdb'
        self.trackingnet_dir = '/mnt/sda/hjd/ARTrack/data/trackingnet'
        self.trackingnet_lmdb_dir = '/mnt/sda/hjd/ARTrack/data/trackingnet_lmdb'
        self.coco_dir = '/mnt/sda/hjd/ARTrack/data/coco'
        self.coco_lmdb_dir = '/mnt/sda/hjd/ARTrack/data/coco_lmdb'
        self.lvis_dir = ''
        self.sbd_dir = ''
        self.imagenet_dir = '/mnt/sda/hjd/ARTrack/data/vid'
        self.imagenet_lmdb_dir = '/mnt/sda/hjd/ARTrack/data/vid_lmdb'
        self.imagenetdet_dir = ''
        self.ecssd_dir = ''
        self.hkuis_dir = ''
        self.msra10k_dir = ''
        self.davis_dir = ''
        self.youtubevos_dir = ''

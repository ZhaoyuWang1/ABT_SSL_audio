"""
Barlow Twins for Audio (w/ Transformer encoder): Training.
References:
    https://github.com/facebookresearch/barlowtwins/blob/main/main.py
    https://github.com/nttcslab/byol-a/blob/master/train.py
"""

import argparse
from pprint import pprint
import os
import datetime

import torch
import torch.distributed as dist
import torch.backends.cudnn as cudnn
import torch.multiprocessing as mp

import wandb

from barlow.barlow import BarlowTwinsTrainer
from utils import utils

def get_args_parser():
    
    parser = argparse.ArgumentParser(description='Barlow Twins Training', add_help=False)
    parser.add_argument('--config-path', type=str, default='./config.yaml',
                        help='path to .yaml config file')
    return parser


def train(cfg, wandb_run):

    trainer = BarlowTwinsTrainer(cfg, wandb_run)
    print(f'Starting training for {cfg.optimizer.epochs} epochs')
    for epoch in range(cfg.optimizer.epochs):
        trainer.train_one_epoch(epoch)


def pretrain_btaudio(args=None):

    if args is None:
        parser = argparse.ArgumentParser('BT-A', parents=[get_args_parser()])
        args = parser.parse_args()

    # load training params from .ymal config file
    cfg = utils.load_yaml_config(args.config_path)
    # update config with any remaining arguments from args
    utils.update_cfg_from_args(cfg, args)

    # time stamp
    cfg.time_stamp = datetime.datetime.now().strftime('%d%m_%H-%M')

    # shared file-system initialization for torch distributed (https://pytorch.org/docs/stable/distributed.html)
    if cfg.dist_init == 'file':
        cfg.dist_url = 'file:///vol/bitbucket/jla21/proj/slurm/sharedfile'

    # update path for logging
    name = (f'{cfg.time_stamp}-model={cfg.model.encoder.type}_{cfg.model.encoder.size}-ps={cfg.model.encoder.ps[0]}x{cfg.model.encoder.ps[1]}'
            f'-maskratio={cfg.model.encoder.mask_ratio}')
    cfg.logging.log_dir = cfg.logging.log_dir.format(name)
    cfg.checkpoint.ckpt_path = os.path.join(cfg.logging.log_dir, 'models')
    os.makedirs(cfg.logging.log_dir, exist_ok=True)
    os.makedirs(cfg.checkpoint.ckpt_path, exist_ok=True)


    """set-up DDP"""
    utils.init_distributed_mode(cfg)
    # fix random seeds
    utils.fix_random_seeds(cfg.meta.seed)
    cudnn.benchmark = True

    # logging 
    print(f'Rank: {cfg.rank}')
    if cfg.rank == 0:
        wandb_run = wandb.init(
            project='BT-Audio-pretrain',
            config=cfg,
        )
    else:
        wandb_run = None
    
    # run training
    train(cfg, wandb_run)



if __name__ == "__main__":
    pretrain_btaudio()
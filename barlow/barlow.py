"""
Implementation of Barlow Twins [Zbontar et al., 2021], 
adapted from
	https://github.com/MaxLikesMath/Barlow-Twins-Pytorch
	https://github.com/facebookresearch/barlowtwins
using some code from
	https://github.com/facebookresearch/dino
	https://github.com/lucidrains/byol-pytorch
	https://github.com/yaox12/BYOL-PyTorch
"""

import torch
import torch.nn as nn
import torch.nn.functional as F 
import torch.backends.cudnn as cudnn

import time
import datetime
import numpy as np
import sys
import math
import os
from pathlib import Path
from pprint import pprint 

from utils import utils
from data_manager.audioset import AudioSetLoader
from models.mst import get_mst_model


class BarlowTwinsTrainer:

	def __init__(self, cfg, log_writer, printer=print):
		
		self.cfg = cfg
		self.log_writer = log_writer
		self.printer = printer

		# checkpoint path
		self.ckpt_path = self.cfg.checkpoint.ckpt_path.format(
			self.cfg.time_stamp, self.cfg.model.encoder.type, {}
		)

		self.construct_model()

		self.printer(f'Loaded model: \n{self.model}')
		self.printer(f'Config parameters: \n{self.cfg}')
		

	def construct_model(self):

		"""*****data loader*****"""
		self.data_loader = AudioSetLoader(self.cfg).get_loader() 

		"""*****build model*****"""
		if self.cfg.model.encoder.type == 'transformer':
			backbone = get_mst_model(
				size=self.cfg.model.encoder.size,
				patch_size=(self.cfg.model.encoder.ps[0], self.cfg.model.encoder.ps[1])
			)

			embed_dim = backbone.embed_dim
			
		if self.cfg.model.projection.sizes is None:
			self.cfg.model.projection.sizes = [embed_dim, 4*embed_dim, 4*embed_dim, 4*embed_dim]
	
		self.model = BarlowTwins(
			backbone=backbone,
			projection_sizes=self.cfg.model.projection.sizes,
			lambd=self.cfg.model.lambd,
			mask_ratio=self.cfg.model.encoder.mask_ratio,
		)
		# move model to gpu
		self.model = self.model.cuda(self.cfg.gpu)
		# synchronize batch norms
		self.model = nn.SyncBatchNorm.convert_sync_batchnorm(self.model)
		# wrap model with ddp
		self.model = nn.parallel.DistributedDataParallel(self.model, device_ids=[self.cfg.gpu])
		self.model_without_ddp = self.model.module
		
		"""*****prepare optimizer*****"""
		param_groups = utils.get_param_groups(self.model)
		if self.cfg.optimizer.type == 'adamw':
			self.optimizer = torch.optim.AdamW(param_groups)  # to use with ViTs
		# for mixed precision training
		self.fp16_scaler = None
		if self.cfg.meta.use_fp16:
			self.fp16_scaler = torch.cuda.amp.GradScaler()
		
		"""*****init schedulers*****"""
		self.lr_schedule = utils.cosine_scheduler(
			base_value=self.cfg.optimizer.base_lr * (self.cfg.optimizer.batch_size_per_gpu * utils.get_world_size() / 256.),  # linear scaling rule
			final_value=self.cfg.optimizer.final_lr,
			epochs=self.cfg.optimizer.epochs, 
			niter_per_ep=len(self.data_loader),
			warmup_epochs=self.cfg.optimizer.warmup_epochs,
		)
		self.wd_schedule = utils.cosine_scheduler(
			base_value=self.cfg.optimizer.weight_decay,
			final_value=self.cfg.optimizer.final_weight_decay,
			epochs=self.cfg.optimizer.epochs,
			niter_per_ep=len(self.data_loader),
		)
	

	def train_one_epoch(self, epoch):
		
		metric_logger = utils.MetricLogger(delimiter=" ")
		header = f'Epoch: [{epoch}/{self.cfg.optimizer.epochs}]'

		for iteration, (y1, y2) in enumerate(metric_logger.log_every(self.data_loader, 10, header)):
			# update weight decay and learning rate according to their schedule 
			iteration = len(self.data_loader) * epoch + iteration  # global training iteration
			for i, param_group in enumerate(self.optimizer.param_groups):
				param_group["lr"] = self.lr_schedule[iteration]
				if i == 0:  # only the first group is regularized
					param_group["weight_decay"] = self.wd_schedule[iteration]
			
			# move to gpu
			y1 = y1.cuda(non_blocking=True)
			y2 = y2.cuda(non_blocking=True)

			# forward passes + compute barlow twins loss
			with torch.cuda.amp.autocast(self.fp16_scaler is not None):
				loss = self.model(y1, y2)
			
			if not math.isfinite(loss.item()):
				self.printer(f"Loss is {loss.item()}, stopping training", force=True)
				sys.exit(1)

			# gradient update
			self.optimizer.zero_grad()
			if self.fp16_scaler is None:
				loss.backward()
				self.optimizer.step()
			else:
				self.fp16_scaler.scale(loss).backward()
				self.fp16_scaler.step(self.optimizer)
				self.fp16_scaler.update()

			# logging 
			torch.cuda.synchronize()
			loss_val = loss.item()
			lr = self.optimizer.param_groups[0]["lr"]
			wd = self.optimizer.param_groups[0]["weight_decay"]
			metric_logger.update(loss=loss_val)
			metric_logger.update(lr=lr)
			metric_logger.update(wd=wd)


			if self.log_writer is not None:
				self.log_writer.add_scalar('train_loss', utils.all_reduce_mean(loss_val), iteration)
				self.log_writer.add_scalar('lr', lr, iteration)
				self.log_writer.add_scalar('wd', wd, iteration)

		
		# gather the stats from all processes
		metric_logger.synchronize_between_processes()
		self.printer("Averaged stats:", metric_logger)

		# return training stats
		train_stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}

		# save checkpoint
		if epoch % self.cfg.checkpoint.save_epoch == 0:
			self.save_checkpoint(epoch, train_stats)

	
	def save_checkpoint(self, epoch, train_stats):
		save_dict = {
			'backbone': self.model.backbone.state_dict(),
			'opt': self.optimizer.state_dict(),
			'epoch': epoch + 1,
			'config': self.cfg,
		}
		if self.fp16_scaler is not None:
			save_dict['fp16_scaler'] = self.fp16_scaler.state_dict()
		
		utils.save_on_master(save_dict, self.ckpt_path.format(f'epoch-{epoch+1}'))
		
		log_stats = {
			**{f'train_{k}': v for k, v in train_stats.items()},
			'epoch': epoch,
		}
		if utils.is_main_process():
			with (Path(self.cfg.logging.log_path)).open("a") as f:
				f.write(json.dumps(log_stats) + "\n")


class BarlowTwins(nn.Module):
	
	def __init__(self, backbone, projection_sizes, lambd, mask_ratio):
		
		super().__init__()
		self.backbone = backbone
		self.lambd = lambd
		self.mask_ratio = mask_ratio
		
		# projector
		sizes = projection_sizes
		layers = []
		for i in range(len(sizes) - 2):
			layers.append(nn.Linear(sizes[i], sizes[i + 1], bias=False))
			layers.append(nn.BatchNorm1d(sizes[i + 1]))
			layers.append(nn.ReLU(inplace=True))
		layers.append(nn.Linear(sizes[-2], sizes[-1], bias=False))
		self.projector = nn.Sequential(*layers)
			
		# normalization layer for the representations z1 and z2
		self.bn = nn.BatchNorm1d(sizes[-1], affine=False)
		
	
	def forward(self, y1, y2):
		z1 = self.projector(self.backbone(y1, mask_ratio=0.))
		z2 = self.projector(self.backbone(y2, mask_ratio=self.mask_ratio))
		
		# empirical cross-correlation matrix
		c = self.bn(z1).T @ self.bn(z2)
		
		# sum the cross-correlation matrix between all gpus
		c.div_(z1.shape[0])
		torch.distributed.all_reduce(c)
		
		on_diag = torch.diagonal(c).add_(-1).pow_(2).sum()
		off_diag = off_diagonal(c).pow_(2).sum()
		loss = on_diag + self.lambd * off_diag
		return loss


def off_diagonal(x):
	# return a flattened view of the off-diagonal elements of a square matrix
	n, m = x.shape
	assert n == m
	return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()
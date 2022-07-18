import argparse
import os
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from thop import profile, clever_format
from torch.utils.data import DataLoader
from tqdm import tqdm

import utils
from model import ResNet, ViT

import torchvision

import wandb 
import datasets
import torch.distributed as dist


if torch.cuda.is_available():
	torch.backends.cudnn.benchmark = True

def off_diagonal(x):
	# return a flattened view of the off-diagonal elements of a square matrix
	n, m = x.shape
	assert n == m
	return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()

# train for one epoch to learn unique features
def train(cfg, net, data_loader, train_optimizer, wandb_run):
	net.train()
	total_loss, total_num, train_bar = 0.0, 0, tqdm(data_loader)
	for data_tuple in train_bar:
		(pos_1, pos_2), _ = data_tuple
		pos_1, pos_2 = pos_1.cuda(non_blocking=True), pos_2.cuda(non_blocking=True)
		feature_1, out_1 = net(pos_1, mask_ratio=cfg.mask_ratio)
		feature_2, out_2 = net(pos_2, mask_ratio=0.)
		# Barlow Twins
		
		# normalize the representations along the batch dimension
		out_1_norm = (out_1 - out_1.mean(dim=0)) / out_1.std(dim=0)
		out_2_norm = (out_2 - out_2.mean(dim=0)) / out_2.std(dim=0)
		
		# cross-correlation matrix
		c = torch.matmul(out_1_norm.T, out_2_norm) / batch_size
		# reduce between gpus
		if distributed:
			torch.distributed.all_reduce(c)

		# loss
		on_diag = torch.diagonal(c).add_(-1).pow_(2).sum()
		if corr_neg_one is False:
			# the loss described in the original Barlow Twin's paper
			# encouraging off_diag to be zero
			off_diag = off_diagonal(c).pow_(2).sum()
		else:
			# inspired by HSIC
			# encouraging off_diag to be negative ones
			off_diag = off_diagonal(c).add_(1).pow_(2).sum()
		loss = on_diag + lmbda * off_diag
		

		train_optimizer.zero_grad()
		loss.backward()
		train_optimizer.step()

		total_num += batch_size
		total_loss += loss.item() * batch_size
		if corr_neg_one is True:
			off_corr = -1
		else:
			off_corr = 0
		train_bar.set_description('Train Epoch: [{}/{}] Loss: {:.4f} off_corr:{} lmbda:{:.4f} bsz:{} f_dim:{} dataset: {}'.format(\
								epoch, epochs, total_loss / total_num, off_corr, lmbda, batch_size, feature_dim, dataset))

		if wandb_run is not None:
			wandb_run.log({'Loss': total_loss / total_num})

	return total_loss / total_num


# test for one epoch, use weighted knn to find the most similar images' label to assign the test image
def test(net, memory_data_loader, test_data_loader):
	net.eval()
	total_top1, total_top5, total_num, feature_bank, target_bank = 0.0, 0.0, 0, [], []
	with torch.no_grad():
		# generate feature bank and target bank
		for data_tuple in tqdm(memory_data_loader, desc='Feature extracting'):
			(data, _), target = data_tuple
			target_bank.append(target)
			feature, out = net(data.cuda(non_blocking=True))
			feature_bank.append(feature)
		# [D, N]
		feature_bank = torch.cat(feature_bank, dim=0).t().contiguous()
		# [N]
		feature_labels = torch.cat(target_bank, dim=0).contiguous().to(feature_bank.device)
		# loop test data to predict the label by weighted knn search
		test_bar = tqdm(test_data_loader)
		for data_tuple in test_bar:
			(data, _), target = data_tuple
			data, target = data.cuda(non_blocking=True), target.cuda(non_blocking=True)
			feature, out = net(data)

			total_num += data.size(0)
			# compute cos similarity between each feature vector and feature bank ---> [B, N]
			sim_matrix = torch.mm(feature, feature_bank)
			# [B, K]
			sim_weight, sim_indices = sim_matrix.topk(k=k, dim=-1)
			# [B, K]
			sim_labels = torch.gather(feature_labels.expand(data.size(0), -1), dim=-1, index=sim_indices)
			sim_weight = (sim_weight / temperature).exp()

			# counts for each class
			one_hot_label = torch.zeros(data.size(0) * k, c, device=sim_labels.device)
			# [B*K, C]
			one_hot_label = one_hot_label.scatter(dim=-1, index=sim_labels.view(-1, 1), value=1.0)
			# weighted score ---> [B, C]
			pred_scores = torch.sum(one_hot_label.view(data.size(0), -1, c) * sim_weight.unsqueeze(dim=-1), dim=1)

			pred_labels = pred_scores.argsort(dim=-1, descending=True)
			total_top1 += torch.sum((pred_labels[:, :1] == target.unsqueeze(dim=-1)).any(dim=-1).float()).item()
			total_top5 += torch.sum((pred_labels[:, :5] == target.unsqueeze(dim=-1)).any(dim=-1).float()).item()
			test_bar.set_description('Test Epoch: [{}/{}] Acc@1:{:.2f}% Acc@5:{:.2f}%'
									 .format(epoch, epochs, total_top1 / total_num * 100, total_top5 / total_num * 100))

	return total_top1 / total_num * 100, total_top5 / total_num * 100


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Train barlow twins')
	parser.add_argument('--dataset', default='fsd50k', type=str, help='Dataset: fsd50k or cifar10 or tiny_imagenet or stl10')
	parser.add_argument('--feature_dim', default=128, type=int, help='Feature dim for latent vector')
	parser.add_argument('--temperature', default=0.5, type=float, help='Temperature used in softmax')
	parser.add_argument('--k', default=200, type=int, help='Top k most similar images used to predict the label')
	parser.add_argument('--batch_size', default=128, type=int, help='Number of images in each mini-batch')
	parser.add_argument('--epochs', default=20, type=int, help='Number of sweeps over the dataset to train')
	parser.add_argument('--save_every', default=20, type=int, help='Frequency (in epochs) to save model')

	# model type 
	parser.add_argument('--model_type', default='resnet', type=str, help='Encoder: resnet or vit [tiny, small, base]')
	parser.add_argument('--latent', default='cls', type=str, help='[CLS] token or mean pool vit outputs')
	parser.add_argument('--mask_ratio', default=0., type=float, help='masking ratio')

	# for barlow twins
	parser.add_argument('--lmbda', default=0.005, type=float, help='Lambda that controls the on- and off-diagonal terms')
	parser.add_argument('--corr_neg_one', dest='corr_neg_one', action='store_true')
	parser.add_argument('--corr_zero', dest='corr_neg_one', action='store_false')
	parser.set_defaults(corr_neg_one=False)

	# for audio processing
	parser.add_argument('--unit_sec', type=float, default=0.95)
	parser.add_argument('--crop_frames', type=int, default=96)
	parser.add_argument('--sample_rate', type=int, default=16000)
	parser.add_argument('--n_fft', type=int, default=1024)
	parser.add_argument('--win_length', type=int, default=1024)
	parser.add_argument('--hop_length', type=int, default=160)
	parser.add_argument('--n_mels', type=int, default=64)
	parser.add_argument('--f_min', type=int, default=60)
	parser.add_argument('--f_max', type=int, default=7800)
	parser.add_argument('--n_norm_calc', type=int, default=10000)

	# load pre-computed lms 
	parser.add_argument('--load_lms', action='store_true', default=True)

	# distributed training 
	parser.add_argument('--distributed', action='store_true', default=False)
	

	# args parse
	args = parser.parse_args()
	dataset = args.dataset
	feature_dim, temperature, k = args.feature_dim, args.temperature, args.k
	batch_size, epochs = args.batch_size, args.epochs
	lmbda = args.lmbda
	corr_neg_one = args.corr_neg_one
	distributed = args.distributed
	model_type = args.model_type
	mask_ratio = args.mask_ratio
	save_every = args.save_every

	# distributed training 
	utils.init_distributed_mode(args)

	# wandb init
	if utils.is_main_process():
		wandb_run = wandb.init(
				project='barlow twins {}'.format(dataset),
				config=args,
				settings=wandb.Settings(start_method="fork"),
			)
	else:
		wandb_run = None
		
	# data prepare
	if dataset == 'cifar10':
		train_data = torchvision.datasets.CIFAR10(root='data', train=True, \
												  transform=utils.CifarPairTransform(train_transform = True), download=True)
		memory_data = torchvision.datasets.CIFAR10(root='data', train=True, \
												  transform=utils.CifarPairTransform(train_transform = False), download=True)
		test_data = torchvision.datasets.CIFAR10(root='data', train=False, \
												  transform=utils.CifarPairTransform(train_transform = False), download=True)
	elif dataset == 'stl10':
		train_data = torchvision.datasets.STL10(root='data', split="train+unlabeled", \
												  transform=utils.StlPairTransform(train_transform = True), download=True)
		memory_data = torchvision.datasets.STL10(root='data', split="train", \
												  transform=utils.StlPairTransform(train_transform = False), download=True)
		test_data = torchvision.datasets.STL10(root='data', split="test", \
												  transform=utils.StlPairTransform(train_transform = False), download=True)
	elif dataset == 'tiny_imagenet':
		train_data = torchvision.datasets.ImageFolder('data/tiny-imagenet-200/train', \
													  utils.TinyImageNetPairTransform(train_transform = True))
		memory_data = torchvision.datasets.ImageFolder('data/tiny-imagenet-200/train', \
													  utils.TinyImageNetPairTransform(train_transform = False))
		test_data = torchvision.datasets.ImageFolder('data/tiny-imagenet-200/val', \
													  utils.TinyImageNetPairTransform(train_transform = False))
	elif dataset == 'fsd50k':
		# fsd50k [mean, std] (lms)
		norm_stats = [-4.950, 5.855]
		train_data = datasets.FSD50K(args, train=True, transform=utils.FSD50KPairTransform(train_transform = True), norm_stats=norm_stats)
		memory_data = datasets.FSD50K(args, train=True, transform=utils.FSD50KPairTransform(train_transform = False), norm_stats=norm_stats)
		test_data = datasets.FSD50K(args, train=False, transform=utils.FSD50KPairTransform(train_transform = False), norm_stats=norm_stats)
	
	if distributed:
		train_sampler = torch.utils.data.distributed.DistributedSampler(train_data)
		memory_sampler = torch.utils.data.distributed.DistributedSampler(memory_data)
		test_sampler = torch.utils.data.distributed.DistributedSampler(test_data)
	else:
		train_sampler, memory_sampler, test_sampler = None, None, None
		
	train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=(True if train_sampler is None else False),
							  num_workers=4, pin_memory=True, sampler=train_sampler, drop_last=True)
	memory_loader = DataLoader(memory_data, batch_size=batch_size, shuffle=(True if memory_sampler is None else False),
							   num_workers=4, pin_memory=True, sampler=memory_sampler)
	test_loader = DataLoader(test_data, batch_size=batch_size, shuffle=(True if test_sampler is None else False),
							 num_workers=4, pin_memory=True, sampler=test_sampler)

	# model setup and optimizer config
	if model_type == 'resnet':
		model = ResNet(feature_dim, dataset).cuda()
	elif model_type == 'vit_base':
		model = ViT(feature_dim, dataset, size='base', latent=args.latent).cuda()

	if distributed:
		# sync batch norms
		model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
		# wrap model with ddp
		model = nn.parallel.DistributedDataParallel(
			model,
			device_ids=[args.gpu],
			output_device=args.gpu,
			)
		model_without_ddp = model.module

	if model_type == 'resnet':
		if dataset == 'cifar10':
			flops, params = profile(model, inputs=(torch.randn(1, 3, 32, 32).cuda(),))
		elif dataset == 'tiny_imagenet' or dataset == 'stl10':
			flops, params = profile(model, inputs=(torch.randn(1, 3, 64, 64).cuda(),))
		elif dataset == 'fsd50k':
			flops, params = profile(model, inputs=(torch.randn(1, 1, 64, 96).cuda(),))
		flops, params = clever_format([flops, params])
		print('# Model Params: {} FLOPs: {}'.format(params, flops))
	
	if 'vit' in model_type:
		optimizer = optim.AdamW(model.parameters(), lr=1e-4, weight_decay=0.1) 
	else:
		optimizer = optim.Adam(model.parameters(), lr=1e-3, weight_decay=1e-6)
	if dataset == 'fsd50k':
		c = memory_data.label_num
	else:
		c = len(memory_data.classes)

	# training loop
	results = {'train_loss': [], 'test_acc@1': [], 'test_acc@5': []}
	if corr_neg_one is True:
		corr_neg_one_str = 'neg_corr_'
	else:
		corr_neg_one_str = ''
	save_name_pre = '{}_maskratio{}_{}{}_{}_{}_{}'.format(model_type, mask_ratio, corr_neg_one_str, lmbda, feature_dim, batch_size, dataset)
	
	if not os.path.exists('results/{}'.format(dataset)):
		os.mkdir('results/{}'.format(dataset))
	best_acc = 0.0
	for epoch in range(1, epochs + 1):
		train_loss = train(args, model, train_loader, optimizer, wandb_run)
		if epoch % 5 == 0 and dataset != 'fsd50k':
			results['train_loss'].append(train_loss)
			test_acc_1, test_acc_5 = test(model, memory_loader, test_loader)
			results['test_acc@1'].append(test_acc_1)
			results['test_acc@5'].append(test_acc_5)
			if wandb_run is not None:
				wandb_run.log({
					'test_acc@1': test_acc_1,
					'test_acc@5': test_acc_5,
				})
			# save statistics
			data_frame = pd.DataFrame(data=results, index=range(5, epoch + 1, 5))
			data_frame.to_csv('results/{}/{}_statistics.csv'.format(dataset, save_name_pre), index_label='epoch')
			if test_acc_1 > best_acc:
				best_acc = test_acc_1
				utils.save_on_master(model.state_dict(), 'results/{}/{}_model.pth'.format(dataset, save_name_pre))
		if epoch % save_every == 0:
			utils.save_on_master(model.state_dict(), 'results/{}/{}_model_{}.pth'.format(dataset, save_name_pre, epoch))
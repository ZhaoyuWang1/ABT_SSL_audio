import argparse
import logging
import os
import torch
from torch.utils.data import DataLoader
import numpy as np
import time
import datetime
import logging

from utils import hyperparameters
from utils.torch_mlp_clf import TorchMLPClassifier
import datasets
from model import BarlowTwins


MODELS = [
	'resnet50', 'resnet50_ReGP_NRF',
	'audiontt',
	'vit_base', 'vit_small', 'vit_tiny',
	'vitc_base', 'vitc_small', 'vitc_tiny',
]


@torch.no_grad()
def get_embeddings(model, data_loader):
	model.eval()
	embs, targets = [], []
	for data, target in data_loader:
		emb = model(data.cuda(non_blocking=True))
		if isinstance(emb, list):
			emb = emb[-1]
		emb = emb.detach().cpu().numpy()
		embs.extend(emb)
		targets.extend(target.numpy())

	return np.array(embs), np.array(targets)


def eval_linear(model, train_loader, val_loader, test_loader):

	print('Extracting embeddings')
	start = time.time()
	X_train, y_train = get_embeddings(model, train_loader)
	X_val, y_val = get_embeddings(model, val_loader)
	X_test, y_test = get_embeddings(model, test_loader)
	print(f'Done\tTime elapsed = {time.time() - start:.2f}s')

	print('Fitting linear classifier')
	start = time.time()
	clf = TorchMLPClassifier(
		hidden_layer_sizes=(1024,),
		max_iter=500,
		early_stopping=True,
		n_iter_no_change=20,
		debug=True,
	)
	clf.fit(X_train, y_train, X_val=X_val, y_val=y_val)

	score = clf.score(X_test, y_test)
	print(f'Done\tTime elapsed = {time.time() - start:.2f}s')

	return score


def get_data(args):
	if args.dataset == 'fsd50k':
		return get_fsd50k(args)


def get_fsd50k(args):
	norm_stats = [-4.950, 5.855]
	eval_train_loader = DataLoader(
		datasets.FSD50K(args, split='train', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	eval_val_loader = DataLoader(
		datasets.FSD50K(args, split='val', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	eval_test_loader = DataLoader(
		datasets.FSD50K(args, split='test', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	return eval_train_loader, eval_val_loader, eval_test_loader


def log_print(msg):
	logger.info(msg)
	print(msg)


if __name__ == '__main__':

	parser = argparse.ArgumentParser(description='Linear eval', parents=hyperparameters.get_hyperparameters())
	parser.add_argument('--model_file_path', type=str, required=True)
	args = parser.parse_args()


	log_dir = f"logs/linear_eval/{args.dataset}/{args.name}/"
	os.makedirs(log_dir, exist_ok=True)
	timestamp = datetime.datetime.now().strftime('%H:%M_%h%d')
	log_path = os.path.join(log_dir, f"{timestamp}.log")
	logger = logging.getLogger()
	logger.setLevel(logging.DEBUG)  # Setup the root logger
	logger.addHandler(logging.FileHandler(log_path, mode="w"))

	# Get data
	eval_train_loader, eval_val_loader, eval_test_loader = get_data(args)

	# Load model
	model = BarlowTwins(args).encoder
	sd = torch.load(args.model_file_path, map_location='cpu')
	sd_try = {k.replace("encoder.", "", 1): v for k, v in sd.items() if "encoder." in k}
	if len(sd_try) > 0:
		sd = sd_try
	model.load_state_dict(sd, strict=True)
	model = model.cuda()
	model.eval()

	# Linear evaluation 
	score = eval_linear(model, eval_train_loader, eval_val_loader, eval_test_loader)
	log_print(f'Score: {score}')
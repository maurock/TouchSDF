import torch.nn as nn
import torch
import model.sdf_model as sdf_model
import torch.optim as optim
import data_making.dataset as dataset
from torch.utils.data import random_split
from torch.utils.data import DataLoader
import argparse
import results.runs as runs
from utils.utils import SDFLoss, SDFLoss_multishape
import os
from datetime import datetime
import numpy as np
import time
from utils import utils
import results

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
if device=="cuda:0":
    print(torch.cuda.get_device_name(0))

class Trainer():
    def __init__(self, args):
        self.args = args

    def __call__(self):
        # directories
        self.timestamp_run = datetime.now().strftime('%d_%m_%H%M%S')   # timestamp to use for logging data
        self.runs_dir = os.path.dirname(runs.__file__)               # directory fo all runs
        self.run_dir = os.path.join(self.runs_dir, self.timestamp_run)  # directory for this run
        if not os.path.exists(self.run_dir):
            os.makedirs(self.run_dir)

        # calculate num objects in samples_dictionary, wich is the number of keys
        samples_dict_path = os.path.join(os.path.dirname(results.__file__), 'samples_dict.npy')
        samples_dict = np.load(samples_dict_path, allow_pickle=True).item()

        # instantiate model and optimisers
        self.model = sdf_model.SDFModelMulti().double().to(device)
        self.optimizer_model = optim.SGD(self.model.parameters(), lr=self.args.lr, weight_decay=0)
        # generate a unique random latent code for each shape
        self.latent_codes, self.dict_latent_codes = utils.generate_latent_codes(self.args.latent_size, samples_dict)
        self.optimizer_latent = optim.SGD([self.latent_codes], lr=self.args.lr, weight_decay=0)

        # get data
        train_loader, val_loader = self.get_loaders()
        self.results = {
            'train':  {'loss': [], 'latent_codes': []},
            'val':    {'loss': []}
        }

        start = time.time()
        for epoch in range(self.args.epochs):
            print(f'============================ Epoch {epoch} ============================')
            self.epoch = epoch
            avg_train_loss = self.train(train_loader)
            self.results['train']['loss'].append(avg_train_loss)
            self.results['train']['latent_codes'].append(self.latent_codes.detach().cpu().numpy())
            self.latent_codes
            with torch.no_grad():
                avg_val_loss = self.validate(val_loader)
                self.results['val']['loss'].append(avg_val_loss)
            
            np.save(os.path.join(self.run_dir, 'results.npy'), self.results)
            torch.save(self.model.state_dict(), os.path.join(self.run_dir, 'weights.pt'))
            
        end = time.time()
        print(f'Time elapsed: {end - start} s')

    def get_loaders(self):
        data = dataset.SDFDataset()
        train_size = int(0.8 * len(data))
        val_size = len(data) - train_size
        train_data, val_data = random_split(data, [train_size, val_size])
        train_loader = DataLoader(
                train_data,
                batch_size=self.args.batch_size,
                shuffle=True,
                drop_last=True
            )
        val_loader = DataLoader(
            val_data,
            batch_size=self.args.batch_size,
            shuffle=False,
            drop_last=True
            )
        return train_loader, val_loader
   
    def generate_xy(self, batch):
        """
        Combine latent code and coordinates.
        Return:
            - x: latent codes + coordinates, torch tensor shape (batch_size, latent_size + 3)
            - y: ground truth sdf, shape (batch_size, 1)
            - latent_codes_indexes_batch: all latent class indexes per sample, shape (batch size, 1).
                                            e.g. [[2], [2], [1], ..] eaning the batch contains the 2nd, 2nd, 1st latent code
            - latent_batch_codes: all latent codes per sample, shape (batch_size, latent_size)
        Return ground truth as y, and the latent codes for this batch.
        """
        latent_classes_batch = batch[0][:, 0].view(-1, 1)               # shape (batch_size, 1)
        coords = batch[0][:, 1:]                                  # shape (batch_size, 3)
        latent_codes_indexes_batch = torch.tensor(
                [self.dict_latent_codes[str(int(latent_class))] for latent_class in latent_classes_batch],
                dtype=torch.int64
            ).to(device)
        latent_codes_batch = self.latent_codes[latent_codes_indexes_batch]    # shape (batch_size, 128)
        x = torch.hstack((latent_codes_batch, coords))                  # shape (batch_size, 131)
        y = batch[1].view(-1, 1)     # (batch_size, 1)
        return x, y, latent_codes_indexes_batch, latent_codes_batch
    
    def train(self, train_loader):
        total_loss = 0.0
        iterations = 0.0
        self.model.train()
        for batch in train_loader:
            # batch[0]: [class, x, y, z], shape: (batch_size, 4)
            # batch[1]: [sdf], shape: (batch size)
            iterations += 1.0
            #batch_size = self.args.batch_size
            self.optimizer_model.zero_grad()
            self.optimizer_latent.zero_grad()
            x, y, latent_codes_indexes_batch, latent_codes_batch = self.generate_xy(batch)
            predictions = self.model(x)  # (batch_size, 1)
            loss_value = SDFLoss_multishape(y, predictions, latent_codes_batch)
            loss_value.backward()       
            # set gradients of latent codes that were not in the batch to 0     
            unique_latent_indexes_batch = torch.unique(latent_codes_indexes_batch, dim=0).to(device)
            for i in range(0, self.latent_codes.shape[0]):
                if i not in unique_latent_indexes_batch:
                    self.latent_codes.grad[i, :].data.zero_()              
            self.optimizer_latent.step()
            self.optimizer_model.step()
            total_loss += loss_value.data.cpu().numpy()      
        avg_train_loss = total_loss/iterations
        print(f'Training: loss {avg_train_loss}')
        return avg_train_loss

    def validate(self, val_loader):
        total_loss = 0.0
        iterations = 0.0
        self.model.train()
        for batch in val_loader:
            # batch[0]: [class, x, y, z], shape: (batch_size, 4)
            # batch[1]: [sdf], shape: (batch size)
            iterations += 1.0            
            x, y, latent_codes_indexes_batch, latent_codes_batch = self.generate_xy(batch)
            unique_latent_indexes_batch = torch.unique(latent_codes_indexes_batch, dim=0).to(device)
            predictions = self.model(x)  # (batch_size, 1)
            loss_value = SDFLoss_multishape(y, predictions, latent_codes_batch)          
            total_loss += loss_value.data.cpu().numpy()      
        avg_val_loss = total_loss/iterations
        print(f'Validation: loss {avg_val_loss}')
        return avg_val_loss

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed", type=int, default=42, help="Setting for the random seed."
    )
    parser.add_argument(
        "--epochs", type=int, default=1000, help="Number of epochs to use."
    )
    parser.add_argument(
        "--lr", type=float, default=0.0001, help="Initial learning rate."
    )
    parser.add_argument(
        "--batch_size", type=int, default=1000, help="Size of the batch."
    )
    parser.add_argument(
        "--latent_size", type=int, default=128, help="Size of the batch."
    )
    args = parser.parse_args()
    trainer = Trainer(args)
    trainer()
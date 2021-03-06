import torch
import torch.optim as optim
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, random_split
from torchvision import transforms
from torchvision.utils import make_grid
from tensorboardX import SummaryWriter
import argparse
import datetime
import os
import random
from datasets import FivekDataset
from models import ConditionalCAN, ConditionalUNet
from torch_utils import JoinedDataLoader
from loss import ColorSSIM, NimaLoss

parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', type=int, default=8, help='input batch size')
parser.add_argument('--epochs', type=int, default=100, help='number of epochs to train for')
parser.add_argument('--lr', type=float, default=2e-4, help='learning rate')
parser.add_argument('--cuda', action='store_true', help='enables cuda')
parser.add_argument('--cuda_idx', type=int, default=1, help='cuda device id')
parser.add_argument('--manual_seed', type=int, help='manual seed')
parser.add_argument('--logdir', default='log', help='logdir for tensorboard')
parser.add_argument('--run_tag', default='', help='tags for the current run')
parser.add_argument('--checkpoint_every', default=10, help='number of epochs after which saving checkpoints')
parser.add_argument('--checkpoint_dir', default="checkpoints", help='directory for the checkpoints')
parser.add_argument('--final_dir', default="final_models", help='directory for the final models')
parser.add_argument('--model_type', default='can32', choices=['can32','unet'], help='type of model to use')
parser.add_argument('--loss', default='mse', choices=['mse','mae','l1nima','l2nima','l1ssim','colorssim'], help='loss to be used')
parser.add_argument('--gamma', default=0.001, type=float, help='gamma to be used only in case of Nima Loss')
parser.add_argument('--data_path', default='/home/iacv3_1/fivek', help='path of the base directory of the dataset')
opt = parser.parse_args()

#Create writer for tensorboard
date = datetime.datetime.now().strftime("%d-%m-%y_%H:%M")
run_name = "{}_{}".format(opt.run_tag,date) if opt.run_tag != '' else date
log_dir_name = os.path.join(opt.logdir, run_name)
writer = SummaryWriter(log_dir_name)
writer.add_text('Options', str(opt), 0)
print(opt)

if opt.manual_seed is None:
    opt.manual_seed = random.randint(1, 10000)
print("Random Seed: ", opt.manual_seed)
random.seed(opt.manual_seed)
torch.manual_seed(opt.manual_seed)

os.makedirs(opt.checkpoint_dir, exist_ok=True)

if torch.cuda.is_available() and not opt.cuda:
	print("You should run with CUDA.")
device = torch.device("cuda:"+str(opt.cuda_idx) if opt.cuda else "cpu")

landscape_transform = transforms.Compose([
                              transforms.Resize((332, 500)),
                              transforms.ToTensor(),
                              transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)) #normalize in [-1,1]
                             ])
portrait_transform = transforms.Compose([
                              transforms.Resize((500, 332)),
                              transforms.ToTensor(),
                              transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)) #normalize in [-1,1]
                             ])
landscape_dataset = FivekDataset(opt.data_path, expert_idx=2, transform=landscape_transform, filter_ratio="landscape", use_features=True)
portrait_dataset = FivekDataset(opt.data_path, expert_idx=2, transform=portrait_transform, filter_ratio="portrait", use_features=True)


train_size = int(0.8 * len(landscape_dataset))
test_size = len(landscape_dataset) - train_size
train_landscape_dataset, test_landscape_dataset = random_split(landscape_dataset, [train_size, test_size])

train_size = int(0.8 * len(portrait_dataset))
test_size = len(portrait_dataset) - train_size
train_portrait_dataset, test_portrait_dataset = random_split(portrait_dataset, [train_size, test_size])

train_landscape_loader = DataLoader(train_landscape_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=2)
train_portrait_loader = DataLoader(train_portrait_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=2)
train_loader = JoinedDataLoader(train_landscape_loader, train_portrait_loader)

test_landscape_loader = DataLoader(test_landscape_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=2)
test_portrait_loader = DataLoader(test_portrait_dataset, batch_size=opt.batch_size, shuffle=True, num_workers=2)
test_loader = JoinedDataLoader(test_landscape_loader, test_portrait_loader)


if opt.model_type == 'can32':
  model = ConditionalCAN()
if opt.model_type == 'unet':
  model = ConditionalUNet()
assert model
model = model.to(device)

if opt.loss == "mse":
  criterion = nn.MSELoss()
if opt.loss == "mae":
  criterion = nn.L1Loss()
if opt.loss == "l1nima":
  criterion = NimaLoss(device,opt.gamma,nn.L1Loss())
if opt.loss == "l2nima":
  criterion = NimaLoss(device,opt.gamma,nn.MSELoss())
if opt.loss == "l1ssim":
  criterion = ColorSSIM(device,'l1')
if opt.loss == "colorssim":
  criterion = ColorSSIM(device)
assert criterion
criterion = criterion.to(device)

optimizer = optim.Adam(model.parameters(), lr=opt.lr)

#Select random idxs for displaying
test_idxs = random.sample(range(len(test_landscape_dataset)), 3)
for epoch in range(opt.epochs):
    model.train()
    cumulative_loss = 0.0
    for i, (im_o, im_t, feats) in enumerate(train_loader):
        im_o, im_t = im_o.to(device), im_t.to(device)
        feats = [f.to(device) for f in feats]
        optimizer.zero_grad()

        output = model(im_o, feats)
        loss = criterion(output, im_t)
        loss.backward()
        optimizer.step()
        cumulative_loss += loss.item()
        print('[Epoch %d, Batch %2d] loss: %.3f' %
         (epoch + 1, i + 1, cumulative_loss / (i+1)), end="\r")
    #Evaluate
    writer.add_scalar('Train Error', cumulative_loss / len(train_loader), epoch)
    #Checkpointing
    if (epoch+1) % opt.checkpoint_every == 0:
        torch.save(model.state_dict(), os.path.join(opt.checkpoint_dir, "{}_epoch{}.pt".format(opt.run_tag, epoch+1)))

    #Model evaluation
    model.eval()
    test_loss = []
    for i, (im_o, im_t, feats) in enumerate(test_loader):
      im_o, im_t = im_o.to(device), im_t.to(device)
      feats = [f.to(device) for f in feats]
      with torch.no_grad():
        output = model(im_o, feats)
        test_loss.append(criterion(output, im_t).item())
    avg_loss = sum(test_loss)/len(test_loss)
    writer.add_scalar('Test Error', avg_loss, epoch)

    for idx in test_idxs:
      original, actual, feats = test_landscape_dataset[idx]
      original, actual = original.unsqueeze(0).to(device), actual.unsqueeze(0).to(device)
      feats = [f.unsqueeze(0).to(device) for f in feats]
      estimated = model(original, feats)
      images = torch.cat((original, estimated, actual))
      grid = make_grid(images, nrow=1, normalize=True, range=(-1,1))
      writer.add_image('{}:Original|Estimated|Actual'.format(idx), grid, epoch)

print("Training Finished")
torch.save(model.state_dict(), os.path.join(opt.final_dir, "{}_final.pt".format(opt.run_tag)))

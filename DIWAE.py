import utils, torch, time, os, pickle
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from torchvision import datasets, transforms
#from torch.distributions.distribution import Distribution

from utils import log_likelihood_samples_mean_sigma, prior_z, log_mean_exp

torch.manual_seed(1)
torch.cuda.manual_seed_all(1)



class DIWAE(nn.Module):

    def __init__(self, args):
        super(DIWAE, self).__init__()
        # parameters
        self.epoch = args.epoch
        self.sample_num = 16
        self.batch_size = args.batch_size
        self.save_dir = args.save_dir
        self.result_dir = args.result_dir
        self.dataset = args.dataset
        self.log_dir = args.log_dir
        self.gpu_mode = args.gpu_mode
        self.model_name = args.model_type
        self.z_dim = args.z_dim
        self.num_sam= args.num_sam
        self.arch_type = args.arch_type

        # networks init
        self.encoder_init()
        self.decoder_init()

        if self.gpu_mode:
            self.reconstruction_function = nn.BCELoss().cuda()
        else:
            self.reconstruction_function = nn.BCELoss()

        self.reconstruction_function.size_average = False

        # fixed noise
        if self.gpu_mode:
            self.sample_z_ = Variable(torch.randn((self.batch_size, 1, self.z_dim)).cuda(), volatile=True)
        else:
            self.sample_z_ = Variable(torch.randn((self.batch_size, 1, self.z_dim)), volatile=True)


    def elbo(self, recon_x, x, mu, logvar):

        N, M, C, iw, ih = recon_x.shape
        recon_x = recon_x.view([N*M,C,iw,ih])
        BCE = self.reconstruction_function(recon_x, x) / (N*M)
        KLD_element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
        KLD = torch.mean(torch.sum(KLD_element, dim=2).mul_(-0.5))

        return BCE + KLD


    def log_likelihood_estimate(self, recon_x, x_tile, Z, mu, logvar):

        bce = x_tile * torch.log(recon_x) + (1. - x_tile) * torch.log(1 - recon_x)
        log_p_x_z   =  torch.sum(torch.sum(torch.sum(bce, dim=4), dim=3), dim=2)

        log_q_z_x = log_likelihood_samples_mean_sigma(Z, mu, logvar, dim=2)
        log_p_z   = prior_z(Z, dim=2)

        log_ws              = log_p_x_z - log_q_z_x + log_p_z
        #log_ws_minus_max    = log_ws - torch.max(log_ws, dim=1, keepdim=True)[0]
        #ws                  = torch.exp(log_ws_minus_max)
        #normalized_ws       = ws / torch.sum(ws, dim=1, keepdim=True)
        return torch.mean(torch.squeeze(log_mean_exp(log_ws, dim=1)), dim=0)


    def loss_function(self, recon_x, x, Z, mu, logvar):

        N, C, iw, ih = x.shape
        x_tile = x.repeat(self.num_sam,1,1,1,1).permute(1,0,2,3,4)
        J = - self.log_likelihood_estimate(recon_x, x_tile, Z, mu, logvar)
        J_low = self.elbo(recon_x, x_tile, mu, logvar)
        return J, J_low


    def decoder_init(self):
        # Architecture : FC1024_BR-FC7x7x128_BR-(64)4dc2s_BR-(1)4dc2s_S
        self.input_height = 28
        self.input_width = 28
        self.output_dim = 1
    
        if self.arch_type == 'conv':
            self.dec_layer1 = nn.Sequential(
                nn.Linear(self.z_dim, 128 * (self.input_height // 4) * (self.input_width // 4)),
                nn.BatchNorm1d(128 * (self.input_height // 4) * (self.input_width // 4)),
                nn.ReLU(),
            )

            self.dec_layer2 = nn.Sequential(
                nn.ConvTranspose2d(128, 64, 4, 2, 1),
                nn.BatchNorm2d(64),
                nn.ReLU(),
                nn.ConvTranspose2d(64, self.output_dim, 4, 2, 1),
                nn.Sigmoid(),
            )
        else:

            self.dec_layer1 = nn.Sequential(
                nn.Linear(self.z_dim, self.z_dim*4),
                nn.BatchNorm1d(self.z_dim*4),
                nn.Tanh(),
            )

            self.dec_layer2 = nn.Sequential(
                nn.Linear(self.z_dim*4, self.input_height * self.input_width),
                nn.Sigmoid(),
            )

        utils.initialize_weights(self)
   

    def encoder_init(self):
        # Architecture : (64)4c2s-(128)4c2s_BL-FC1024_BL-FC1_S
        self.input_height = 28
        self.input_width = 28
        self.input_dim = 1
    
        if self.arch_type == 'conv':
            self.enc_layer1 = nn.Sequential(
                nn.Conv2d(self.input_dim, 64, 4, 2, 1),
                nn.LeakyReLU(0.2),
                nn.Conv2d(64, 128, 4, 2, 1),
                nn.BatchNorm2d(128),
                nn.LeakyReLU(0.2),
            )
            self.mu_fc = nn.Sequential(
                nn.Linear(128 * (self.input_height // 4) * (self.input_width // 4), self.z_dim),
            )
    
            self.sigma_fc = nn.Sequential(
                nn.Linear(128 * (self.input_height // 4) * (self.input_width // 4), self.z_dim),
            )
        else:

            self.enc_layer1 = nn.Sequential(
                nn.Linear(self.input_height*self.input_width, self.z_dim*4),
                nn.BatchNorm1d(self.z_dim*4),
                nn.ReLU(),
                nn.Linear(self.z_dim*4, self.z_dim*4),
                nn.BatchNorm1d(self.z_dim*4),
                nn.Tanh()
                #nn.ReLU(),
            )

            self.mu_fc = nn.Sequential(
                nn.Linear(self.z_dim*4, self.z_dim),
            )
    
            self.sigma_fc = nn.Sequential(
                nn.Linear(self.z_dim*4, self.z_dim),
            )

    
        utils.initialize_weights(self)


    def encode(self, x):

        if self.arch_type == 'conv':
            x = self.enc_layer1(x)
            x = x.view(-1, 128 * (self.input_height // 4) * (self.input_width // 4))
        else:
            x = x.view([-1, self.input_height * self.input_width * self.input_dim])
            x = self.enc_layer1(x)

        mean  = self.mu_fc(x)
        sigma = self.sigma_fc(x)
        
        return mean, sigma


    def sample(self, mu, logvar):

        std = torch.exp(logvar)
        if self.gpu_mode :
            eps = torch.cuda.FloatTensor(std.size()).normal_()
        else:
            eps = torch.FloatTensor(std.size()).normal_()
        eps = Variable(eps)

        return eps.mul(std).add_(mu)


    def get_latent_sample(self, x):

        mu, logvar = self.encode(x)
        z = self.sample(mu, logvar)
        return z


    def decode(self, z):

        N,T,D = z.size()
        x = self.dec_layer1(z.view([-1,D]))

        if self.arch_type == 'conv':
            x = x.view(-1, 128, (self.input_height // 4), (self.input_width // 4))
            x = self.dec_layer2(x)
        else:
            x = self.dec_layer2(x)
            x = x.view(-1, 1, self.input_height, self.input_width)
        return x.view([N,T,-1,self.input_width, self.input_height])

    
    def forward(self, x):

        if self.model_name == 'DIWAE':
            if self.gpu_mode:
                eps = torch.cuda.FloatTensor(x.size()).normal_(std=0.05)
            else:
                eps = torch.FloatTensor(x.size()).normal_(std=0.05)
            eps = Variable(eps) # requires_grad=False
            x = x.add_(eps)

        mu, logvar = self.encode(x)
        mu  = mu.repeat(self.num_sam,1,1).permute(1,0,2)
        logvar = logvar.repeat(self.num_sam,1,1).permute(1,0,2)

        z = self.sample(mu, logvar)
        res = self.decode(z)
        return res, mu, logvar, z



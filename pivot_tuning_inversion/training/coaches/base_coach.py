import abc
import os
import pickle
from argparse import Namespace
import os.path
import torch
from torchvision import transforms
from lpips import LPIPS

from configs import pti_hparams, path_configs
from pivot_tuning_inversion.training.projectors import w_projector
from pivot_tuning_inversion.criteria.localitly_regulizer import Space_Regulizer
from pivot_tuning_inversion.criteria import l2_loss
from pivot_tuning_inversion.e4e.psp import pSp
from pivot_tuning_inversion.utils.models_utils import toogle_grad


class BaseCoach:
    def __init__(self, data_loader, device=None, generator=None, mode='w'):

        self.data_loader = data_loader
        self.w_pivots = {}
        self.image_counter = 0
        self.device = device
        self.mode = mode

        assert generator is not None
        self.G = generator
        self.original_G = generator

        if pti_hparams.first_inv_type == 'w+':
            self.initilize_e4e(device=device)

        self.e4e_image_transform = transforms.Compose([
            transforms.ToPILImage(),
            transforms.Resize((256, 256)),
            transforms.ToTensor(),
            transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])])

        # Initialize loss
        self.lpips_loss = LPIPS(net=pti_hparams.lpips_type).to(device).eval()

        self.restart_training()


    def restart_training(self):

        toogle_grad(self.G, True)
        self.space_regulizer = Space_Regulizer(self.original_G, self.lpips_loss, self.device)
        self.optimizer = self.configure_optimizers(mode=self.mode)

    def get_inversion(self, w_path_dir, image_name, image):

        w_pivot = None
        w_pivot = self.calc_inversions(image, image_name)

        w_pivot = w_pivot.to(self.device)
        return w_pivot

    def calc_inversions(self, image, image_name):

        if pti_hparams.first_inv_type == 'w+':
            w = self.get_e4e_inversion(image)

        else: # TODO : use projector.py
            id_image = torch.squeeze((image.to(self.device) + 1) / 2) * 255
            w = w_projector.project(self.G, id_image, device=torch.device(self.device), w_avg_samples=600,
                                    num_steps=pti_hparams.first_inv_steps, w_name=image_name,
                                    )

        return w

    @abc.abstractmethod
    def train(self):
        pass

    def configure_optimizers(self, mode='w'):
        if mode == 's':
            names, params, new_names, new_params = list(), list(), list(), list()
            for name_, param_ in self.G.named_parameters():
                names.append(name_)
                params.append(param_)
            for name_, param_ in zip(names, params):
                if 'affine' not in name_:
                    new_names.append(name_)
                    new_params.append(param_)
            optimizer = torch.optim.Adam(new_params, lr=pti_hparams.pti_learning_rate)

        elif mode == 'w':
            optimizer = torch.optim.Adam(self.G.parameters(), lr=pti_hparams.pti_learning_rate)
        return optimizer

    def calc_loss(self, generated_images, real_images, log_name, new_G, use_ball_holder, w_batch):
        loss = 0.0
        if pti_hparams.pt_l2_lambda > 0:
            l2_loss_val = l2_loss.l2_loss(generated_images, real_images)
            loss += l2_loss_val * pti_hparams.pt_l2_lambda
        if pti_hparams.pt_lpips_lambda > 0:
            loss_lpips = self.lpips_loss(generated_images, real_images)
            loss_lpips = torch.squeeze(loss_lpips)
            loss += loss_lpips * pti_hparams.pt_lpips_lambda

        if use_ball_holder and pti_hparams.use_locality_regularization:
            ball_holder_loss_val = self.space_regulizer.space_regulizer_loss(new_G, w_batch)
            loss += ball_holder_loss_val

        return loss, l2_loss_val, loss_lpips

    def forward(self, w):
        generated_images = self.G.synthesis(w, noise_mode='const', force_fp32=True)

        return generated_images

    def initilize_e4e(self, device=None):
        ckpt = torch.load(path_configs.e4e, map_location='cpu')
        opts = ckpt['opts']
        if device is not None:
            opts['device'] = device
        opts['batch_size'] = pti_hparams.train_batch_size
        opts['checkpoint_path'] = path_configs.e4e
        opts = Namespace(**opts)
        self.e4e_inversion_net = pSp(opts)
        self.e4e_inversion_net.eval()
        self.e4e_inversion_net = self.e4e_inversion_net.to(opts.device)
        toogle_grad(self.e4e_inversion_net, False)

    def get_e4e_inversion(self, image):
        image = (image + 1) / 2
        new_image = self.e4e_image_transform(image[0]).to(self.e4e_inversion_net.opts.device)
        _, w = self.e4e_inversion_net(new_image.unsqueeze(0), randomize_noise=False, return_latents=True, resize=False,
                                      input_code=False)
        return w

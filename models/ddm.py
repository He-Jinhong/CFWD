import os
import time
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import utils
from models.unet import DiffusionUNet
from pytorch_msssim import ssim
from models.mods import HFRM
from math import sqrt
import torch.nn as nn
from torch.nn import functional as F
import torch.optim
import clip_loss as clip_loss
from collections import OrderedDict
from torch.utils.tensorboard import SummaryWriter
import clip
from models.Dwt_Fre import DWT, IWT, get_Fre
from tqdm import tqdm

device = "cuda" if torch.cuda.is_available() else "cpu"
print(device)
# load clip
c_model, preprocess = clip.load( "/home/ubuntu/Low-image/Diffusion-Low-Light-main/clip_model/ViT-B-32.pt", device=torch.device("cpu"))  # ViT-B/32
c_model.to(device)


def data_transform(X):
    return 2 * X - 1.0


def inverse_data_transform(X):
    return torch.clamp((X + 1.0) / 2.0, 0.0, 1.0)


class TextEncoder(nn.Module):
    def __init__(self, clip_model):
        super().__init__()
        self.transformer = clip_model.transformer
        self.positional_embedding = clip_model.positional_embedding
        self.ln_final = clip_model.ln_final
        self.text_projection = clip_model.text_projection
        self.dtype = clip_model.dtype

    def forward(self, prompts, tokenized_prompts):

        x = prompts + self.positional_embedding.type(self.dtype)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_final(x).type(self.dtype)
        x = x[torch.arange(x.shape[0]), tokenized_prompts.argmax(
            dim=-1)] @ self.text_projection
        return x


class Prompts(nn.Module):
    def __init__(self, initials=None):
        super(Prompts, self).__init__()
        self.text_encoder = TextEncoder(c_model)
        if isinstance(initials, list):
            text = clip.tokenize(initials).cuda()
            print(text)
            self.embedding_prompt = nn.Parameter(
                c_model.token_embedding(text).requires_grad_()).cuda()
        elif isinstance(initials, str):
            prompt_path = initials
            state_dict = torch.load(prompt_path)
            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = k[7:]  # remove `module.`
                new_state_dict[name] = v
            self.embedding_prompt = nn.Parameter(
                new_state_dict['embedding_prompt']).cuda()
            self.embedding_prompt.requires_grad = True
        else:
            self.embedding_prompt = torch.nn.init.xavier_normal_(nn.Parameter(
                c_model.token_embedding([" ".join(["X"]*16), " ".join(["X"]*16)]).requires_grad_())).cuda()

    def forward(self, tensor, flag=1):

        tokenized_prompts = torch.cat(
            [clip.tokenize(p) for p in [" ".join(["X"]*16)]])
        text_features = self.text_encoder(
            self.embedding_prompt, tokenized_prompts)
        for i in range(tensor.shape[0]):
            image_features = tensor[i]
            nor = torch.norm(text_features, dim=-1, keepdim=True)
            if flag == 0:
                similarity = (100.0 * image_features @
                              (text_features/nor).T)  # .softmax(dim=-1)
                if (i == 0):
                    probs = similarity
                else:
                    probs = torch.cat([probs, similarity], dim=0)
            else:
                similarity = (100.0 * image_features @
                              (text_features/nor).T).softmax(dim=-1)  # /nor
                if (i == 0):
                    probs = similarity[:, 0]
                else:
                    probs = torch.cat([probs, similarity[:, 0]], dim=0)
        return probs


class TVLoss(nn.Module):
    def __init__(self, TVLoss_weight=1):
        super(TVLoss, self).__init__()
        self.TVLoss_weight = TVLoss_weight

    def forward(self, x):
        batch_size = x.size()[0]
        h_x = x.size()[2]
        w_x = x.size()[3]
        count_h = self._tensor_size(x[:, :, 1:, :])
        count_w = self._tensor_size(x[:, :, :, 1:])
        h_tv = torch.pow((x[:, :, 1:, :] - x[:, :, :h_x - 1, :]), 2).sum()
        w_tv = torch.pow((x[:, :, :, 1:] - x[:, :, :, :w_x - 1]), 2).sum()
        return self.TVLoss_weight * 2 * (h_tv / count_h + w_tv / count_w) / batch_size

    def _tensor_size(self, t):
        return t.size()[1] * t.size()[2] * t.size()[3]


class EMAHelper(object):
    def __init__(self, mu=0.9999):
        self.mu = mu
        self.shadow = {}

    def register(self, module):
        if isinstance(module, nn.DataParallel):
            module = module.module
        for name, param in module.named_parameters():
            if param.requires_grad:
                self.shadow[name] = param.data.clone()

    def update(self, module):
        if isinstance(module, nn.DataParallel):
            module = module.module
        for name, param in module.named_parameters():
            if param.requires_grad:
                self.shadow[name].data = (
                    1. - self.mu) * param.data + self.mu * self.shadow[name].data

    def ema(self, module):
        if isinstance(module, nn.DataParallel):
            module = module.module
        for name, param in module.named_parameters():
            if param.requires_grad:
                param.data.copy_(self.shadow[name].data)

    def ema_copy(self, module):
        if isinstance(module, nn.DataParallel):
            inner_module = module.module
            module_copy = type(inner_module)(
                inner_module.config).to(inner_module.config.device)
            module_copy.load_state_dict(inner_module.state_dict())
            module_copy = nn.DataParallel(module_copy)
        else:
            module_copy = type(module)(module.config).to(module.config.device)
            module_copy.load_state_dict(module.state_dict())
        self.ema(module_copy)
        return module_copy

    def state_dict(self):
        return self.shadow

    def load_state_dict(self, state_dict):
        self.shadow = state_dict


def get_beta_schedule(beta_schedule, *, beta_start, beta_end, num_diffusion_timesteps):
    def sigmoid(x):
        return 1 / (np.exp(-x) + 1)

    if beta_schedule == "quad":
        betas = (np.linspace(beta_start ** 0.5, beta_end ** 0.5,
                 num_diffusion_timesteps, dtype=np.float64) ** 2)
    elif beta_schedule == "linear":
        betas = np.linspace(beta_start, beta_end,
                            num_diffusion_timesteps, dtype=np.float64)
    elif beta_schedule == "const":
        betas = beta_end * np.ones(num_diffusion_timesteps, dtype=np.float64)
    elif beta_schedule == "jsd":  # 1/T, 1/(T-1), 1/(T-2), ..., 1
        betas = 1.0 / np.linspace(num_diffusion_timesteps,
                                  1, num_diffusion_timesteps, dtype=np.float64)
    elif beta_schedule == "sigmoid":
        betas = np.linspace(-6, 6, num_diffusion_timesteps)
        betas = sigmoid(betas) * (beta_end - beta_start) + beta_start
    else:
        raise NotImplementedError(beta_schedule)
    assert betas.shape == (num_diffusion_timesteps,)
    return betas


class Net(nn.Module):
    def __init__(self, args, config):
        super(Net, self).__init__()

        self.args = args
        self.config = config
        self.device = config.device

        self.high_enhance0 = HFRM(in_channels=3, out_channels=64)
        self.high_enhance1 = HFRM(in_channels=3, out_channels=64)
        self.Unet = DiffusionUNet(config)

        betas = get_beta_schedule(
            beta_schedule=config.diffusion.beta_schedule,
            beta_start=config.diffusion.beta_start,
            beta_end=config.diffusion.beta_end,
            num_diffusion_timesteps=config.diffusion.num_diffusion_timesteps,
        )

        self.betas = torch.from_numpy(betas).float()
        self.num_timesteps = self.betas.shape[0]

    @staticmethod
    def compute_alpha(beta, t):
        beta = torch.cat([torch.zeros(1).to(beta.device), beta], dim=0)
        a = (1 - beta).cumprod(dim=0).index_select(0, t + 1).view(-1, 1, 1, 1)
        return a

    def sample_training(self, x_cond, b, eta=0.):
        skip = self.config.diffusion.num_diffusion_timesteps // self.args.sampling_timesteps
        seq = range(0, self.config.diffusion.num_diffusion_timesteps, skip)
        n, c, h, w = x_cond.shape
        seq_next = [-1] + list(seq[:-1])
        x = torch.randn(n, c, h, w, device=self.device)
        xs = [x]
        for i, j in zip(reversed(seq), reversed(seq_next)):
            t = (torch.ones(n) * i).to(x.device)
            next_t = (torch.ones(n) * j).to(x.device)
            at = self.compute_alpha(b, t.long())
            at_next = self.compute_alpha(b, next_t.long())
            xt = xs[-1].to(x.device)

            et = self.Unet(torch.cat([x_cond, xt], dim=1), t)
            x0_t = (xt - et * (1 - at).sqrt()) / at.sqrt()

            c1 = eta * ((1 - at / at_next) * (1 - at_next) / (1 - at)).sqrt()
            c2 = ((1 - at_next) - c1 ** 2).sqrt()
            xt_next = at_next.sqrt() * x0_t + c1 * torch.randn_like(x) + c2 * et
            xs.append(xt_next.to(x.device))

        # return xs[-1]
        return xs

    def forward(self, x):
        data_dict = {}
        dwt, idwt = DWT(), IWT()

        input_img = x[:, :3, :, :]
        n, c, h, w = input_img.shape
        input_img_norm = data_transform(input_img)
        input_dwt = dwt(input_img_norm)

        input_LL, input_high0 = input_dwt[:n, ...], input_dwt[n:, ...]

        input_high0 = self.high_enhance0(input_high0)

        input_LL_dwt = dwt(input_LL)
        input_LL_LL, input_high1 = input_LL_dwt[:n, ...], input_LL_dwt[n:, ...]
        input_high1 = self.high_enhance1(input_high1)

        b = self.betas.to(input_img.device)

        t = torch.randint(low=0, high=self.num_timesteps, size=(
            input_LL_LL.shape[0] // 2 + 1,)).to(self.device)
        t = torch.cat([t, self.num_timesteps - t - 1],
                      dim=0)[:input_LL_LL.shape[0]].to(x.device)
        a = (1 - b).cumprod(dim=0).index_select(0, t).view(-1, 1, 1, 1)

        e = torch.randn_like(input_LL_LL)

        if self.training:
            gt_img_norm = data_transform(x[:, 3:, :, :])
            gt_dwt = dwt(gt_img_norm)
            gt_LL, gt_high0 = gt_dwt[:n, ...], gt_dwt[n:, ...]
            gt_LL_dwt = dwt(gt_LL)
            gt_LL_LL, gt_high1 = gt_LL_dwt[:n, ...], gt_LL_dwt[n:, ...]
            x = gt_LL_LL * a.sqrt() + e * (1.0 - a).sqrt()
            noise_output = self.Unet(
                torch.cat([input_LL_LL, x], dim=1), t.float())
            denoise_LL_LL_list = self.sample_training(input_LL_LL, b)
            denoise_LL_LL = denoise_LL_LL_list[-1]
            pred_LL = idwt(torch.cat((denoise_LL_LL, input_high1), dim=0))

            pred_x = idwt(torch.cat((pred_LL, input_high0), dim=0))
            pred_x = inverse_data_transform(pred_x)

            data_dict["input_high0"] = input_high0
            data_dict["input_high1"] = input_high1
            data_dict["gt_high0"] = gt_high0
            data_dict["gt_high1"] = gt_high1
            data_dict["pred_LL"] = pred_LL
            data_dict["denoise_LL_LL"] = denoise_LL_LL
            data_dict["denoise_LL_LL_list"] = denoise_LL_LL_list
            data_dict["gt_LL"] = gt_LL
            data_dict["noise_output"] = noise_output
            data_dict["pred_x"] = pred_x
            data_dict["e"] = e

        else:
            denoise_LL_LL_list = self.sample_training(input_LL_LL, b)
            denoise_LL_LL = denoise_LL_LL_list[-1]
            pred_LL = idwt(torch.cat((denoise_LL_LL, input_high1), dim=0))
            pred_x = idwt(torch.cat((pred_LL, input_high0), dim=0))
            pred_x = inverse_data_transform(pred_x)

            data_dict["pred_x"] = pred_x

        return data_dict


class DenoisingDiffusion(object):
    def __init__(self, args, config):
        super().__init__()
        self.args = args
        self.config = config
        self.device = config.device

        self.model = Net(args, config) 
        self.model.to(self.device)
        self.model = torch.nn.DataParallel(self.model)  

        self.ema_helper = EMAHelper()
        self.ema_helper.register(self.model)

        self.l2_loss = torch.nn.MSELoss()
        self.l1_loss = torch.nn.L1Loss()
        self.TV_loss = TVLoss()

        self.optimizer, self.scheduler = utils.optimize.get_optimizer(self.config, self.model.parameters())
        self.start_epoch, self.step = 0, 0

    def load_ddm_ckpt(self, load_path, ema=False):
        checkpoint = utils.logging.load_checkpoint(load_path, None)
        self.model.load_state_dict(checkpoint['state_dict'], strict=True)
        self.ema_helper.load_state_dict(checkpoint['ema_helper'])
        if ema:
            self.ema_helper.ema(self.model)
        print("Load checkpoint: ", os.path.exists(load_path))
        print("Current checkpoint: {}".format(load_path))

    def train(self, DATASET):
        if self.args.load_pretrain_prompt == True:
            learn_prompt = Prompts(self.args.prompt_pretrain_dir).cuda()
        else:
            learn_prompt = Prompts([" ".join(["X"] * (self.args.length_prompt)), " ".join(["X"] * (self.args.length_prompt))]).cuda()
        learn_prompt = torch.nn.DataParallel(learn_prompt)

        cudnn.benchmark = True
        train_loader, val_loader = DATASET.get_loaders()

        if os.path.isfile(self.args.resume):
            self.load_ddm_ckpt(self.args.resume)

        for epoch in range(self.start_epoch, self.config.training.n_epochs):
            print('epoch: ', epoch)

            for data in tqdm(train_loader, desc=f"Epoch {epoch + 1}/{self.config.training.n_epochs}", leave=False):
                data_start = time.time()
                data_time = 0
                for i, (x, y) in enumerate(train_loader):

                    x = x.flatten(start_dim=0, end_dim=1) if x.ndim == 5 else x

                    data_time += time.time() - data_start
                    self.model.train()

                    self.step += 1

                    x = x.to(self.device)

                    output = self.model(x)

                    noise_loss, photo_loss, frequency_loss, loss_fre = self.estimation_loss(x, output)
                    c_loss = self.clip_loss(self.args, x, output)
                    loss = noise_loss + photo_loss + frequency_loss + 0.001 * c_loss + 0.001 * loss_fre

                    self.optimizer.zero_grad()
                    loss.backward()
                    self.optimizer.step()
                    self.ema_helper.update(self.model)
                    data_start = time.time()

                    if self.step % self.config.training.validation_freq == 0 and self.step != 0:
                        self.model.eval()

                        self.sample_validation_patches(val_loader, self.step)

                        utils.logging.save_checkpoint({'step': self.step, 'epoch': epoch + 1,
                                                       'state_dict': self.model.state_dict(),
                                                       'optimizer': self.optimizer.state_dict(),
                                                       'scheduler': self.scheduler.state_dict(),
                                                       'ema_helper': self.ema_helper.state_dict(),
                                                       'params': self.args,
                                                       'config': self.config},
                                                      filename=os.path.join(self.config.data.ckpt_dir, 'our_model'))

            print("epoch:{}, lr:{:.6f}, noise_loss:{:.4f}, photo_loss:{:.4f}, "
                  "frequency_loss:{:.4f},c_loss:{:.4f},loss_fre:{:.4f},loss:{:.4f}".format(epoch,
                                                                                        self.scheduler.get_last_lr()[0],
                                                                                        noise_loss.item(),
                                                                                        photo_loss.item(),
                                                                                        frequency_loss.item(),
                                                                                        c_loss.item(), loss_fre.item(),
                                                                                        loss.item()))

            self.scheduler.step()

    def estimation_loss(self, x, output):

        input_high0, input_high1, gt_high0, gt_high1 = output["input_high0"], output["input_high1"], \
            output["gt_high0"], output["gt_high1"]

        pred_LL, gt_LL, pred_x, noise_output, e = output["pred_LL"], output["gt_LL"], output["pred_x"], \
            output["noise_output"], output["e"]

        gt_img = x[:, 3:, :, :].to(self.device)

        criterion = nn.L1Loss()
        net_getFre = get_Fre()

        out_amp0, out_pha0 = net_getFre(input_high0)
        gt_amp0, gt_pha0 = net_getFre(gt_high0)
        out_amp, out_pha = net_getFre(input_high1)
        gt_amp, gt_pha = net_getFre(gt_high1)
        loss_fre_amp = criterion(out_amp, gt_amp)
        loss_fre_pha = criterion(out_pha, gt_pha)
        loss_fre_amp0 = criterion(out_amp0, gt_amp0)
        loss_fre_pha0 = criterion(out_pha0, gt_pha0)
        loss_fre = 0.5*loss_fre_amp + 0.5 * loss_fre_pha + \
            0.5*loss_fre_amp0 + 0.5*loss_fre_pha0


        noise_loss = self.l2_loss(noise_output, e)

        frequency_loss = 0.1 * (self.l2_loss(input_high0, gt_high0) +
                                self.l2_loss(input_high1, gt_high1) +
                                self.l2_loss(pred_LL, gt_LL)) +\
                         0.01 * (self.TV_loss(input_high0) +
                                 self.TV_loss(input_high1) +
                                 self.TV_loss(pred_LL))

        content_loss = self.l1_loss(pred_x, gt_img)
        ssim_loss = 1 - ssim(pred_x, gt_img, data_range=1.0).to(self.device)
        photo_loss = content_loss + ssim_loss

        return noise_loss, photo_loss, frequency_loss, loss_fre

    def sample_validation_patches(self, val_loader, step):
        image_folder = os.path.join(
            self.args.image_folder, self.config.data.type + str(self.config.data.patch_size))
        self.model.eval()
        with torch.no_grad():
            print(
                f"Current Sampling Steps: {step}")
            for i, (x, y) in enumerate(val_loader):

                b, _, img_h, img_w = x.shape
                img_h_32 = int(32 * np.ceil(img_h / 32.0))
                img_w_32 = int(32 * np.ceil(img_w / 32.0))
                x = F.pad(x, (0, img_w_32 - img_w, 0,
                          img_h_32 - img_h), 'reflect')

                out = self.model(x.to(self.device))
                pred_x = out["pred_x"]
                pred_x = pred_x[:, :, :img_h, :img_w]
                utils.logging.save_image(pred_x, os.path.join(
                    image_folder, str(step), f"{y[0]}.png"))

    def clip_loss(self, args, x, output):

        pred_LL,  pred_x, denoise_LL_LL_list = output["pred_LL"], output["pred_x"], \
            output["denoise_LL_LL_list"]

        if args.load_pretrain_prompt == True:
            learn_prompt = Prompts(args.prompt_pretrain_dir).cuda()
        else:
            learn_prompt = Prompts(["well light ".join(["X"]*(args.length_prompt)), "low light ".join(["X"]*(args.length_prompt))]).cuda()
        learn_prompt = torch.nn.DataParallel(learn_prompt)

        text_encoder = TextEncoder(c_model)
        L_clip_LL = clip_loss.L_clip()
        L_clip = clip_loss.L_clip_from_feature()
        L_clip_MSE = clip_loss.L_clip_MSE()

        embedding_prompt = learn_prompt.module.embedding_prompt
        embedding_prompt.requires_grad = False
        tokenized_prompts = torch.cat(
            [clip.tokenize(p) for p in ["UHD image".join(["X"]*args.length_prompt)]])
        text_features = text_encoder(embedding_prompt, tokenized_prompts)

        x = x[:, 3:, :, :]

        denoise_LL_LL = denoise_LL_LL_list[-1]
        clip_LLLloss = L_clip_LL(pred_LL, denoise_LL_LL)
        cliploss = 16*20*L_clip(pred_x, text_features)
        clip_MSEloss = 25*L_clip_MSE(pred_x, x, [1.0, 1.0, 1.0, 1.0, 0.5])

        c_loss = cliploss + 0.9*clip_MSEloss+0.7*clip_LLLloss

        return c_loss

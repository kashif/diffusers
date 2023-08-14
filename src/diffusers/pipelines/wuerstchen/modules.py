import math

import numpy as np
import torch
import torch.nn as nn

from diffusers.configuration_utils import ConfigMixin, register_to_config

from ...models.modeling_utils import ModelMixin


class LayerNorm2d(nn.LayerNorm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(self, x):
        return super().forward(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)


class TimestepBlock(nn.Module):
    def __init__(self, c, c_timestep):
        super().__init__()
        self.mapper = nn.Linear(c_timestep, c * 2)

    def forward(self, x, t):
        a, b = self.mapper(t)[:, :, None, None].chunk(2, dim=1)
        return x * (1 + a) + b


class Attention2D(nn.Module):
    def __init__(self, c, nhead, dropout=0.0):
        super().__init__()
        self.attn = nn.MultiheadAttention(c, nhead, dropout=dropout, bias=True, batch_first=True)

    def forward(self, x, kv=None, self_attn=False):
        orig_shape = x.shape
        x = x.view(x.size(0), x.size(1), -1).permute(0, 2, 1)  # Bx4xHxW -> Bx(HxW)x4
        if self_attn and kv is not None:
            kv = torch.cat([x, kv], dim=1)
        elif kv is None:
            kv = x
        x = self.attn(x, kv, kv, need_weights=False)[0]
        x = x.permute(0, 2, 1).view(*orig_shape)
        return x


class ResBlockStageB(nn.Module):
    def __init__(self, c, c_skip=None, kernel_size=3, dropout=0.0):
        super().__init__()
        self.depthwise = nn.Conv2d(c, c, kernel_size=kernel_size, padding=kernel_size // 2, groups=c)
        self.norm = LayerNorm2d(c, elementwise_affine=False, eps=1e-6)
        self.channelwise = nn.Sequential(
            nn.Linear(c + c_skip, c * 4),
            nn.GELU(),
            GlobalResponseNorm(c * 4),
            nn.Dropout(dropout),
            nn.Linear(c * 4, c),
        )

    def forward(self, x, x_skip=None):
        x_res = x
        x = self.norm(self.depthwise(x))
        if x_skip is not None:
            x = torch.cat([x, x_skip], dim=1)
        x = self.channelwise(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        return x + x_res


class ResBlock(nn.Module):
    def __init__(self, c, c_skip=0, kernel_size=3, dropout=0.0):
        super().__init__()
        self.depthwise = nn.Conv2d(c + c_skip, c, kernel_size=kernel_size, padding=kernel_size // 2, groups=c)
        self.norm = LayerNorm2d(c, elementwise_affine=False, eps=1e-6)
        self.channelwise = nn.Sequential(
            nn.Linear(c, c * 4), nn.GELU(), GlobalResponseNorm(c * 4), nn.Dropout(dropout), nn.Linear(c * 4, c)
        )

    def forward(self, x, x_skip=None):
        x_res = x
        if x_skip is not None:
            x = torch.cat([x, x_skip], dim=1)
        x = self.norm(self.depthwise(x)).permute(0, 2, 3, 1)
        x = self.channelwise(x).permute(0, 3, 1, 2)
        return x + x_res


# from https://github.com/facebookresearch/ConvNeXt-V2/blob/3608f67cc1dae164790c5d0aead7bf2d73d9719b/models/utils.py#L105
class GlobalResponseNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.gamma = nn.Parameter(torch.zeros(1, 1, 1, dim))
        self.beta = nn.Parameter(torch.zeros(1, 1, 1, dim))

    def forward(self, x):
        agg_norm = torch.norm(x, p=2, dim=(1, 2), keepdim=True)
        stand_div_norm = agg_norm / (agg_norm.mean(dim=-1, keepdim=True) + 1e-6)
        return self.gamma * (x * stand_div_norm) + self.beta + x


class AttnBlock(nn.Module):
    def __init__(self, c, c_cond, nhead, self_attn=True, dropout=0.0):
        super().__init__()
        self.self_attn = self_attn
        self.norm = LayerNorm2d(c, elementwise_affine=False, eps=1e-6)
        self.attention = Attention2D(c, nhead, dropout)
        self.kv_mapper = nn.Sequential(nn.SiLU(), nn.Linear(c_cond, c))

    def forward(self, x, kv=None):
        if kv is not None:
            kv = self.kv_mapper(kv)
        x = x + self.attention(self.norm(x), kv, self_attn=self.self_attn)
        return x


class DiffNeXt(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        c_in=4,
        c_out=4,
        c_r=64,
        patch_size=2,
        c_cond=1024,
        c_hidden=[320, 640, 1280, 1280],
        nhead=[-1, 10, 20, 20],
        blocks=[4, 4, 14, 4],
        level_config=["CT", "CTA", "CTA", "CTA"],
        inject_effnet=[False, True, True, True],
        effnet_embd=16,
        clip_embd=1024,
        kernel_size=3,
        dropout=0.1,
        self_attn=True,
    ):
        super().__init__()
        self.c_r = c_r
        self.c_cond = c_cond
        if not isinstance(dropout, list):
            dropout = [dropout] * len(c_hidden)

        # CONDITIONING
        self.clip_mapper = nn.Linear(clip_embd, c_cond)
        self.effnet_mappers = nn.ModuleList(
            [
                nn.Conv2d(effnet_embd, c_cond, kernel_size=1) if inject else None
                for inject in inject_effnet + list(reversed(inject_effnet))
            ]
        )
        self.seq_norm = nn.LayerNorm(c_cond, elementwise_affine=False, eps=1e-6)

        self.embedding = nn.Sequential(
            nn.PixelUnshuffle(patch_size),
            nn.Conv2d(c_in * (patch_size**2), c_hidden[0], kernel_size=1),
            LayerNorm2d(c_hidden[0], elementwise_affine=False, eps=1e-6),
        )

        def get_block(block_type, c_hidden, nhead, c_skip=0, dropout=0):
            if block_type == "C":
                return ResBlockStageB(c_hidden, c_skip, kernel_size=kernel_size, dropout=dropout)
            elif block_type == "A":
                return AttnBlock(c_hidden, c_cond, nhead, self_attn=self_attn, dropout=dropout)
            elif block_type == "T":
                return TimestepBlock(c_hidden, c_r)
            else:
                raise Exception(f"Block type {block_type} not supported")

        # BLOCKS
        # -- down blocks
        self.down_blocks = nn.ModuleList()
        for i in range(len(c_hidden)):
            down_block = nn.ModuleList()
            if i > 0:
                down_block.append(
                    nn.Sequential(
                        LayerNorm2d(c_hidden[i - 1], elementwise_affine=False, eps=1e-6),
                        nn.Conv2d(c_hidden[i - 1], c_hidden[i], kernel_size=2, stride=2),
                    )
                )
            for _ in range(blocks[i]):
                for block_type in level_config[i]:
                    c_skip = c_cond if inject_effnet[i] else 0
                    down_block.append(get_block(block_type, c_hidden[i], nhead[i], c_skip=c_skip, dropout=dropout[i]))
            self.down_blocks.append(down_block)

        # -- up blocks
        self.up_blocks = nn.ModuleList()
        for i in reversed(range(len(c_hidden))):
            up_block = nn.ModuleList()
            for j in range(blocks[i]):
                for k, block_type in enumerate(level_config[i]):
                    c_skip = c_hidden[i] if i < len(c_hidden) - 1 and j == k == 0 else 0
                    c_skip += c_cond if inject_effnet[i] else 0
                    up_block.append(get_block(block_type, c_hidden[i], nhead[i], c_skip=c_skip, dropout=dropout[i]))
            if i > 0:
                up_block.append(
                    nn.Sequential(
                        LayerNorm2d(c_hidden[i], elementwise_affine=False, eps=1e-6),
                        nn.ConvTranspose2d(c_hidden[i], c_hidden[i - 1], kernel_size=2, stride=2),
                    )
                )
            self.up_blocks.append(up_block)

        # OUTPUT
        self.clf = nn.Sequential(
            LayerNorm2d(c_hidden[0], elementwise_affine=False, eps=1e-6),
            nn.Conv2d(c_hidden[0], 2 * c_out * (patch_size**2), kernel_size=1),
            nn.PixelShuffle(patch_size),
        )

        # --- WEIGHT INIT ---
        self.apply(self._init_weights)  # General init
        for mapper in self.effnet_mappers:
            if mapper is not None:
                nn.init.normal_(mapper.weight, std=0.02)  # conditionings
        nn.init.normal_(self.clip_mapper.weight, std=0.02)  # conditionings
        nn.init.xavier_uniform_(self.embedding[1].weight, 0.02)  # inputs
        nn.init.constant_(self.clf[1].weight, 0)  # outputs

        # blocks
        for level_block in self.down_blocks + self.up_blocks:
            for block in level_block:
                if isinstance(block, ResBlockStageB):
                    block.channelwise[-1].weight.data *= np.sqrt(1 / sum(blocks))
                elif isinstance(block, TimestepBlock):
                    nn.init.constant_(block.mapper.weight, 0)

    def _init_weights(self, m):
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def gen_r_embedding(self, r, max_positions=10000):
        r = r * max_positions
        half_dim = self.c_r // 2
        emb = math.log(max_positions) / (half_dim - 1)
        emb = torch.arange(half_dim, device=r.device).float().mul(-emb).exp()
        emb = r[:, None] * emb[None, :]
        emb = torch.cat([emb.sin(), emb.cos()], dim=1)
        if self.c_r % 2 == 1:  # zero pad
            emb = nn.functional.pad(emb, (0, 1), mode="constant")
        return emb.to(dtype=r.dtype)

    def gen_c_embeddings(self, clip):
        clip = self.clip_mapper(clip)
        clip = self.seq_norm(clip)
        return clip

    def _down_encode(self, x, r_embed, effnet, clip=None):
        level_outputs = []
        for i, down_block in enumerate(self.down_blocks):
            effnet_c = None
            for block in down_block:
                if isinstance(block, ResBlockStageB):
                    if effnet_c is None and self.effnet_mappers[i] is not None:
                        dtype = effnet.dtype
                        effnet_c = self.effnet_mappers[i](
                            nn.functional.interpolate(
                                effnet.float(), size=x.shape[-2:], mode="bicubic", antialias=True, align_corners=True
                            ).to(dtype)
                        )
                    skip = effnet_c if self.effnet_mappers[i] is not None else None
                    x = block(x, skip)
                elif isinstance(block, AttnBlock):
                    x = block(x, clip)
                elif isinstance(block, TimestepBlock):
                    x = block(x, r_embed)
                else:
                    x = block(x)
            level_outputs.insert(0, x)
        return level_outputs

    def _up_decode(self, level_outputs, r_embed, effnet, clip=None):
        x = level_outputs[0]
        for i, up_block in enumerate(self.up_blocks):
            effnet_c = None
            for j, block in enumerate(up_block):
                if isinstance(block, ResBlockStageB):
                    if effnet_c is None and self.effnet_mappers[len(self.down_blocks) + i] is not None:
                        dtype = effnet.dtype
                        effnet_c = self.effnet_mappers[len(self.down_blocks) + i](
                            nn.functional.interpolate(
                                effnet.float(), size=x.shape[-2:], mode="bicubic", antialias=True, align_corners=True
                            ).to(dtype)
                        )
                    skip = level_outputs[i] if j == 0 and i > 0 else None
                    if effnet_c is not None:
                        if skip is not None:
                            skip = torch.cat([skip, effnet_c], dim=1)
                        else:
                            skip = effnet_c
                    x = block(x, skip)
                elif isinstance(block, AttnBlock):
                    x = block(x, clip)
                elif isinstance(block, TimestepBlock):
                    x = block(x, r_embed)
                else:
                    x = block(x)
        return x

    def forward(self, x, r, effnet, clip=None, x_cat=None, eps=1e-3, return_noise=True):
        if x_cat is not None:
            x = torch.cat([x, x_cat], dim=1)
        # Process the conditioning embeddings
        r_embed = self.gen_r_embedding(r)
        if clip is not None:
            clip = self.gen_c_embeddings(clip)

        # Model Blocks
        x_in = x
        x = self.embedding(x)
        level_outputs = self._down_encode(x, r_embed, effnet, clip)
        x = self._up_decode(level_outputs, r_embed, effnet, clip)
        a, b = self.clf(x).chunk(2, dim=1)
        b = b.sigmoid() * (1 - eps * 2) + eps
        if return_noise:
            return (x_in - a) / b
        else:
            return a, b


class Prior(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(self, c_in=16, c=1280, c_cond=1024, c_r=64, depth=16, nhead=16, latent_size=(12, 12), dropout=0.1):
        super().__init__()
        self.c_r = c_r
        self.projection = nn.Conv2d(c_in, c, kernel_size=1)
        self.cond_mapper = nn.Sequential(
            nn.Linear(c_cond, c),
            nn.LeakyReLU(0.2),
            nn.Linear(c, c),
        )

        self.blocks = nn.ModuleList()
        for _ in range(depth):
            self.blocks.append(ResBlock(c, dropout=dropout))
            self.blocks.append(TimestepBlock(c, c_r))
            self.blocks.append(AttnBlock(c, c, nhead, self_attn=True, dropout=dropout))
        self.out = nn.Sequential(
            LayerNorm2d(c, elementwise_affine=False, eps=1e-6),
            nn.Conv2d(c, c_in * 2, kernel_size=1),
        )

    def gen_r_embedding(self, r, max_positions=10000):
        r = r * max_positions
        half_dim = self.c_r // 2
        emb = math.log(max_positions) / (half_dim - 1)
        emb = torch.arange(half_dim, device=r.device).float().mul(-emb).exp()
        emb = r[:, None] * emb[None, :]
        emb = torch.cat([emb.sin(), emb.cos()], dim=1)
        if self.c_r % 2 == 1:  # zero pad
            emb = nn.functional.pad(emb, (0, 1), mode="constant")
        return emb.to(dtype=r.dtype)

    def forward(self, x, r, c):
        x_in = x
        x = self.projection(x)
        c_embed = self.cond_mapper(c)
        r_embed = self.gen_r_embedding(r)
        for block in self.blocks:
            if isinstance(block, AttnBlock):
                x = block(x, c_embed)
            elif isinstance(block, TimestepBlock):
                x = block(x, r_embed)
            else:
                x = block(x)
        a, b = self.out(x).chunk(2, dim=1)
        # denoised = a / (1-(1-b).pow(2)).sqrt()
        return (x_in - a) / ((1 - b).abs() + 1e-5)

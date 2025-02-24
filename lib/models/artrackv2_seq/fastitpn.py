# --------------------------------------------------------
# Fast-iTPN: Integrally Pre-Trained Transformer Pyramid Network with Token Migration
# Github source: https://github.com/sunsmarterjie/iTPN/tree/main/fast_itpn
# Copyright (c) 2023 University of Chinese Academy of Sciences
# Licensed under The MIT License [see LICENSE for details]
# By Yunjie Tian
# Based on EVA02, timm and deit code bases
# https://github.com/baaivision/EVA/tree/master/EVA-02
# https://github.com/rwightman/pytorch-image-models/tree/master/timm
# https://github.com/facebookresearch/deit/
# --------------------------------------------------------'
from functools import partial
import warnings
import math
import torch
import torch.nn as nn
from timm.models.registry import register_model
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint
from timm.models.layers import to_2tuple, drop_path, trunc_normal_
from lib.models.layers.patch_embed import PatchEmbed as PatchEmbed_true
from lib.models.artrackv2.utils import combine_tokens, recover_tokens
from lib.models.artrackv2.vit import Block as final_block
from torch import Tensor, Size
from typing import Union, List
import os

current_file_path = os.getcwd()


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': (0.5, 0.5, 0.5), 'std': (0.5, 0.5, 0.5),
        **kwargs
    }


_shape_t = Union[int, List[int], Size]

def generate_square_subsequent_mask(sz, sx, ss):
    r"""Generate a square mask for the sequence. The masked positions are filled with float('-inf').
        Unmasked positions are filled with float(0.0).
    """
    sum = sz + sx + ss
    mask = (torch.triu(torch.ones(sum, sum)) == 1).transpose(0, 1)
    mask[:, :] = 0
    mask[:int(sz/2), :int(sz/2)] = 1 #template self
    mask[int(sz/2):sz, int(sz/2):sz] = 1 # dt self
    mask[int(sz/2):sz, sz:sz+sx] = 1 # dt search
    mask[int(sz / 2):sz, -1] = 1  # dt search
    mask[sz:sz+sx, :sz+sx] = 1 # sr dt-t-sr
    mask[sz+sx:, :] = 1 # co dt-t-sr-co
    return ~mask

class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """

    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)

    def extra_repr(self) -> str:
        return 'p={}'.format(self.drop_prob)


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.,
                 norm_layer=nn.LayerNorm, subln=False
                 ):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()

        self.ffn_ln = norm_layer(hidden_features) if subln else nn.Identity()

        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.ffn_ln(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class ConvMlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.,
                 norm_layer=nn.LayerNorm, subln=False
                 ):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.fc1 = nn.Conv2d(in_features, hidden_features, 1)
        self.act = act_layer()

        self.ffn_ln = norm_layer(hidden_features) if subln else None

        self.fc2 = nn.Conv2d(hidden_features, out_features, 1)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        if self.ffn_ln is not None:
            x = x.permute(0, 2, 3, 1)
            x = self.ffn_ln(x)
            x = x.permute(0, 3, 1, 2)
        x = self.fc2(x)
        x = self.drop(x)
        return x


class SwiGLU(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.SiLU, drop=0.,
                 norm_layer=nn.LayerNorm, subln=False
                 ):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.w1 = nn.Linear(in_features, hidden_features)
        self.w2 = nn.Linear(in_features, hidden_features)

        self.act = act_layer()
        self.ffn_ln = norm_layer(hidden_features) if subln else nn.Identity()
        self.w3 = nn.Linear(hidden_features, out_features)

        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x1 = self.w1(x)
        x2 = self.w2(x)
        hidden = self.act(x1) * x2
        x = self.ffn_ln(hidden)
        x = self.w3(x)
        x = self.drop(x)
        return x


class ConvSwiGLU(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.SiLU, drop=0.,
                 norm_layer=nn.LayerNorm, subln=False
                 ):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.w1 = nn.Conv2d(in_features, hidden_features, 1)
        self.w2 = nn.Conv2d(in_features, hidden_features, 1)

        self.act = act_layer()
        self.ffn_ln = norm_layer(hidden_features) if subln else nn.Identity()
        self.w3 = nn.Conv2d(hidden_features, out_features, 1)

        self.drop = nn.Dropout(drop)

    def forward(self, x):
        B, C, H, W = x.shape
        x1 = self.w1(x).flatten(2).transpose(1, 2)
        x2 = self.w2(x).flatten(2).transpose(1, 2)
        hidden = self.act(x1) * x2
        x = self.ffn_ln(hidden).transpose(1, 2).view(B, C, H, W)
        x = self.w3(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(
            self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0., window_size=None,
            attn_head_dim=None, use_decoupled_rel_pos_bias=False, deepnorm=False, subln=False
    ):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        if attn_head_dim is not None:
            head_dim = attn_head_dim
        all_head_dim = head_dim * self.num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.deepnorm = deepnorm
        self.subln = subln
        if self.deepnorm or self.subln:
            self.q_proj = nn.Linear(dim, all_head_dim, bias=False)
            self.k_proj = nn.Linear(dim, all_head_dim, bias=False)
            self.v_proj = nn.Linear(dim, all_head_dim, bias=False)
        else:
            self.qkv = nn.Linear(dim, all_head_dim * 3, bias=False)

        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.v_bias = nn.Parameter(torch.zeros(all_head_dim))
        else:
            self.q_bias = None
            self.v_bias = None

        self.rel_pos_bias = None
        self.qk_float = True

        self.window_size = None
        self.relative_position_bias_table = None

        if window_size:
            if use_decoupled_rel_pos_bias:
                self.rel_pos_bias = DecoupledRelativePositionBias(window_size=window_size, num_heads=num_heads)
            else:
                self.window_size = window_size
                self.num_relative_distance = (2 * window_size[0] - 1) * (
                        2 * window_size[1] - 1) + 3  # (2*14-1) * (2*14-1) + 3
                self.relative_position_bias_table = nn.Parameter(
                    torch.zeros(self.num_relative_distance, num_heads))  # 2*Wh-1 * 2*Ww-1, nH
                # cls to token & token 2 cls & cls to cls

                # get pair-wise relative position index for each token inside the window
                coords_h = torch.arange(window_size[0])
                coords_w = torch.arange(window_size[1])
                coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
                coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
                relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
                relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
                relative_coords[:, :, 0] += window_size[0] - 1  # shift to start from 0
                relative_coords[:, :, 1] += window_size[1] - 1
                relative_coords[:, :, 0] *= 2 * window_size[1] - 1
                relative_position_index = \
                    torch.zeros(size=(window_size[0] * window_size[1] + 1,) * 2, dtype=relative_coords.dtype)
                relative_position_index[1:, 1:] = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
                relative_position_index[0, 0:] = self.num_relative_distance - 3
                relative_position_index[0:, 0] = self.num_relative_distance - 2
                relative_position_index[0, 0] = self.num_relative_distance - 1

                self.register_buffer("relative_position_index", relative_position_index)

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(all_head_dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, rel_pos_bias=None, attn_mask=None):
        B, N, C = x.shape

        if self.deepnorm or self.subln:
            q = F.linear(input=x, weight=self.q_proj.weight, bias=self.q_bias)
            k = F.linear(input=x, weight=self.k_proj.weight, bias=None)
            v = F.linear(input=x, weight=self.v_proj.weight, bias=self.v_bias)

            q = q.reshape(B, N, self.num_heads, -1).permute(0, 2, 1, 3)  # B, num_heads, N, C
            k = k.reshape(B, N, self.num_heads, -1).permute(0, 2, 1, 3)
            v = v.reshape(B, N, self.num_heads, -1).permute(0, 2, 1, 3)
        else:
            qkv_bias = None
            if self.q_bias is not None:
                qkv_bias = torch.cat((self.q_bias, torch.zeros_like(self.v_bias, requires_grad=False), self.v_bias))
            qkv = F.linear(input=x, weight=self.qkv.weight, bias=qkv_bias)
            qkv = qkv.reshape(B, N, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)  # 3, B, num_heads, N, C
            q, k, v = qkv[0], qkv[1], qkv[2]

        q = q * self.scale
        if self.qk_float:
            attn = (q.float() @ k.float().transpose(-2, -1))
        else:
            attn = (q @ k.transpose(-2, -1))

        if self.relative_position_bias_table is not None:
            relative_position_bias = \
                self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
                    self.window_size[0] * self.window_size[1] + 1,
                    self.window_size[0] * self.window_size[1] + 1, -1)  # Wh*Ww,Wh*Ww,nH
            relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww
            attn = attn + relative_position_bias.unsqueeze(0).type_as(attn)

        if self.rel_pos_bias is not None:
            attn = attn + self.rel_pos_bias().type_as(attn)

        if rel_pos_bias is not None:
            attn = attn + rel_pos_bias.type_as(attn)
        if attn_mask is not None:
            attn_mask = attn_mask.bool()
            attn = attn.masked_fill(~attn_mask[:, None, None, :], float("-inf"))
        attn = attn.softmax(dim=-1).type_as(x)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class Block(nn.Module):

    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., init_values=None, norm_layer=nn.LayerNorm, window_size=None, attn_head_dim=None,
                 use_decoupled_rel_pos_bias=False,
                 depth=None,
                 postnorm=False,
                 deepnorm=False,
                 subln=False,
                 swiglu=False,
                 naiveswiglu=False,
                 ):
        super().__init__()

        with_attn = num_heads > 0

        self.norm1 = norm_layer(dim) if with_attn else None
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, window_size=window_size,
            use_decoupled_rel_pos_bias=use_decoupled_rel_pos_bias, attn_head_dim=attn_head_dim,
            deepnorm=deepnorm,
            subln=subln
        ) if with_attn else None

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)

        mlp_hidden_dim = int(dim * mlp_ratio)
        if swiglu:
            self.mlp = xops.SwiGLU(
                in_features=dim,
                hidden_features=mlp_hidden_dim
            )  # hidden_features: 2/3
        elif naiveswiglu:
            self.mlp = SwiGLU(
                in_features=dim,
                hidden_features=mlp_hidden_dim,
                subln=subln,
                norm_layer=norm_layer,
            )
        else:
            self.mlp = Mlp(
                in_features=dim,
                hidden_features=mlp_hidden_dim,
                subln=subln,
                norm_layer=norm_layer
            )

        if init_values is not None and init_values > 0:
            self.gamma_1 = nn.Parameter(init_values * torch.ones((dim)),
                                        requires_grad=True) if self.attn is not None else None
            self.gamma_2 = nn.Parameter(init_values * torch.ones((dim)), requires_grad=True)
        else:
            self.gamma_1, self.gamma_2 = None, None

        self.deepnorm = deepnorm
        if self.deepnorm:
            self.alpha = math.pow(2.0 * depth, 0.25)

        self.postnorm = postnorm

    def forward(self, x, rel_pos_bias=None, attn_mask=None):
        if self.gamma_2 is None:
            if self.postnorm:
                if self.attn is not None:
                    x = x + self.drop_path(
                        self.norm1(self.attn(x, rel_pos_bias=rel_pos_bias, attn_mask=attn_mask)))
                x = x + self.drop_path(self.norm2(self.mlp(x)))
            elif self.deepnorm:
                if self.attn is not None:
                    residual = x
                    x = self.attn(x, rel_pos_bias=rel_pos_bias, attn_mask=attn_mask)
                    x = self.drop_path(x)
                    x = residual * self.alpha + x
                    x = self.norm1(x)

                residual = x
                x = self.mlp(x)
                x = self.drop_path(x)
                x = residual * self.alpha + x
                x = self.norm2(x)
            else:
                if self.attn is not None:
                    x = x + self.drop_path(
                        self.attn(self.norm1(x), rel_pos_bias=rel_pos_bias, attn_mask=attn_mask))
                x = x + self.drop_path(self.mlp(self.norm2(x)))
        else:
            if self.postnorm:
                if self.attn is not None:
                    x = x + self.drop_path(
                        self.gamma_1 * self.norm1(self.attn(x, rel_pos_bias=rel_pos_bias, attn_mask=attn_mask)))
                x = x + self.drop_path(self.gamma_2 * self.norm2(self.mlp(x)))
            else:
                if self.attn is not None:
                    x = x + self.drop_path(
                        self.gamma_1 * self.attn(self.norm1(x), rel_pos_bias=rel_pos_bias, attn_mask=attn_mask))
                x = x + self.drop_path(self.gamma_2 * self.mlp(self.norm2(x)))
        return x


class ConvMlpBlock(nn.Module):

    def __init__(self, dim, mlp_ratio=4., drop_path=0., init_values=None, norm_layer=nn.LayerNorm,
                 depth=None,
                 postnorm=False,
                 deepnorm=False,
                 subln=False,
                 swiglu=False,
                 naiveswiglu=False,
                 ):
        super().__init__()

        self.attn = None

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)

        mlp_hidden_dim = int(dim * mlp_ratio)

        if swiglu:
            self.mlp = xops.SwiGLU(
                in_features=dim,
                hidden_features=mlp_hidden_dim
            )  # hidden_features: 2/3
        elif naiveswiglu:
            self.mlp = ConvSwiGLU(
                in_features=dim,
                hidden_features=mlp_hidden_dim,
                subln=subln,
                norm_layer=norm_layer,
            )
        else:
            self.mlp = ConvMlp(
                in_features=dim,
                hidden_features=mlp_hidden_dim,
                subln=subln,
                norm_layer=norm_layer
            )

        if init_values is not None and init_values > 0:
            self.gamma_1 = nn.Parameter(init_values * torch.ones((dim)),
                                        requires_grad=True) if self.attn is not None else None
            self.gamma_2 = nn.Parameter(init_values * torch.ones((dim)), requires_grad=True)
        else:
            self.gamma_1, self.gamma_2 = None, None

        self.deepnorm = deepnorm
        if self.deepnorm:
            self.alpha = math.pow(2.0 * depth, 0.25)

        self.postnorm = postnorm

    def forward(self, x):
        if self.gamma_2 is None:
            if self.postnorm:
                x = x + self.drop_path(self.norm2(self.mlp(x)))
            elif self.deepnorm:
                residual = x
                x = self.mlp(x)
                x = self.drop_path(x)
                x = residual * self.alpha + x
                x = self.norm2(x)
            else:
                x = x + self.drop_path(self.mlp(self.norm2(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)))
        else:
            if self.postnorm:
                x = x + self.drop_path(self.gamma_2 * self.norm2(self.mlp(x)))
            else:
                m = self.mlp(self.norm2(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2))
                x = x + self.drop_path(self.gamma_2 * m)
        return x


class PatchEmbed(nn.Module):
    def __init__(self, img_size=224, patch_size=16, inner_patches=4, in_chans=3, embed_dim=128, norm_layer=None):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        patches_resolution = [img_size[0] // patch_size[0], img_size[1] // patch_size[1]]
        self.img_size = img_size
        self.patch_size = patch_size
        self.inner_patches = inner_patches
        self.patches_resolution = self.patch_shape = patches_resolution
        self.num_patches = patches_resolution[0] * patches_resolution[1]

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        conv_size = [size // inner_patches for size in patch_size]
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=conv_size, stride=conv_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):
        B, C, H, W = x.shape
        patches_resolution = (H // self.patch_size[0], W // self.patch_size[1])
        num_patches = patches_resolution[0] * patches_resolution[1]
        x = self.proj(x).view(
            B, -1,
            patches_resolution[0], self.inner_patches,
            patches_resolution[1], self.inner_patches,
        ).permute(0, 2, 4, 3, 5, 1).reshape(B, num_patches, self.inner_patches, self.inner_patches, -1)
        if self.norm is not None:
            x = self.norm(x)
        return x


class ConvPatchEmbed(nn.Module):
    def __init__(self, search_size=224,template_size=112, patch_size=16, inner_patches=4, in_chans=3, embed_dim=128, norm_layer=None,
                 stop_grad_conv1=False):
        super().__init__()
        search_size = to_2tuple(search_size)
        template_size = to_2tuple(template_size)
        patch_size = to_2tuple(patch_size)
        patches_resolution_search = [search_size[0] // patch_size[0], search_size[1] // patch_size[1]]
        patches_resolution_template = [template_size[0] // patch_size[0], template_size[1] // patch_size[1]]
        self.search_size = search_size
        self.template_size = template_size
        self.patch_size = patch_size
        self.stop_grad_conv1 = stop_grad_conv1
        self.inner_patches = inner_patches
        self.patches_resolution_search = self.patch_shape_search = patches_resolution_search
        self.num_patches_search = patches_resolution_search[0] * patches_resolution_search[1]
        self.patches_resolution_template = self.patch_shape_template = patches_resolution_template
        self.num_patches_template = patches_resolution_template[0] * patches_resolution_template[1]

        self.in_chans = in_chans
        self.embed_dim = embed_dim

        conv_size = [size // inner_patches for size in patch_size]
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=conv_size, stride=conv_size)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x, bool_masked_pos=None, mask_token=None):
        """
        input: x: B, C, H, W
        output: x: B, num_patches, inner_patches, inner_patches, embed_dim
        """
        B, C, H, W = x.shape
        x = self.proj(x)
        if self.stop_grad_conv1:
            x = x.detach() * 0.9 + x * 0.1

        if bool_masked_pos is not None:
            x = torch.nn.functional.unfold(x, kernel_size=4, stride=4, padding=0).transpose(1, 2)

            seq_len = x.shape[1]
            mask_token = mask_token.expand(B, seq_len, -1)
            w = bool_masked_pos.unsqueeze(-1).type_as(mask_token)
            x = x * (1 - w) + mask_token * w

            x = torch.nn.functional.fold(x.transpose(1, 2), output_size=(H // 4, W // 4), kernel_size=4, padding=0,
                                         stride=4)
        if self.norm is not None:
            x = self.norm(x)
        return x


class PatchMerge(nn.Module):
    def __init__(self, dim, norm_layer):
        super().__init__()
        self.norm = norm_layer(dim * 4)
        self.reduction = nn.Linear(dim * 4, dim * 2, bias=False)
        self.mlp = None

    def forward(self, x):
        x0 = x[..., 0::2, 0::2, :]
        x1 = x[..., 1::2, 0::2, :]
        x2 = x[..., 0::2, 1::2, :]
        x3 = x[..., 1::2, 1::2, :]

        x = torch.cat([x0, x1, x2, x3], dim=-1)
        x = self.norm(x)
        x = self.reduction(x)
        return x


class ConvPatchMerge(nn.Module):
    def __init__(self, dim, norm_layer):
        super().__init__()
        self.norm = norm_layer(dim)
        self.reduction = nn.Conv2d(dim, dim * 2, kernel_size=2, stride=2, padding=0)
        self.mlp = None

    def forward(self, x):
        x = self.norm(x.permute(0, 2, 3, 1)).permute(0, 3, 1, 2)
        x = self.reduction(x)
        return x


class RelativePositionBias(nn.Module):

    def __init__(self, window_size, num_heads):
        super().__init__()
        self.window_size = window_size
        self.num_relative_distance = (2 * window_size[0] - 1) * (2 * window_size[1] - 1) + 3
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros(self.num_relative_distance, num_heads))  # 2*Wh-1 * 2*Ww-1, nH
        # cls to token & token 2 cls & cls to cls

        # get pair-wise relative position index for each token inside the window
        coords_h = torch.arange(window_size[0])
        coords_w = torch.arange(window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2
        relative_coords[:, :, 0] += window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * window_size[1] - 1
        relative_position_index = \
            torch.zeros(size=(window_size[0] * window_size[1] + 1,) * 2, dtype=relative_coords.dtype)
        relative_position_index[1:, 1:] = relative_coords.sum(-1)  # Wh*Ww, Wh*Ww
        relative_position_index[0, 0:] = self.num_relative_distance - 3
        relative_position_index[0:, 0] = self.num_relative_distance - 2
        relative_position_index[0, 0] = self.num_relative_distance - 1

        self.register_buffer("relative_position_index", relative_position_index)

    def forward(self):
        relative_position_bias = \
            self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
                self.window_size[0] * self.window_size[1] + 1,
                self.window_size[0] * self.window_size[1] + 1, -1)  # Wh*Ww,Wh*Ww,nH
        return relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wh*Ww, Wh*Ww


def _mask_1d_rel_pos_index(seq_len):
    index = torch.arange(seq_len)
    return index.view(1, seq_len) - index.view(seq_len, 1) + seq_len - 1


def _add_cls_to_index_matrix(index, num_tokens, offset):
    index = index.contiguous().view(num_tokens, num_tokens)
    new_index = torch.zeros(size=(num_tokens + 1, num_tokens + 1), dtype=index.dtype)
    new_index[1:, 1:] = index
    new_index[0, 0:] = offset
    new_index[0:, 0] = offset + 1
    new_index[0, 0] = offset + 2
    return new_index


class DecoupledRelativePositionBias(nn.Module):

    def __init__(self, window_size, num_heads):
        super().__init__()
        self.window_size = window_size
        self.num_relative_distance = (2 * window_size[0] + 2, 2 * window_size[1] + 2)

        num_tokens = window_size[0] * window_size[1]

        self.relative_position_bias_for_high = nn.Parameter(torch.zeros(self.num_relative_distance[0], num_heads))
        self.relative_position_bias_for_width = nn.Parameter(torch.zeros(self.num_relative_distance[1], num_heads))
        # cls to token & token 2 cls & cls to cls

        h_index = _mask_1d_rel_pos_index(window_size[0]).view(
            window_size[0], 1, window_size[0], 1).expand(-1, window_size[1], -1, window_size[1])
        h_index = _add_cls_to_index_matrix(h_index, num_tokens, 2 * window_size[0] - 1)
        self.register_buffer("relative_position_high_index", h_index)

        w_index = _mask_1d_rel_pos_index(window_size[1]).view(
            1, window_size[1], 1, window_size[1]).expand(window_size[0], -1, window_size[0], -1)
        w_index = _add_cls_to_index_matrix(w_index, num_tokens, 2 * window_size[1] - 1)

        self.register_buffer("relative_position_width_index", w_index)

    def forward(self):
        relative_position_bias = \
            F.embedding(input=self.relative_position_high_index, weight=self.relative_position_bias_for_high) + \
            F.embedding(input=self.relative_position_width_index, weight=self.relative_position_bias_for_width)
        return relative_position_bias.permute(2, 0, 1).contiguous()


class Fast_iTPN(nn.Module):
    def __init__(self, search_size=224,template_size=112, patch_size=16, in_chans=3, embed_dim=512, depth_stage1=3, depth_stage2=3, depth=24,
                 num_heads=8, bridge_mlp_ratio=3., mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0.,
                 attn_drop_rate=0., drop_path_rate=0.0, init_values=None, attn_head_dim=None, norm_layer=nn.LayerNorm,
                 patch_norm=False, num_classes=1000, use_mean_pooling=False,
                 init_scale=0.01,
                 cls_token=False,
                 grad_ckpt=False,
                 stop_grad_conv1=False,
                 use_abs_pos_emb=True,
                 use_rel_pos_bias=False,
                 use_shared_rel_pos_bias=False,
                 use_shared_decoupled_rel_pos_bias=False,
                 convmlp=False,

                 postnorm=False,
                 deepnorm=False,
                 subln=False,
                 swiglu=False,
                 bins = 400,
                 range_time = 2,
                 naiveswiglu=False,
                 token_type_indicate=False,
                 prenum = 7,
                 **kwargs):
        super().__init__()
        self.search_size = search_size
        self.template_size = template_size
        self.token_type_indicate = token_type_indicate
        self.mlp_ratio = mlp_ratio
        self.grad_ckpt = grad_ckpt
        self.num_main_blocks = depth
        self.depth_stage1 = depth_stage1
        self.depth_stage2 = depth_stage2
        self.depth = depth
        self.patch_size = patch_size
        self.num_features = self.embed_dim = embed_dim
        self.convmlp = convmlp
        self.stop_grad_conv1 = stop_grad_conv1
        self.use_rel_pos_bias = use_rel_pos_bias
        self.use_shared_rel_pos_bias = use_shared_rel_pos_bias
        self.use_shared_decoupled_rel_pos_bias = use_shared_decoupled_rel_pos_bias
        self.use_decoupled_rel_pos_bias = False

        mlvl_dims = {'4': embed_dim // 4, '8': embed_dim // 2, '16': embed_dim}
        # split image into non-overlapping patches
        if convmlp:
            self.patch_embed = ConvPatchEmbed(
                search_size=search_size,template_size=template_size, patch_size=patch_size, in_chans=in_chans, embed_dim=mlvl_dims['4'],
                stop_grad_conv1=stop_grad_conv1,
                norm_layer=norm_layer if patch_norm else None)
        else:
            self.patch_embed = PatchEmbed(
                img_size=search_size, patch_size=patch_size, in_chans=in_chans, embed_dim=mlvl_dims['4'],
                norm_layer=norm_layer if patch_norm else None)
        self.num_patches_search = self.patch_embed.num_patches_search
        self.num_patches_template = self.patch_embed.num_patches_template
        if cls_token:
            self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        else:
            self.cls_token = None
        if use_abs_pos_emb:
            if cls_token:
                self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
            else:
                self.pos_embed = nn.Parameter(torch.zeros(1, self.num_patches_search+self.num_patches_template, embed_dim))
        else:
            self.pos_embed = None
        # indicate for tracking
        if self.token_type_indicate:
            self.template_background_token = nn.Parameter(torch.zeros(embed_dim))
            self.template_foreground_token = nn.Parameter(torch.zeros(embed_dim))
            self.search_token = nn.Parameter(torch.zeros(embed_dim))

        self.pos_drop = nn.Dropout(p=drop_rate)

        if use_shared_rel_pos_bias:
            self.rel_pos_bias = RelativePositionBias(window_size=self.patch_embed.patch_shape, num_heads=num_heads)
        else:
            self.rel_pos_bias = None

        if use_shared_decoupled_rel_pos_bias:
            assert self.rel_pos_bias is None
            self.rel_pos_bias = DecoupledRelativePositionBias(window_size=self.patch_embed.patch_shape,
                                                              num_heads=num_heads)
        self.bins = bins
        self.range = range_time
        self.word_embeddings = nn.Embedding(self.bins*self.range + 6,embed_dim,padding_idx=self.bins*self.range+4,max_norm=None,norm_type = 2.0)
        self.subln = subln
        self.swiglu = swiglu
        self.naiveswiglu = naiveswiglu

        # self.pos_embed_z0 = None
        # self.pos_embed_z1 = None
        # self.pos_embed_x = None
        self.prev_position_embeddings = nn.Embedding(prenum * 4,embed_dim)
        self.position_embeddings = nn.Embedding(
            5, embed_dim)
        nn.init.kaiming_normal_(self.word_embeddings.weight.data)
        trunc_normal_(self.position_embeddings.weight.data,std=.02)
        self.patch_embed_true = PatchEmbed_true(img_size=self.search_size, patch_size=patch_size, in_chans=48,
                                          embed_dim=self.embed_dim)
        self.output_bias = torch.nn.Parameter(torch.zeros(self.bins * self.range + 6))
        final_depth = 3
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, final_depth)]
        self.final_blocks = nn.Sequential(*[final_block(dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, drop=drop_rate,
                attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, act_layer=nn.GELU)for i in range(final_depth)])
        self.build_blocks(
            depths=[depth_stage1, depth_stage2, depth],
            dims=mlvl_dims,
            num_heads=num_heads,
            bridge_mlp_ratio=bridge_mlp_ratio,
            mlp_ratio=mlp_ratio,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            window_size=self.patch_embed.patch_shape if use_rel_pos_bias else None,
            drop=drop_rate,
            attn_drop=attn_drop_rate,
            drop_path_rate=drop_path_rate,
            norm_layer=norm_layer,
            init_values=init_values,
            attn_head_dim=attn_head_dim,
            postnorm=postnorm,
            deepnorm=deepnorm,
            subln=subln,
            swiglu=swiglu,
            naiveswiglu=naiveswiglu,
            convmlp=convmlp,
        )
        self.img_size = [224, 224]
        self.norm = nn.Identity() if use_mean_pooling else norm_layer(embed_dim)
        self.fc_norm = norm_layer(embed_dim) if use_mean_pooling else None
        self.head = nn.Identity()
 
        if self.pos_embed is not None:
            trunc_normal_(self.pos_embed, std=.02)
        if self.cls_token is not None:
            trunc_normal_(self.cls_token, std=.02)

        if isinstance(self.head, nn.Linear):
            trunc_normal_(self.head.weight, std=.02)

        self.apply(self._init_weights)

        if isinstance(self.head, nn.Linear):
            self.head.weight.data.mul_(init_scale)
            self.head.bias.data.mul_(init_scale)

    def build_blocks(self,
                     depths=[3, 3, 24],
                     dims={'4': 128 // 4, '8': 256, '16': 512},
                     num_heads=8,
                     bridge_mlp_ratio=3.,
                     mlp_ratio=4.0,
                     qkv_bias=True,
                     qk_scale=None,
                     window_size=None,
                     drop=0.,
                     attn_drop=0.,
                     drop_path_rate=0.,
                     norm_layer=nn.LayerNorm,
                     init_values=0.,
                     attn_head_dim=None,
                     postnorm=False,
                     deepnorm=False,
                     subln=False,
                     swiglu=False,
                     naiveswiglu=False,
                     convmlp=False,
                     ):
        dpr = iter(x.item() for x in torch.linspace(0, drop_path_rate, depths[0] + depths[1] + depths[2]))

        self.blocks = nn.ModuleList()

        if convmlp:
            self.blocks.extend([
                ConvMlpBlock(
                    dim=dims['4'],
                    mlp_ratio=bridge_mlp_ratio,
                    drop_path=next(dpr),
                    norm_layer=norm_layer,
                    init_values=0.,
                    depth=depths[-1],
                    postnorm=postnorm,
                    deepnorm=deepnorm,
                    subln=subln,
                    swiglu=False,
                    naiveswiglu=False,
                ) for _ in range(depths[0])
            ])
            self.blocks.append(ConvPatchMerge(dims['4'], norm_layer))
            self.blocks.extend([
                ConvMlpBlock(
                    dim=dims['8'],
                    mlp_ratio=bridge_mlp_ratio,
                    drop_path=next(dpr),
                    norm_layer=norm_layer,
                    init_values=0.,
                    depth=depths[-1],
                    postnorm=postnorm,
                    deepnorm=deepnorm,
                    subln=subln,
                    swiglu=False,
                    naiveswiglu=False,
                ) for _ in range(depths[1])
            ])
            self.blocks.append(ConvPatchMerge(dims['8'], norm_layer))
        else:
            self.blocks.extend([
                Block(
                    dim=dims['4'],
                    num_heads=0,
                    mlp_ratio=bridge_mlp_ratio,
                    qkv_bias=qkv_bias,
                    qk_scale=qk_scale,
                    drop=drop,
                    attn_drop=attn_drop,
                    drop_path=next(dpr),
                    norm_layer=norm_layer,
                    init_values=init_values,
                    window_size=window_size,
                    depth=depths[-1],
                    postnorm=postnorm,
                    deepnorm=deepnorm,
                    subln=subln,
                    swiglu=swiglu,
                    naiveswiglu=naiveswiglu,
                ) for _ in range(depths[0])
            ])
            self.blocks.append(PatchMerge(dims['4'], norm_layer))
            self.blocks.extend([
                Block(
                    dim=dims['8'],
                    num_heads=0,
                    mlp_ratio=bridge_mlp_ratio,
                    qkv_bias=qkv_bias,
                    qk_scale=qk_scale,
                    drop=drop,
                    attn_drop=attn_drop,
                    drop_path=next(dpr),
                    norm_layer=norm_layer,
                    init_values=init_values,
                    window_size=window_size,
                    depth=depths[-1],
                    postnorm=postnorm,
                    deepnorm=deepnorm,
                    subln=subln,
                    swiglu=swiglu,
                    naiveswiglu=naiveswiglu,
                ) for _ in range(depths[1])
            ])
            self.blocks.append(PatchMerge(dims['8'], norm_layer))

        ######### stage 3 ########
        self.blocks.extend([
            Block(
                dim=dims['16'],
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop,
                attn_drop=attn_drop,
                drop_path=next(dpr),
                norm_layer=norm_layer,
                init_values=init_values,
                window_size=window_size,
                attn_head_dim=attn_head_dim,
                depth=depths[-1],
                postnorm=postnorm,
                deepnorm=deepnorm,
                subln=subln,
                swiglu=swiglu,
                naiveswiglu=naiveswiglu,
            ) for _ in range(depths[2])
        ])

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def get_num_layers(self):
        return len(self.blocks)

    @torch.jit.ignore
    def no_weight_decay(self):
        if self.cls_token is not None:
            return {'pos_embed', 'cls_token'}
        return {'pos_embed'}

    def get_classifer(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    @torch.jit.ignore
    def no_weight_decay_keywords(self):
        return {'relative_position_bias_table'}

    # def create_mask(self, image, image_anno):
    #     height = image.size(2)
    #     width = image.size(3)

    #     # Extract bounding box coordinates
    #     x0 = (image_anno[:, 0] * width).unsqueeze(1)
    #     y0 = (image_anno[:, 1] * height).unsqueeze(1)
    #     w = (image_anno[:, 2] * width).unsqueeze(1)
    #     h = (image_anno[:, 3] * height).unsqueeze(1)

    #     # Generate pixel indices
    #     x_indices = torch.arange(width, device=image.device)
    #     y_indices = torch.arange(height, device=image.device)

    #     # Create masks for x and y coordinates within the bounding boxes
    #     x_mask = ((x_indices >= x0) & (x_indices < x0 + w)).float()
    #     y_mask = ((y_indices >= y0) & (y_indices < y0 + h)).float()

    #     # Combine x and y masks to get final mask
    #     mask = x_mask.unsqueeze(1) * y_mask.unsqueeze(2) # (b,h,w)

    #     return mask

    # def prepare_tokens_with_masks(self, template_list, search_list, template_anno_list):
        # num_template = len(template_list)
        # num_search = len(search_list)

    #     z = torch.stack(template_list, dim=1)  # (b,n,c,h,w)
    #     z = z.view(-1, *z.size()[2:])  # (bn,c,h,w)
    #     x = torch.stack(search_list, dim=1)  # (b,n,c,h,w)
    #     x = x.view(-1, *x.size()[2:])  # (bn,c,h,w)
    #     z_anno = torch.stack(template_anno_list, dim=1)  # (b,n,4)
    #     z_anno = z_anno.view(-1, *z_anno.size()[2:])  # (bn,4)
    #     if self.token_type_indicate:
    #         # generate the indicate_embeddings for z
    #         z_indicate_mask = self.create_mask(z, z_anno)
    #         z_indicate_mask = z_indicate_mask.unfold(1, self.patch_size, self.patch_size).unfold(2, self.patch_size, self.patch_size) # to match the patch embedding
    #         z_indicate_mask = z_indicate_mask.mean(dim=(3,4)).flatten(1) # elements are in [0,1], float, near to 1 indicates near to foreground, near to 0 indicates near to background
    #         template_background_token = self.template_background_token.unsqueeze(0).unsqueeze(1).expand(z_indicate_mask.size(0), z_indicate_mask.size(1), self.embed_dim)
    #         template_foreground_token = self.template_foreground_token.unsqueeze(0).unsqueeze(1).expand(z_indicate_mask.size(0), z_indicate_mask.size(1), self.embed_dim)
    #         weighted_foreground = template_foreground_token * z_indicate_mask.unsqueeze(-1)
    #         weighted_background = template_background_token * (1 - z_indicate_mask.unsqueeze(-1))
    #         z_indicate = weighted_foreground + weighted_background

    #     z = self.patch_embed(z)
    #     x = self.patch_embed(x)
    #     # forward stage1&2
    #     if not self.convmlp and self.stop_grad_conv1:
    #         x = x.detach() * 0.9 + x * 0.1

    #     for blk in self.blocks[:-self.num_main_blocks]:
    #         z = checkpoint.checkpoint(blk, z, use_reentrant=False) if self.grad_ckpt else blk(z)  # bn,c,h,w
    #         x = checkpoint.checkpoint(blk, x, use_reentrant=False) if self.grad_ckpt else blk(x)  # bn,c,h,w

    #     x = x.flatten(2).transpose(1, 2)  # bn,l,c
    #     z = z.flatten(2).transpose(1, 2)

    #     if self.cls_token is not None:
    #         cls_tokens = self.cls_token.expand(B, -1, -1)
    #         x = torch.cat([cls_tokens, x], dim=1)
    #     if self.pos_embed is not None:
    #         x = x + self.pos_embed[:, :self.num_patches_search, :]
    #         z = z + self.pos_embed[:, self.num_patches_search:, :]

    #     if self.token_type_indicate:
    #         # generate the indicate_embeddings for x
    #         x_indicate = self.search_token.unsqueeze(0).unsqueeze(1).expand(x.size(0), x.size(1), self.embed_dim)
    #         # add indicate_embeddings to z and x
    #         x = x + x_indicate
    #         z = z + z_indicate


        # z = z.view(-1, num_template, z.size(-2), z.size(-1))  # b,n,l,c
        # z = z.reshape(z.size(0), -1, z.size(-1))  # b,l,c
        # x = x.view(-1, num_search, x.size(-2), x.size(-1))
        # x = x.reshape(x.size(0), -1, x.size(-1))
        # xz = torch.cat([x, z], dim=1)
        # return xz
    # def forward_features(self, template_list, search_list,template_anno_list):
    #     xz = self.prepare_tokens_with_masks(template_list, search_list, template_anno_list)
    #     xz = self.pos_drop(xz)
    #     return xz
    def forward_features(self,z_0,z_1,x,identity,seqs_input):
        B_x,C_x,H_x,W_x = x.shape
        B_z,C_z,H_z,W_z = z_0.shape
        x = self.patch_embed(x)
        z_0 = self.patch_embed(z_0) 
        z_1 = self.patch_embed(z_1) 

        # forward stage1&2
        if not self.convmlp and self.stop_grad_conv1:
            x = x.detach() * 0.9 + x * 0.1
        for blk in self.blocks[:-self.num_main_blocks]:
            z_0 = checkpoint.checkpoint(blk, z_0, use_reentrant=False) if self.grad_ckpt else blk(z_0)  # bn,c,h,w
            z_1 = checkpoint.checkpoint(blk, z_1, use_reentrant=False) if self.grad_ckpt else blk(z_1)  # bn,c,h,w
            x = checkpoint.checkpoint(blk, x, use_reentrant=False) if self.grad_ckpt else blk(x)  # bn,c,h,w
        
        x = x.flatten(2).transpose(1,2)
        z_0 = z_0.flatten(2).transpose(1,2)
        z_1 = z_1.flatten(2).transpose(1,2)
  
        if self.cls_token is not None:
            cls_tokens = self.cls_token.expand(B_x,-1,-1)
            x = torch.cat([cls_tokens, x], dim=1)
        if self.pos_embed is not None:
            x = x + self.pos_embed[:, :self.num_patches_search, :]
            z_0 = z_0 + self.pos_embed[:, self.num_patches_search:, :]
            z_1 = z_1 + self.pos_embed[:, self.num_patches_search:, :]
        
        x = x + self.pos_drop(x)
        z_0 = z_0 + self.pos_drop(z_0)
        z_1 = z_1 + self.pos_drop(z_1)
        
        share_weight = self.word_embeddings.weight.T
        out_list = []

        x0 = self.bins*self.range
        y0 = self.bins*self.range + 1
        x1 = self.bins*self.range + 2
        y1 = self.bins*self.range + 3
        score = self.bins*self.range + 5

        command= torch.cat([torch.ones(B_x,1).to(x)*x0,torch.ones(B_x,1).to(x)*y0,torch.ones(B_x,1).to(x)*x1,torch.ones(B_x,1).to(x)*y1,torch.ones(B_x,1).to(x)*score],dim = 1)
 
        trajectory = seqs_input
        command = command.to(trajectory)

        seqs_input = torch.cat([trajectory, command], dim=1)

        seqs_input = seqs_input.to(torch.int64).to(x.device)
        output_x_feat = x.clone()

        tgt = self.word_embeddings(seqs_input).premute(1,0,2)

        x = self.patch_embed(x)
        z_0 = self.patch_embed(z_0)
        z_1 = z_1

        len_x = x.shape[1]
        len_z = z_0.shape[1]+z_1.shape[1]
        len_seq = seqs_input.shape[1]

        z_0 += identity[:, 0, :].repeat(B_z, z_0.shape[1], 1)
        z_1 += identity[:, 1, :].repeat(B_z, z_1.shape[1], 1)

        x += identity[:, 2, :].repeat(B_x, x.shape[1], 1)

        query_command_embed_ = self.position_embeddings.weight.unsqueeze(1)
        prev_embed_ = self.prev_position_embeddings.weight.unsqueeze(1)
        query_seq_embed = torch.cat([query_command_embed_, prev_embed_], dim=0)
        
        query_seq_embed = query_seq_embed.repeat(1, B_x, 1)

        tgt = tgt.transpose(0,11)

        query_seq_embed = query_seq_embed.transpose(0,1)

        mask = generate_square_subsequent_mask(len_z,len_x,len_seq).to(tgt.device)

        tgt +=query_seq_embed[:,:tgt.shape[1]]

        z = torch.cat((z_0, z_1), dim=1)

        zx = combine_tokens(z,x,mode = self.cat_mode)
        zxs = torch.cat((zx,tgt),dim=1)

        for blk in self.blocks[-self.num_main_blocks: ]:
            zxs = checkpoint.checkpoint(blk, zxs, use_reentrant=False) if self.grad_ckpt else blk(zxs)  # bn,c,h,w
        
        zxs = self.norm(zxs)

        lens_z_single = z_0.shape[1]

        z_0_feat = zxs[:, :lens_z_single]
        z_1_feat = zxs[:, lens_z_single:lens_z_single*2]
        x_feat = zxs[:, lens_z_single*2:lens_z_single*2+len_x]

        x_out = self.norm(zxs[:, -5:-1])
        score_feat = zxs[:, -1]
        seq_feat = x_out

        possibility = torch.matmul(x_out, share_weight)
        out = possibility + self.output_bias
        temp = out.transpose(0, 1)

        out_list.append(out.unsqueeze(0))
        out = out.softmax(-1)

        value, extra_seq = out.topk(dim=-1, k=1)[0], out.topk(dim=-1, k=1)[1]
        for i in range(4):
            value, extra_seq = out[:, i, :].topk(dim=-1, k=1)[0], out[:, i, :].topk(dim=-1, k=1)[1]
            if i == 0:
                seqs_output = extra_seq
                values = value
            else:
                seqs_output = torch.cat([seqs_output, extra_seq], dim=-1)
                values = torch.cat([values, value], dim=-1)

        output = {'seqs': seqs_output, 'class': values, 'feat': temp, "state": "val/test", "x_feat": output_x_feat.detach(), "seq_feat": seq_feat}

        return output, z_0_feat, z_1_feat, x_feat, score_feat
        # seqs_input = seqs_input.to(torch.int64).to(x.device)
        # tgt = self.word_embeddings(seqs_input).permute(1, 0, 2)
        # query_embed = self.position_embeddings.weight.unsqueeze(1)
        # query_embed = query_embed.repeat(1, B_x, 1)

        # tgt = tgt.transpose(0, 1)
        # query_embed = query_embed.transpose(0, 1)
        # len_x = x.shape[1]
        # len_z = z_0.shape[1] + z_1.shape[1]
        # len_seq = seqs_input.shape[1]

        # mask = generate_square_subsequent_mask(len_z, len_x, len_seq).to(tgt.device)

        # tgt += query_embed
        
        # z_0 += identity[:, 0, :].repeat(B_z, z_0.shape[1], 1)
        # z_1 += identity[:, 1, :].repeat(B_z, z_1.shape[1], 1)

        # x += identity[:, 2, :].repeat(B_x, x.shape[1], 1)

        # z = torch.cat((z_0, z_1), dim=1)

        # x = combine_tokens(z, x, mode=self.cat_mode)
        # x = torch.cat((x, tgt), dim=1)
        
        
        rel_pos_bias = self.rel_pos_bias() if self.rel_pos_bias is not None else None
        for blk in self.blocks[-self.num_main_blocks:]:
            x = checkpoint.checkpoint(blk, x, rel_pos_bias) if self.grad_ckpt else blk(x, rel_pos_bias)

        x = self.norm(x)

        # for i, blk in enumerate(self.final_blocks):
        #     x = blk(x, padding_mask=mask)
        x_out = self.norm(x[:, -5:-1])
        score_feat = x[:, -1]

        lens_z = z_0.shape[1]
        lens_x = x.shape[1]

        z_0_feat = x[:, :lens_z]
        z_1_feat = x[:, lens_z:lens_z*2]
        x_feat = x[:, lens_z*2:lens_z*2+lens_x]

        #x = recover_tokens(x, lens_z, lens_x, mode=self.cat_mode)
        at = torch.matmul(x_out, share_weight)
        at = at + self.output_bias
        at = at[:, -4:]
        at = at.transpose(0, 1)
        output = {'feat': at, 'score_feat':score_feat, "state": "train"}

        return output, z_0_feat, z_1_feat, x_feat

    def finetune_track(self, cfg, patch_start_index=1):

        search_size = to_2tuple(cfg.DATA.SEARCH.SIZE)
        template_size = to_2tuple(cfg.DATA.TEMPLATE.SIZE)
        new_patch_size = cfg.MODEL.BACKBONE.STRIDE

        self.cat_mode = cfg.MODEL.BACKBONE.CAT_MODE
        self.return_inter = cfg.MODEL.RETURN_INTER
        self.add_sep_seg = cfg.MODEL.BACKBONE.SEP_SEG

        # resize patch embedding
        if new_patch_size != self.patch_size:
            print('Inconsistent Patch Size With The Pretrained Weights, Interpolate The Weight!')
            old_patch_embed = {}
            for name, param in self.patch_embed.named_parameters():
                if 'weight' in name:
                    param = nn.functional.interpolate(param, size=(new_patch_size, new_patch_size),
                                                      mode='bicubic', align_corners=False)
                    param = nn.Parameter(param)
                old_patch_embed[name] = param
            self.patch_embed = PatchEmbed_true(img_size=self.img_size, patch_size=new_patch_size, in_chans=3,
                                          embed_dim=self.embed_dim)
            self.patch_embed.proj.bias = old_patch_embed['proj.bias']
            self.patch_embed.proj.weight = old_patch_embed['proj.weight']

        # for patch embedding
        patch_pos_embed = self.pos_embed[:, patch_start_index:, :]
        patch_pos_embed = patch_pos_embed.transpose(1, 2)
        B, E, Q = patch_pos_embed.shape
        P_H, P_W = self.img_size[0] // self.patch_size, self.img_size[1] // self.patch_size
        Q = patch_pos_embed.shape[-1]
        if Q != P_H * P_W:
             patch_pos_embed = patch_pos_embed[:, :, patch_start_index:patch_start_index + P_H * P_W]
        patch_pos_embed = patch_pos_embed.view(B, E, P_H, P_W)
        # for search region
        H, W = search_size
        new_P_H, new_P_W = H // new_patch_size, W // new_patch_size
        search_patch_pos_embed = nn.functional.interpolate(patch_pos_embed, size=(new_P_H, new_P_W), mode='bicubic',
                                                           align_corners=False)
        search_patch_pos_embed = search_patch_pos_embed.flatten(2).transpose(1, 2)

        # for template region
        H, W = template_size
        new_P_H, new_P_W = H // new_patch_size, W // new_patch_size
        template_patch_pos_embed = nn.functional.interpolate(patch_pos_embed, size=(new_P_H, new_P_W), mode='bicubic',
                                                             align_corners=False)
        template_patch_pos_embed = template_patch_pos_embed.flatten(2).transpose(1, 2)
        # pos_embed_z0_tensor = torch.randn(4)
        # pos_embed_z1_tensor = torch.randn(4)
        # pos_embed_x_tensor = torch.randn(16)
        # self.pos_embed_z0 = nn.Parameter(pos_embed_z0_tensor)
        # self.pos_embed_z1 = nn.Parameter(pos_embed_z1_tensor)
        # self.pos_embed_x = nn.Parameter(pos_embed_x_tensor)

        # for cls token (keep it but not used)
        if self.cls_token and patch_start_index > 0:
            cls_pos_embed = self.pos_embed[:, 0:1, :]
            self.cls_pos_embed = nn.Parameter(cls_pos_embed)

        # separate token and segment token
        if self.add_sep_seg:
            self.template_segment_pos_embed = nn.Parameter(torch.zeros(1, 1, self.embed_dim))
            self.template_segment_pos_embed = trunc_normal_(self.template_segment_pos_embed, std=.02)
            self.search_segment_pos_embed = nn.Parameter(torch.zeros(1, 1, self.embed_dim))
            self.search_segment_pos_embed = trunc_normal_(self.search_segment_pos_embed, std=.02)

   
       
         

    def forward(self, z_0, z_1, x, identity, seqs_input, **kwargs):
        """
        Joint feature extraction and relation modeling for the basic ViT backbone.
        Args:
            z (torch.Tensor): template feature, [B, C, H_z, W_z]
            x (torch.Tensor): search region feature, [B, C, H_x, W_x]

        Returns:
            x (torch.Tensor): merged template and search region feature, [B, L_z+L_x, C]
            attn : None
        """
        output = self.forward_features(z_0, z_1, x, identity, seqs_input)

        return output

def load_pretrained(model, checkpoint, pos_type):
    if "module" in checkpoint.keys():
        # adjust position encoding
        state_dict = checkpoint["module"]
    elif "model" in checkpoint.keys():
        state_dict = checkpoint["model"]
    else:
        state_dict = checkpoint
    pe = state_dict['pos_embed'].float()
    b_pe, hw_pe, c_pe = pe.shape
    side_pe = int(math.sqrt(hw_pe))
    side_num_patches_search = int(math.sqrt(model.num_patches_search))
    side_num_patches_template = int(math.sqrt(model.num_patches_template))
    pe_2D = pe.reshape([b_pe, side_pe, side_pe, c_pe]).permute([0,3,1,2])  #b,c,h,w

    def adjust_pe(pe_2D, side_pe, side_new):
        if pos_type == 'index':
            if side_pe < side_new:
                pe_new_2D = nn.functional.interpolate(pe_2D, [side_new, side_new], align_corners=True, mode='bicubic')
                warnings.warn('The resolution is too large, the POS_TYPE has been modified to \'interpolate\'')
            else:
                pe_new_2D = pe_2D[:,:,0:side_new,0:side_new]
            pe_new = torch.flatten(pe_new_2D.permute([0, 2, 3, 1]), 1, 2)
        elif pos_type == 'interpolate':
            pe_new_2D = nn.functional.interpolate(pe_2D, [side_new, side_new], align_corners=True, mode='bicubic')
            pe_new = torch.flatten(pe_new_2D.permute([0, 2, 3, 1]), 1, 2)#b,l,c
        else:
            raise NotImplementedError('The POS_TYPE should be index or interpolate')
        return pe_new

    if side_pe != side_num_patches_search:
        pe_s = adjust_pe(pe_2D, side_pe, side_num_patches_search)
    else:
        pe_s = pe
    if side_pe != side_num_patches_template:
        pe_t = adjust_pe(pe_2D, side_pe, side_num_patches_template)
    else:
        pe_t = pe
    pe_xz = torch.cat((pe_s, pe_t), dim=1)
    state_dict['pos_embed'] = pe_xz
    auxiliary_keys = ["template_background_token", "template_foreground_token", "search_token"]
    for key in auxiliary_keys:
        if (key in model.state_dict().keys()) and (key not in state_dict.keys()):
            state_dict[key] = model.state_dict()[key]

    model.load_state_dict(state_dict, strict=False)


@register_model
def fastitpnt(pretrained=False, pos_type="interpolate", pretrain_type="",bins = 400,range = 2, prenum =7,search_size = 256,template_size = 128, **kwargs):
    model = Fast_iTPN(
        search_size=search_size,
        template_size=template_size,
        patch_size=16, embed_dim=384, depth_stage1=1, depth_stage2=1, depth=12, num_heads=6, bridge_mlp_ratio=3.,
        mlp_ratio=3., qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6),
        convmlp=True,
        naiveswiglu=True,
        subln=True,
        pos_type=pos_type,
        bins = bins,
        range_time = range,
        prenum= prenum,
        **kwargs)
    model.default_cfg = _cfg()
    #TODO :fix the pretrain_path
    pretrain_path = "/home/ubuntu/fishworld/project/hjd/hjd/ARTrack/pretrained_models/fast_itpn_tiny_1600e_1k.pt"
    if pretrained:
        checkpoint = torch.load(pretrain_path, map_location="cpu")
        load_pretrained(model,checkpoint,pos_type)
    return model


@register_model
def fastitpns(pretrained=False, pos_type="interpolate", pretrain_type="", **kwargs):
    model = Fast_iTPN(
        patch_size=16, embed_dim=384, depth_stage1=2, depth_stage2=2, depth=20, num_heads=6, bridge_mlp_ratio=3.,
        mlp_ratio=3., qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6),
        convmlp=True,
        naiveswiglu=True,
        subln=True,
        pos_type=pos_type,
        **kwargs)
    model.default_cfg = _cfg()
    pretrain_path = current_file_path +  pretrain_type
    if pretrained:
        checkpoint = torch.load(pretrain_path, map_location="cpu")
        load_pretrained(model,checkpoint,pos_type)
    return model

@register_model
def fastitpnb(pretrained=False,pos_type="interpolate",pretrain_type="",**kwargs):
    model = Fast_iTPN(
        patch_size=16, embed_dim=512, depth_stage1=3, depth_stage2=3, depth=24, num_heads=8, bridge_mlp_ratio=3.,
        mlp_ratio=3., qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6),
        convmlp=True,
        naiveswiglu=True,
        subln=True,
        pos_type = pos_type,
        **kwargs)
    model.default_cfg = _cfg()
    pretrain_path = current_file_path + pretrain_type
    if pretrained:
        checkpoint = torch.load(pretrain_path, map_location="cpu")
        load_pretrained(model,checkpoint,pos_type)
        # model.load_state_dict(checkpoint["model"])
    return model


@register_model
def fastitpnl(pretrained=False,pos_type="interpolate",pretrain_type="", **kwargs):
    model = Fast_iTPN(
        patch_size=16, embed_dim=768, depth_stage1=2, depth_stage2=2, depth=40, num_heads=12, bridge_mlp_ratio=3.,
        mlp_ratio=3., qkv_bias=True, norm_layer=partial(nn.LayerNorm, eps=1e-6),
        convmlp=True,
        naiveswiglu=True,
        subln=True,
        pos_type="interpolate",
        **kwargs)
    model.default_cfg = _cfg()
    pretrain_path = current_file_path + pretrain_type
    if pretrained:
        checkpoint = torch.load(pretrain_path, map_location="cpu")
        load_pretrained(model,checkpoint,pos_type)
    return model

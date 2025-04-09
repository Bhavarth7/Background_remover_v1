import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
import numpy as np
from timm.layers import DropPath, to_2tuple, trunc_normal_
from PIL import Image, ImageOps
from torchvision import transforms
import gradio as gr
import click
from pathlib import Path
import os
import uuid
import shutil
from zipfile import ZipFile
from typing import List

# Set random seed for reproducibility
def set_random_seed(seed):
    import random
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
set_random_seed(9)

torch.set_float32_matmul_precision('highest')

# Model Components
class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x

def window_partition(x, window_size):
    B, H, W, C = x.shape
    x = x.view(B, H // window_size, window_size, W // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    return windows

def window_reverse(windows, window_size, H, W):
    B = int(windows.shape[0] / (H * W / window_size / window_size))
    x = windows.view(B, H // window_size, W // window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    return x

class WindowAttention(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=True, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads))
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))
        coords_flatten = torch.flatten(coords, 1)
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()
        relative_coords[:, :, 0] += self.window_size[0] - 1
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)
        self.register_buffer("relative_position_index", relative_position_index)
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):
        B_, N, C = x.shape
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        relative_position_bias = self.relative_position_bias_table[self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1], -1)
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()
        attn = attn + relative_position_bias.unsqueeze(0)
        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

class SwinTransformerBlock(nn.Module):
    def __init__(self, dim, num_heads, window_size=7, shift_size=0, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0., drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio
        assert 0 <= self.shift_size < self.window_size, "shift_size must in 0-window_size"
        self.norm1 = norm_layer(dim)
        self.attn = WindowAttention(dim, window_size=to_2tuple(self.window_size), num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.H = None
        self.W = None

    def forward(self, x, mask_matrix):
        B, L, C = x.shape
        H, W = self.H, self.W
        assert L == H * W, "input feature has wrong size"
        shortcut = x
        x = self.norm1(x)
        x = x.view(B, H, W, C)
        pad_l = pad_t = 0
        pad_r = (self.window_size - W % self.window_size) % self.window_size
        pad_b = (self.window_size - H % self.window_size) % self.window_size
        x = F.pad(x, (0, 0, pad_l, pad_r, pad_t, pad_b))
        _, Hp, Wp, _ = x.shape
        if self.shift_size > 0:
            shifted_x = torch.roll(x, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
            attn_mask = mask_matrix
        else:
            shifted_x = x
            attn_mask = None
        x_windows = window_partition(shifted_x, self.window_size)
        x_windows = x_windows.view(-1, self.window_size * self.window_size, C)
        attn_windows = self.attn(x_windows, mask=attn_mask)
        attn_windows = attn_windows.view(-1, self.window_size, self.window_size, C)
        shifted_x = window_reverse(attn_windows, self.window_size, Hp, Wp)
        if self.shift_size > 0:
            x = torch.roll(shifted_x, shifts=(self.shift_size, self.shift_size), dims=(1, 2))
        else:
            x = shifted_x
        if pad_r > 0 or pad_b > 0:
            x = x[:, :H, :W, :].contiguous()
        x = x.view(B, H * W, C)
        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x

class PatchMerging(nn.Module):
    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.reduction = nn.Linear(4 * dim, 2 * dim, bias=False)
        self.norm = norm_layer(4 * dim)

    def forward(self, x, H, W):
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"
        x = x.view(B, H, W, C)
        pad_input = (H % 2 == 1) or (W % 2 == 1)
        if pad_input:
            x = F.pad(x, (0, 0, 0, W % 2, 0, H % 2))
        x0 = x[:, 0::2, 0::2, :]
        x1 = x[:, 1::2, 0::2, :]
        x2 = x[:, 0::2, 1::2, :]
        x3 = x[:, 1::2, 1::2, :]
        x = torch.cat([x0, x1, x2, x3], -1)
        x = x.view(B, -1, 4 * C)
        x = self.norm(x)
        x = self.reduction(x)
        return x

class BasicLayer(nn.Module):
    def __init__(self, dim, depth, num_heads, window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop=0., attn_drop=0., drop_path=0., norm_layer=nn.LayerNorm, downsample=None, use_checkpoint=False):
        super().__init__()
        self.window_size = window_size
        self.shift_size = window_size // 2
        self.depth = depth
        self.use_checkpoint = use_checkpoint
        self.blocks = nn.ModuleList([
            SwinTransformerBlock(dim=dim, num_heads=num_heads, window_size=window_size, shift_size=0 if (i % 2 == 0) else window_size // 2, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop, attn_drop=attn_drop, drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path, norm_layer=norm_layer)
            for i in range(depth)])
        self.downsample = downsample(dim=dim, norm_layer=norm_layer) if downsample else None

    def forward(self, x, H, W):
        Hp = int(np.ceil(H / self.window_size)) * self.window_size
        Wp = int(np.ceil(W / self.window_size)) * self.window_size
        img_mask = torch.zeros((1, Hp, Wp, 1), device=x.device)
        h_slices = (slice(0, -self.window_size), slice(-self.window_size, -self.shift_size), slice(-self.shift_size, None))
        w_slices = (slice(0, -self.window_size), slice(-self.window_size, -self.shift_size), slice(-self.shift_size, None))
        cnt = 0
        for h in h_slices:
            for w in w_slices:
                img_mask[:, h, w, :] = cnt
                cnt += 1
        mask_windows = window_partition(img_mask, self.window_size)
        mask_windows = mask_windows.view(-1, self.window_size * self.window_size)
        attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
        attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
        for blk in self.blocks:
            blk.H, blk.W = H, W
            x = blk(x, attn_mask)
        if self.downsample:
            x_down = self.downsample(x, H, W)
            Wh, Ww = (H + 1) // 2, (W + 1) // 2
            return x, H, W, x_down, Wh, Ww
        return x, H, W, x, H, W

class PatchEmbed(nn.Module):
    def __init__(self, patch_size=4, in_chans=3, embed_dim=96, norm_layer=None):
        super().__init__()
        patch_size = to_2tuple(patch_size)
        self.patch_size = patch_size
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)
        self.norm = norm_layer(embed_dim) if norm_layer else None

    def forward(self, x):
        _, _, H, W = x.size()
        if W % self.patch_size[1] != 0:
            x = F.pad(x, (0, self.patch_size[1] - W % self.patch_size[1]))
        if H % self.patch_size[0] != 0:
            x = F.pad(x, (0, 0, 0, self.patch_size[0] - H % self.patch_size[0]))
        x = self.proj(x)
        if self.norm:
            Wh, Ww = x.size(2), x.size(3)
            x = x.flatten(2).transpose(1, 2)
            x = self.norm(x)
            x = x.transpose(1, 2).view(-1, self.embed_dim, Wh, Ww)
        return x

class SwinTransformer(nn.Module):
    def __init__(self, pretrain_img_size=224, patch_size=4, in_chans=3, embed_dim=96, depths=[2, 2, 6, 2], num_heads=[3, 6, 12, 24], window_size=7, mlp_ratio=4., qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.2, norm_layer=nn.LayerNorm, ape=False, patch_norm=True, out_indices=(0, 1, 2, 3), frozen_stages=-1, use_checkpoint=False):
        super().__init__()
        self.pretrain_img_size = pretrain_img_size
        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.ape = ape
        self.patch_norm = patch_norm
        self.out_indices = out_indices
        self.frozen_stages = frozen_stages
        self.patch_embed = PatchEmbed(patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim, norm_layer=norm_layer if self.patch_norm else None)
        if self.ape:
            pretrain_img_size = to_2tuple(pretrain_img_size)
            patch_size = to_2tuple(patch_size)
            patches_resolution = [pretrain_img_size[0] // patch_size[0], pretrain_img_size[1] // patch_size[1]]
            self.absolute_pos_embed = nn.Parameter(torch.zeros(1, embed_dim, patches_resolution[0], patches_resolution[1]))
            trunc_normal_(self.absolute_pos_embed, std=.02)
        self.pos_drop = nn.Dropout(p=drop_rate)
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer), depth=depths[i_layer], num_heads=num_heads[i_layer], window_size=window_size, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])], norm_layer=norm_layer, downsample=PatchMerging if (i_layer < self.num_layers - 1) else None, use_checkpoint=use_checkpoint)
            self.layers.append(layer)
        num_features = [int(embed_dim * 2 ** i) for i in range(self.num_layers)]
        self.num_features = num_features
        for i_layer in out_indices:
            layer = norm_layer(num_features[i_layer])
            layer_name = f'norm{i_layer}'
            self.add_module(layer_name, layer)
        self._freeze_stages()

    def _freeze_stages(self):
        if self.frozen_stages >= 0:
            self.patch_embed.eval()
            for param in self.patch_embed.parameters():
                param.requires_grad = False
        if self.frozen_stages >= 1 and self.ape:
            self.absolute_pos_embed.requires_grad = False
        if self.frozen_stages >= 2:
            self.pos_drop.eval()
            for i in range(0, self.frozen_stages - 1):
                m = self.layers[i]
                m.eval()
                for param in m.parameters():
                    param.requires_grad = False

    def forward(self, x):
        x = self.patch_embed(x)
        Wh, Ww = x.size(2), x.size(3)
        if self.ape:
            absolute_pos_embed = F.interpolate(self.absolute_pos_embed, size=(Wh, Ww), mode='bicubic')
            x = (x + absolute_pos_embed)
        outs = [x.contiguous()]
        x = x.flatten(2).transpose(1, 2)
        x = self.pos_drop(x)
        for i in range(self.num_layers):
            layer = self.layers[i]
            x_out, H, W, x, Wh, Ww = layer(x, Wh, Ww)
            if i in self.out_indices:
                norm_layer = getattr(self, f'norm{i}')
                x_out = norm_layer(x_out)
                out = x_out.view(-1, H, W, self.num_features[i]).permute(0, 3, 1, 2).contiguous()
                outs.append(out)
        return tuple(outs)

def get_activation_fn(activation):
    if activation == "gelu":
        return F.gelu
    raise RuntimeError(f"activation should be gelu, not {activation}.")

def make_cbr(in_dim, out_dim):
    return nn.Sequential(nn.Conv2d(in_dim, out_dim, kernel_size=3, padding=1), nn.InstanceNorm2d(out_dim), nn.GELU())

def make_cbg(in_dim, out_dim):
    return nn.Sequential(nn.Conv2d(in_dim, out_dim, kernel_size=3, padding=1), nn.InstanceNorm2d(out_dim), nn.GELU())

def rescale_to(x, scale_factor: float = 2, interpolation='nearest'):
    return F.interpolate(x, scale_factor=scale_factor, mode=interpolation)

def resize_as(x, y, interpolation='bilinear'):
    return F.interpolate(x, size=y.shape[-2:], mode=interpolation)

def image2patches(x):
    return rearrange(x, 'b c (hg h) (wg w) -> (hg wg b) c h w', hg=2, wg=2)

def patches2image(x):
    return rearrange(x, '(hg wg b) c h w -> b c (hg h) (wg w)', hg=2, wg=2)

class PositionEmbeddingSine:
    def __init__(self, num_pos_feats=64, temperature=10000, normalize=False, scale=None):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * math.pi
        self.scale = scale
        self.dim_t = torch.arange(0, self.num_pos_feats, dtype=torch.float32)

    def __call__(self, b, h, w):
        device = self.dim_t.device
        mask = torch.zeros([b, h, w], dtype=torch.bool, device=device)
        not_mask = ~mask
        y_embed = not_mask.cumsum(dim=1, dtype=torch.float32)
        x_embed = not_mask.cumsum(dim=2, dtype=torch.float32)
        if self.normalize:
            eps = 1e-6
            y_embed = (y_embed - 0.5) / (y_embed[:, -1:, :] + eps) * self.scale
            x_embed = (x_embed - 0.5) / (x_embed[:, :, -1:] + eps) * self.scale
        dim_t = self.temperature ** (2 * (self.dim_t.to(device) // 2) / self.num_pos_feats)
        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack((pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()), dim=4).flatten(3)
        pos_y = torch.stack((pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()), dim=4).flatten(3)
        return torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)

class MCLM(nn.Module):
    def __init__(self, d_model, num_heads, pool_ratios=[1, 4, 8]):
        super().__init__()
        self.attention = nn.ModuleList([nn.MultiheadAttention(d_model, num_heads, dropout=0.1) for _ in range(5)])
        self.linear1 = nn.Linear(d_model, d_model * 2)
        self.linear2 = nn.Linear(d_model * 2, d_model)
        self.linear3 = nn.Linear(d_model, d_model * 2)
        self.linear4 = nn.Linear(d_model * 2, d_model)
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout = nn.Dropout(0.1)
        self.dropout1 = nn.Dropout(0.1)
        self.dropout2 = nn.Dropout(0.1)
        self.activation = get_activation_fn('gelu')
        self.pool_ratios = pool_ratios
        self.p_poses = []
        self.g_pos = None
        self.positional_encoding = PositionEmbeddingSine(num_pos_feats=d_model // 2, normalize=True)

    def forward(self, l, g):
        self.p_poses = []
        self.g_pos = None
        b, c, h, w = l.size()
        concated_locs = rearrange(l, '(hg wg b) c h w -> b c (hg h) (wg w)', hg=2, wg=2)
        pools = []
        for pool_ratio in self.pool_ratios:
            tgt_hw = (round(h / pool_ratio), round(w / pool_ratio))
            pool = F.adaptive_avg_pool2d(concated_locs, tgt_hw)
            pools.append(rearrange(pool, 'b c h w -> (h w) b c'))
            if self.g_pos is None:
                pos_emb = self.positional_encoding(pool.shape[0], pool.shape[2], pool.shape[3])
                pos_emb = rearrange(pos_emb, 'b c h w -> (h w) b c')
                self.p_poses.append(pos_emb)
        pools = torch.cat(pools, 0)
        if self.g_pos is None:
            self.p_poses = torch.cat(self.p_poses, dim=0)
            pos_emb = self.positional_encoding(g.shape[0], g.shape[2], g.shape[3])
            self.g_pos = rearrange(pos_emb, 'b c h w -> (h w) b c')
        device = pools.device
        self.p_poses = self.p_poses.to(device)
        self.g_pos = self.g_pos.to(device)
        g_hw_b_c = rearrange(g, 'b c h w -> (h w) b c')
        g_hw_b_c = g_hw_b_c + self.dropout1(self.attention[0](g_hw_b_c + self.g_pos, pools + self.p_poses, pools)[0])
        g_hw_b_c = self.norm1(g_hw_b_c)
        g_hw_b_c = g_hw_b_c + self.dropout2(self.linear2(self.dropout(self.activation(self.linear1(g_hw_b_c)))))
        g_hw_b_c = self.norm2(g_hw_b_c)
        l_hw_b_c = rearrange(l, "b c h w -> (h w) b c")
        _g_hw_b_c = rearrange(g_hw_b_c, '(h w) b c -> h w b c', h=h, w=w)
        _g_hw_b_c = rearrange(_g_hw_b_c, "(ng h) (nw w) b c -> (h w) (ng nw b) c", ng=2, nw=2)
        outputs_re = []
        for i, (_l, _g) in enumerate(zip(l_hw_b_c.chunk(4, dim=1), _g_hw_b_c.chunk(4, dim=1))):
            outputs_re.append(self.attention[i + 1](_l, _g, _g)[0])
        outputs_re = torch.cat(outputs_re, 1)
        l_hw_b_c = l_hw_b_c + self.dropout1(outputs_re)
        l_hw_b_c = self.norm1(l_hw_b_c)
        l_hw_b_c = l_hw_b_c + self.dropout2(self.linear4(self.dropout(self.activation(self.linear3(l_hw_b_c)))))
        l_hw_b_c = self.norm2(l_hw_b_c)
        l = torch.cat((l_hw_b_c, g_hw_b_c), 1)
        return rearrange(l, "(h w) b c -> b c h w", h=h, w=w)

class MCRM(nn.Module):
    def __init__(self, d_model, num_heads, pool_ratios=[4, 8, 16]):
        super().__init__()
        self.attention = nn.ModuleList([nn.MultiheadAttention(d_model, num_heads, dropout=0.1) for _ in range(4)])
        self.linear3 = nn.Linear(d_model, d_model * 2)
        self.linear4 = nn.Linear(d_model * 2, d_model)
        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout = nn.Dropout(0.1)
        self.dropout1 = nn.Dropout(0.1)
        self.dropout2 = nn.Dropout(0.1)
        self.sigmoid = nn.Sigmoid()
        self.activation = get_activation_fn('gelu')
        self.sal_conv = nn.Conv2d(d_model, 1, 1)
        self.pool_ratios = pool_ratios

    def forward(self, x):
        device = x.device
        b, c, h, w = x.size()
        loc, glb = x.split([4, 1], dim=0)
        patched_glb = rearrange(glb, 'b c (hg h) (wg w) -> (hg wg b) c h w', hg=2, wg=2)
        token_attention_map = self.sigmoid(self.sal_conv(glb))
        token_attention_map = F.interpolate(token_attention_map, size=patches2image(loc).shape[-2:], mode='nearest')
        loc = loc * rearrange(token_attention_map, 'b c (hg h) (wg w) -> (hg wg b) c h w', hg=2, wg=2)
        pools = []
        for pool_ratio in self.pool_ratios:
            tgt_hw = (round(h / pool_ratio), round(w / pool_ratio))
            pool = F.adaptive_avg_pool2d(patched_glb, tgt_hw)
            pools.append(rearrange(pool, 'nl c h w -> nl c (h w)'))
        pools = rearrange(torch.cat(pools, 2), "nl c nphw -> nl nphw 1 c")
        loc_ = rearrange(loc, 'nl c h w -> nl (h w) 1 c')
        outputs = []
        for i, q in enumerate(loc_.unbind(dim=0)):
            v = pools[i]
            k = v
            outputs.append(self.attention[i](q, k, v)[0])
        outputs = torch.cat(outputs, 1)
        src = loc.view(4, c, -1).permute(2, 0, 1) + self.dropout1(outputs)
        src = self.norm1(src)
        src = src + self.dropout2(self.linear4(self.dropout(self.activation(self.linear3(src)))))
        src = self.norm2(src)
        src = src.permute(1, 2, 0).reshape(4, c, h, w)
        glb = glb + F.interpolate(patches2image(src), size=glb.shape[-2:], mode='nearest')
        return torch.cat((src, glb), 0), token_attention_map

class BEN_Base(nn.Module):
    def __init__(self):
        super().__init__()
        self.backbone = SwinTransformer(embed_dim=128, depths=[2, 2, 18, 2], num_heads=[4, 8, 16, 32], window_size=12)
        emb_dim = 128
        self.sideout5 = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        self.sideout4 = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        self.sideout3 = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        self.sideout2 = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        self.sideout1 = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        self.output5 = make_cbr(1024, emb_dim)
        self.output4 = make_cbr(512, emb_dim)
        self.output3 = make_cbr(256, emb_dim)
        self.output2 = make_cbr(128, emb_dim)
        self.output1 = make_cbr(128, emb_dim)
        self.multifieldcrossatt = MCLM(emb_dim, 1, [1, 4, 8])
        self.conv1 = make_cbr(emb_dim, emb_dim)
        self.conv2 = make_cbr(emb_dim, emb_dim)
        self.conv3 = make_cbr(emb_dim, emb_dim)
        self.conv4 = make_cbr(emb_dim, emb_dim)
        self.dec_blk1 = MCRM(emb_dim, 1, [2, 4, 8])
        self.dec_blk2 = MCRM(emb_dim, 1, [2, 4, 8])
        self.dec_blk3 = MCRM(emb_dim, 1, [2, 4, 8])
        self.dec_blk4 = MCRM(emb_dim, 1, [2, 4, 8])
        self.insmask_head = nn.Sequential(
            nn.Conv2d(emb_dim, 384, kernel_size=3, padding=1),
            nn.InstanceNorm2d(384),
            nn.GELU(),
            nn.Conv2d(384, 384, kernel_size=3, padding=1),
            nn.InstanceNorm2d(384),
            nn.GELU(),
            nn.Conv2d(384, emb_dim, kernel_size=3, padding=1)
        )
        self.shallow = nn.Sequential(nn.Conv2d(3, emb_dim, kernel_size=3, padding=1))
        self.upsample1 = make_cbg(emb_dim, emb_dim)
        self.upsample2 = make_cbg(emb_dim, emb_dim)
        self.output = nn.Sequential(nn.Conv2d(emb_dim, 1, kernel_size=3, padding=1))
        for m in self.modules():
            if isinstance(m, nn.GELU) or isinstance(m, nn.Dropout):
                m.inplace = True

    @torch.inference_mode()
    @torch.autocast(device_type="cuda", dtype=torch.float16)
    def forward(self, x):
        real_batch = x.size(0)
        shallow_batch = self.shallow(x)
        glb_batch = rescale_to(x, scale_factor=0.5, interpolation='bilinear')
        final_input = None
        for i in range(real_batch):
            start = i * 4
            end = (i + 1) * 4
            loc_batch = image2patches(x[i, :, :, :].unsqueeze(dim=0))
            input_ = torch.cat((loc_batch, glb_batch[i, :, :, :].unsqueeze(dim=0)), dim=0)
            if final_input is None:
                final_input = input_
            else:
                final_input = torch.cat((final_input, input_), dim=0)
        features = self.backbone(final_input)
        outputs = []
        for i in range(real_batch):
            start = i * 5
            end = (i + 1) * 5
            f4 = features[4][start:end, :, :, :]
            f3 = features[3][start:end, :, :, :]
            f2 = features[2][start:end, :, :, :]
            f1 = features[1][start:end, :, :, :]
            f0 = features[0][start:end, :, :, :]
            e5 = self.output5(f4)
            e4 = self.output4(f3)
            e3 = self.output3(f2)
            e2 = self.output2(f1)
            e1 = self.output1(f0)
            loc_e5, glb_e5 = e5.split([4, 1], dim=0)
            e5 = self.multifieldcrossatt(loc_e5, glb_e5)
            e4, tokenattmap4 = self.dec_blk4(e4 + resize_as(e5, e4))
            e4 = self.conv4(e4)
            e3, tokenattmap3 = self.dec_blk3(e3 + resize_as(e4, e3))
            e3 = self.conv3(e3)
            e2, tokenattmap2 = self.dec_blk2(e2 + resize_as(e3, e2))
            e2 = self.conv2(e2)
            e1, tokenattmap1 = self.dec_blk1(e1 + resize_as(e2, e1))
            e1 = self.conv1(e1)
            loc_e1, glb_e1 = e1.split([4, 1], dim=0)
            output1_cat = patches2image(loc_e1)
            output1_cat = output1_cat + resize_as(glb_e1, output1_cat)
            final_output = self.insmask_head(output1_cat)
            shallow = shallow_batch[i, :, :, :].unsqueeze(dim=0)
            final_output = final_output + resize_as(shallow, final_output)
            final_output = self.upsample1(rescale_to(final_output))
            final_output = rescale_to(final_output + resize_as(shallow, final_output))
            final_output = self.upsample2(final_output)
            final_output = self.output(final_output)
            mask = final_output.sigmoid()
            outputs.append(mask)
        return torch.cat(outputs, dim=0)

    def loadcheckpoints(self, model_path):
        model_dict = torch.load(model_path, map_location="cpu", weights_only=True)
        self.load_state_dict(model_dict['model_state_dict'], strict=True)
        del model_dict

    def inference(self, image, refine_foreground=False):
        set_random_seed(9)
        if isinstance(image, Image.Image):
            image, h, w, original_image = rgb_loader_refiner(image)
            if torch.cuda.is_available():
                img_tensor = img_transform(image).unsqueeze(0).to(next(self.parameters()).device)
            else:
                img_tensor = img_transform32(image).unsqueeze(0).to(next(self.parameters()).device)
            with torch.no_grad():
                res = self.forward(img_tensor)
            if refine_foreground:
                pred_pil = transforms.ToPILImage()(res.squeeze())
                image_masked = refine_foreground_process(original_image, pred_pil)
                image_masked.putalpha(pred_pil.resize(original_image.size))
                return image_masked
            else:
                alpha = postprocess_image(res, im_size=[w, h])
                pred_pil = transforms.ToPILImage()(alpha)
                mask = pred_pil.resize(original_image.size)
                original_image.putalpha(mask)
                return original_image
        else:
            foregrounds = []
            for batch in image:
                image, h, w, original_image = rgb_loader_refiner(batch)
                if torch.cuda.is_available():
                    img_tensor = img_transform(image).unsqueeze(0).to(next(self.parameters()).device)
                else:
                    img_tensor = img_transform32(image).unsqueeze(0).to(next(self.parameters()).device)
                with torch.no_grad():
                    res = self.forward(img_tensor)
                if refine_foreground:
                    pred_pil = transforms.ToPILImage()(res.squeeze())
                    image_masked = refine_foreground_process(original_image, pred_pil)
                    image_masked.putalpha(pred_pil.resize(original_image.size))
                    foregrounds.append(image_masked)
                else:
                    alpha = postprocess_image(res, im_size=[w, h])
                    pred_pil = transforms.ToPILImage()(alpha)
                    mask = pred_pil.resize(original_image.size)
                    original_image.putalpha(mask)
                    foregrounds.append(original_image)
            return foregrounds

# Image Processing Utilities
def rgb_loader_refiner(original_image):
    h, w = original_image.size
    image = ImageOps.exif_transpose(original_image)
    if image.mode != 'RGB':
        image = image.convert('RGB')
    image = image.resize((1024, 1024), resample=Image.LANCZOS)
    return image, h, w, original_image

img_transform = transforms.Compose([
    transforms.ToTensor(),
    transforms.ConvertImageDtype(torch.float16),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

img_transform32 = transforms.Compose([
    transforms.ToTensor(),
    transforms.ConvertImageDtype(torch.float32),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
])

def refine_foreground_process(image, mask, r=90):
    if mask.size != image.size:
        mask = mask.resize(image.size)
    image = np.array(image) / 255.0
    mask = np.array(mask) / 255.0
    estimated_foreground = FB_blur_fusion_foreground_estimator_2(image, mask, r=r)
    return Image.fromarray((estimated_foreground * 255.0).astype(np.uint8))

def FB_blur_fusion_foreground_estimator_2(image, alpha, r=90):
    import cv2
    alpha = alpha[:, :, None]
    F, blur_B = FB_blur_fusion_foreground_estimator(image, image, image, alpha, r)
    return FB_blur_fusion_foreground_estimator(image, F, blur_B, alpha, r=6)[0]

def FB_blur_fusion_foreground_estimator(image, F, B, alpha, r=90):
    import cv2
    if isinstance(image, Image.Image):
        image = np.array(image) / 255.0
    blurred_alpha = cv2.blur(alpha, (r, r))[:, :, None]
    blurred_FA = cv2.blur(F * alpha, (r, r))
    blurred_F = blurred_FA / (blurred_alpha + 1e-5)
    blurred_B1A = cv2.blur(B * (1 - alpha), (r, r))
    blurred_B = blurred_B1A / ((1 - blurred_alpha) + 1e-5)
    F = blurred_F + alpha * (image - alpha * blurred_F - (1 - alpha) * blurred_B)
    F = np.clip(F, 0, 1)
    return F, blurred_B

def postprocess_image(result: torch.Tensor, im_size: list) -> np.ndarray:
    result = torch.squeeze(F.interpolate(result, size=im_size, mode='bilinear'), 0)
    ma = torch.max(result)
    mi = torch.min(result)
    result = (result - mi) / (ma - mi)
    im_array = (result * 255).permute(1, 2, 0).cpu().data.numpy().astype(np.uint8)
    return np.squeeze(im_array)

# Gradio Demo Integration
class Config:
    BASE_PATH = Path(__file__).parent / "src"
    DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    MODEL_PATH = BASE_PATH / "BEN2_Base.pth"
    RESULT_DIR = BASE_PATH / "result"
    TEMP_DIR = BASE_PATH / "temp"

class BackgroundRemover:
    def __init__(self):
        self.model = None
        self._initialize_environment()

    def _initialize_environment(self) -> None:
        Config.RESULT_DIR.mkdir(exist_ok=True)
        Config.TEMP_DIR.mkdir(exist_ok=True)
        self._load_model()

    def _load_model(self) -> None:
        self.model = BEN_Base().to(Config.DEVICE).eval()
        self.model.loadcheckpoints(str(Config.MODEL_PATH))

    def _sanitize_filename(self, file_path: str) -> str:
        name = Path(file_path).stem
        ext = Path(file_path).suffix
        return f"{name}_{uuid.uuid4().hex[:6]}{ext}"

    def process_image(self, image_path: str) -> str:
        image = Image.open(image_path)
        output = self.model.inference(image)
        output_path = Config.RESULT_DIR / self._sanitize_filename(image_path)
        output.save(output_path)
        return str(output_path)

    def process_batch(self, image_paths: List[str]) -> str:
        batch_id = uuid.uuid4().hex[:6]
        temp_dir = Config.TEMP_DIR / f"batch_{batch_id}"
        temp_dir.mkdir(exist_ok=True)
        try:
            for path in image_paths:
                processed = self.process_image(path)
                shutil.move(processed, temp_dir)
            zip_path = f"{temp_dir}.zip"
            with ZipFile(zip_path, 'w') as zipf:
                for file in temp_dir.iterdir():
                    zipf.write(file, file.name)
            return zip_path
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

def create_gradio_interface(remover: BackgroundRemover):
    def handle_upload(files: List[str]):
        if not files:
            return None, None, None
        if len(files) == 1:
            output_path = remover.process_image(files[0])
            return Image.open(files[0]), Image.open(output_path), output_path
        else:
            zip_path = remover.process_batch(files)
            return Image.open(files[0]), "Batch processed - Download ZIP", zip_path

    with gr.Blocks(title="Background Removal") as interface:
        gr.Markdown("## Background Removal Tool")
        gr.Markdown("Upload images and click Submit to remove backgrounds. Single image shows preview, multiple images return a ZIP.")
        with gr.Row():
            with gr.Column():
                file_input = gr.File(file_count="multiple", file_types=["image"], label="Upload Images")
                submit_btn = gr.Button("Submit")
                input_image = gr.Image(label="Input Image")
            with gr.Column():
                output_image = gr.Image(label="Output Image")
                download_file = gr.File(label="Download Results")
        submit_btn.click(fn=handle_upload, inputs=file_input, outputs=[input_image, output_image, download_file])
    return interface

@click.command()
@click.option("--debug", is_flag=True, help="Enable debug mode")
def main(debug: bool):
    remover = BackgroundRemover()
    interface = create_gradio_interface(remover)
    interface.launch(debug=debug, server_name="0.0.0.0", server_port=7860, allowed_paths=[str(Config.BASE_PATH)])

if __name__ == "__main__":
    main()
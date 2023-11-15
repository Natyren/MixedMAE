# from functools import partial

import torch
import torch.nn as nn

# from .utils import get_2d_sincos_pos_embed
from .modeling import PatchEmbed, Block
from .utils import mixing


class MixedMaskedAutoencoderViT(nn.Module):
    def __init__(
        self,
        img_size=224,
        patch_size=16,
        in_chans=3,
        embed_dim=1024,
        depth=24,
        num_heads=16,
        decoder_embed_dim=512,
        decoder_depth=8,
        decoder_num_heads=16,
        mlp_ratio=4.0,
        norm_layer=nn.LayerNorm,
        norm_pix_loss=False,
    ):
        super().__init__()

        self.patch_embed = PatchEmbed(img_size, patch_size, in_chans, embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(
            torch.zeros(1, num_patches + 1, embed_dim), requires_grad=False
        )
        self.segment_embed = nn.Embedding(
            4, embed_dim
        )  # TODO change 4 to custom mixing param

        self.blocks = nn.ModuleList(
            [
                Block(
                    embed_dim,
                    num_heads,
                    qkv_bias=True,
                    norm_layer=norm_layer,
                )
                for _ in range(depth)
            ]
        )
        self.norm = norm_layer(embed_dim)

        self.mask_token = nn.Parameter(torch.zeros(1, decoder_embed_dim))

        self.decoder_embed = nn.Linear(embed_dim, decoder_embed_dim, bias=True)

        self.decoder_pos_embed = nn.Parameter(
            torch.zeros(1, num_patches + 1, decoder_embed_dim),
            requires_grad=False,
        )

        self.decoder_blocks = nn.ModuleList(
            [
                Block(
                    decoder_embed_dim,
                    decoder_num_heads,
                    mlp_ratio,
                    qkv_bias=True,
                    norm_layer=norm_layer,
                )
                for _ in range(decoder_depth)
            ]
        )

        self.decoder_norm = norm_layer(decoder_embed_dim)
        self.decoder_pred = nn.Linear(
            decoder_embed_dim, patch_size**2 * in_chans, bias=True
        )

        self.norm_pix_loss = norm_pix_loss

    def shuffling(self, x, n_splits=4):
        mixed = [
            mixing(tnsr) for tnsr in torch.split(x, n_splits)
        ]  # TODO make decrease shape to [num_patches]
        x_tensors = torch.cat([tnsr[0] for tnsr in mixed])
        idxes = torch.cat([tnsr[1] for tnsr in mixed])
        return x_tensors, idxes

    def patchify(self, imgs):
        """
        imgs: (N, 3, H, W)
        x: (N, L, patch_size**2 *3)
        """
        p = self.patch_embed.patch_size[0]
        assert imgs.shape[2] == imgs.shape[3] and imgs.shape[2] % p == 0

        h = w = imgs.shape[2] // p
        x = imgs.reshape(shape=(imgs.shape[0], 3, h, p, w, p))
        x = torch.einsum("nchpwq->nhwpqc", x)
        x = x.reshape(shape=(imgs.shape[0], h * w, p**2 * 3))
        return x

    def forward_encoder(self, x):
        x = self.patch_embed(x)

        x = x + self.pos_embed[:, 1:, :]
        x = x + self.segment_embed(
            torch.tensor(
                [i % 4 for i in range(x.shape[0])]
            )  # TODO change 4 to custom mixing param
        ).unsqueeze(1)

        x, ids = self.shuffling(x)

        cls_token = self.cls_token + self.pos_embed[:, :1, :]
        cls_tokens = cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)

        return x, ids

    def forward_decoder(self, x, ids):
        x = self.decoder_embed(x)

        x_ = x[:, 1:, :]
        for i in range(x.shape[0]):
            x_[i] = torch.where(
                ids[i][:, : x.shape[-1]] != i % 4, self.mask_token, x_[i]
            )  # TODO make with custom group size

        x = torch.cat([x[:, :1, :], x_], dim=1)

        x = x + self.decoder_pos_embed

        for blk in self.decoder_blocks:
            x = blk(x)
        x = self.decoder_norm(x)

        x = self.decoder_pred(x)
        x = x[:, 1:, :]

        return x

    def forward_loss(self, imgs, pred):

        target = self.patchify(imgs)
        if self.norm_pix_loss:
            mean = target.mean(dim=-1, keepdim=True)
            var = target.var(dim=-1, keepdim=True)
            target = (target - mean) / (var + 1.0e-6) ** 0.5

        loss = (pred - target) ** 2
        loss = loss.mean(dim=-1)  # [N, L], mean loss per patch

        loss = loss.sum() * 0.25  # add custom scaling parameter
        return loss

    def forward(self, x):
        latent, ids = self.forward_encoder(x)
        pred = self.forward_decoder(latent, ids)
        loss = self.forward_loss(x, pred)
        return loss, pred

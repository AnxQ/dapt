from .geometry import rot6d_to_rotmat
from .st_gcn import STGCN
from pointnet2_ops.pointnet2_modules import PointnetSAModule
from pointcept.models.builder import MODELS
from typing import Tuple
from einops import rearrange

import torch
import torch.nn as nn
import torch.nn.functional as F

import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def to_sequence(x: dict):
    B = x['frame_group'].max().item() + 1
    x['feat'] = rearrange(x['feat'], '(b t n) d -> b t n d', b=B, n=512)
    return x

class PointNet2Encoder(nn.Module):
    def __init__(self):
        super().__init__()

        self.SA_modules = nn.ModuleList()
        self.SA_modules.append(
            PointnetSAModule(
                npoint=256,
                radius=0.2,
                nsample=32,
                mlp=[0, 64, 64, 128],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                npoint=128,
                radius=0.4,
                nsample=32,
                mlp=[128, 128, 128, 256],
                use_xyz=True,
            )
        )
        self.SA_modules.append(
            PointnetSAModule(
                mlp=[256, 256, 512, 1024], use_xyz=True
            )
        )

    def _break_up_pc(self, pc: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        xyz = pc[..., :3].contiguous()
        features = pc[..., 3:].transpose(
            1, 2).contiguous() if pc.size(-1) > 3 else None
        return xyz, features

    def forward(self, data):
        x = data['feat']
        B, T, N, _ = x.shape
        x = x.reshape(-1, N, 3)
        xyz, features = self._break_up_pc(x)
        for module in self.SA_modules:
            xyz, features = module(xyz, features)
        features = features.squeeze(-1).reshape(B, T, -1)
        return features


class RNN(nn.Module):
    def __init__(self, n_input, n_output, n_hidden, n_rnn_layer=2):
        super(RNN, self).__init__()
        self.rnn = nn.GRU(n_hidden, n_hidden, n_rnn_layer,
                          batch_first=True, bidirectional=True)
        self.linear1 = nn.Linear(n_input, n_hidden)

        self.linear2 = nn.Linear(n_hidden * 2, n_output)

        self.dropout = nn.Dropout()

    def forward(self, x):  # (B, T, D)
        x = self.rnn(F.relu(self.dropout(self.linear1(x)), inplace=True))[0]
        return self.linear2(x)


@MODELS.register_module()
class LiDARCap(nn.Module):
    def __init__(self, graph_cfg=None):
        super().__init__()
        self.encoder = PointNet2Encoder()
        self.pose_s1 = RNN(1024, 24 * 3, 1024)
        self.pose_s2 = STGCN(3 + 1024, 6, graph_cfg=graph_cfg)

    def forward(self, data):
        pred = {}
        data = to_sequence(data)
        x = self.encoder(data)  # (B, T, D)
        B, T, _ = x.shape
        full_joints = self.pose_s1(x)  # (B, T, 24 * 3)
        rot6ds = self.pose_s2(torch.cat((full_joints.reshape(
            B, T, 24, 3), x.unsqueeze(-2).repeat(1, 1, 24, 1)), dim=-1))
        rot6ds = rot6ds.reshape(-1, rot6ds.size(-1))  # (B * T, D)
        rotmats = rot6d_to_rotmat(rot6ds).reshape(-1, 3, 3)  # (B * T * 24, 3, 3)
        pred['pred_rotmats'] = rotmats.reshape(B * T, 24, 3, 3)
        pred['pred_keypoints_3d'] = full_joints.reshape(B * T, 24, 3)

        return pred

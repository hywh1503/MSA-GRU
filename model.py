# paper title: 
# create: Hao Wang, Fangyu Liu, Fangmin Sun*
# date: December 2024

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torchvision import transforms

class LayerNorm(nn.Module):
    def __init__(self, normalized_shape, eps=1e-6, data_format="channels_last"):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(normalized_shape))
        self.bias = nn.Parameter(torch.zeros(normalized_shape))
        self.eps = eps
        self.data_format = data_format
        if self.data_format not in ["channels_last", "channels_first"]:
            raise NotImplementedError
        self.normalized_shape = (normalized_shape,)

    def forward(self, x):
        if self.data_format == "channels_last":
            return F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        elif self.data_format == "channels_first":
            # Compute mean and variance for channels_first format
            u = x.mean(1, keepdim=True) # Mean across the normalized shape
            s = (x - u).pow(2).mean(1, keepdim=True) # Variance
            x = (x - u) / torch.sqrt(s + self.eps) # Normalize
            x = self.weight[:, None] * x + self.bias[:, None] # Scale and shift
            return x

#Gated Spatial Attention Unit (GSAU)
class GSAU(nn.Module):
    def __init__(self, n_feats):
        super().__init__()
        i_feats = n_feats * 2

        self.Conv1 = nn.Conv1d(n_feats, i_feats, 1, 1, 0)
        self.DWConv1 = nn.Conv1d(n_feats, n_feats, 7, 1, 7 // 2, groups=n_feats)
        self.Conv2 = nn.Conv1d(n_feats, n_feats, 1, 1, 0)

        self.norm = LayerNorm(n_feats, data_format='channels_first')
        self.scale = nn.Parameter(torch.zeros((1, n_feats, 1)), requires_grad=True)

    def forward(self, x):
        shortcut = x.clone()
        x = self.norm(x)
        x = self.Conv1(x)
        a, x = torch.chunk(x, 2, dim=1)
        x = x * self.DWConv1(a)
        x = self.Conv2(x)
        return x * self.scale + shortcut

# multi-scale large kernel attention (MLKA)
class MLKA(nn.Module):
    def __init__(self, n_feats):
        super().__init__()
        if n_feats % 3 != 0:
            raise ValueError("n_feats must be divisible by 3 for MLKA.")

        i_feats = 2 * n_feats

        self.norm = LayerNorm(n_feats, data_format='channels_first')
        self.scale = nn.Parameter(torch.zeros((1, n_feats, 1)), requires_grad=True)

        self.LKA7 = nn.Sequential(
            nn.Conv1d(n_feats // 3, n_feats // 3, 7, 1, 7 // 2, groups=n_feats // 3),
            nn.Conv1d(n_feats // 3, n_feats // 3, 9, stride=1, padding=(9 // 2) * 4, groups=n_feats // 3, dilation=4),
            nn.Conv1d(n_feats // 3, n_feats // 3, 1, 1, 0))
        self.LKA5 = nn.Sequential(
            nn.Conv1d(n_feats // 3, n_feats // 3, 5, 1, 5 // 2, groups=n_feats // 3),
            nn.Conv1d(n_feats // 3, n_feats // 3, 7, stride=1, padding=(7 // 2) * 3, groups=n_feats // 3, dilation=3),
            nn.Conv1d(n_feats // 3, n_feats // 3, 1, 1, 0))
        self.LKA3 = nn.Sequential(
            nn.Conv1d(n_feats // 3, n_feats // 3, 3, 1, 1, groups=n_feats // 3),
            nn.Conv1d(n_feats // 3, n_feats // 3, 5, stride=1, padding=(5 // 2) * 2, groups=n_feats // 3, dilation=2),
            nn.Conv1d(n_feats // 3, n_feats // 3, 1, 1, 0))

        self.X3 = nn.Conv1d(n_feats // 3, n_feats // 3, 3, 1, 1, groups=n_feats // 3)
        self.X5 = nn.Conv1d(n_feats // 3, n_feats // 3, 5, 1, 5 // 2, groups=n_feats // 3)
        self.X7 = nn.Conv1d(n_feats // 3, n_feats // 3, 7, 1, 7 // 2, groups=n_feats // 3)

        self.proj_first = nn.Sequential(
            nn.Conv1d(n_feats, i_feats, 1, 1, 0))

        self.proj_last = nn.Sequential(
            nn.Conv1d(n_feats, n_feats, 1, 1, 0))

    def forward(self, x):
        shortcut = x.clone()
        x = self.norm(x)
        x = self.proj_first(x)
        a, x = torch.chunk(x, 2, dim=1)
        a_1, a_2, a_3 = torch.chunk(a, 3, dim=1)
        a = torch.cat([self.LKA3(a_1) * self.X3(a_1), self.LKA5(a_2) * self.X5(a_2), self.LKA7(a_3) * self.X7(a_3)], dim=1)
        x = self.proj_last(x * a) * self.scale + shortcut
        return x

#multi-scale attention blocks (MAB)
class MAB(nn.Module):
    def __init__(self, n_feats):
        super().__init__()
        self.LKA = MLKA(n_feats)
        self.LFE = GSAU(n_feats)

    def forward(self, x):
        x = self.LKA(x)
        x = self.LFE(x)
        return x

# MCA
class MCA(nn.Module):
    def __init__(self, inplanes, planes, stride):
        super().__init__()
        self.inplanes = inplanes
        self.planes = planes
        self.stride = stride
        self.k3 = 3
        self.k5 = 5
        self.k7 = 7
        self.cnn3 = nn.Conv1d(self.inplanes, self.planes, self.k3, self.stride, self.k3 // 2)
        self.cnn5 = nn.Conv1d(self.inplanes, self.planes, self.k5, self.stride, self.k5 // 2)
        self.cnn7 = nn.Conv1d(self.inplanes, self.planes, self.k7, self.stride, self.k7 // 2)
        self.MAB3 = MAB(self.planes)
        self.MAB5 = MAB(self.planes)
        self.MAB7 = MAB(self.planes)

    def forward(self, a_1, a_2, a_3):
        a_1 = self.cnn3(a_1)
        a_2 = self.cnn5(a_2)
        a_3 = self.cnn7(a_3)
        a_1 = self.MAB3(a_1)
        a_2 = self.MAB5(a_2)
        a_3 = self.MAB7(a_3)
        return a_1, a_2, a_3

# 
class Our(nn.Module):
    def __init__(self, n_feats):
        super().__init__()
        i_feats = 3 * n_feats
        self.proj_first = nn.Sequential(nn.Conv1d(n_feats, i_feats, 1, 1, 0))
        self.MCA1 = MCA(inplanes=n_feats, planes=n_feats * 2, stride=2)
        self.MCA2 = MCA(inplanes=n_feats * 2, planes=n_feats * 4, stride=2)
        self.MCA3 = MCA(inplanes=n_feats * 4, planes=n_feats * 8, stride=2)
        self.biGRU = nn.GRU(input_size=38, hidden_size=19, num_layers=2, batch_first=True, bidirectional=True)
        self.flatten = nn.Flatten()
        self.fc = nn.Sequential(
            nn.Linear(n_feats * 8 * 3 * 38, 512),
            nn.Linear(512, 18)
        )

    def forward(self, sensor_accel, sensor_gyro):
        # (B, 6, 300)
        x = torch.cat((sensor_accel, sensor_gyro), dim=1)
        x = self.proj_first(x)
        # a (B, 6, 300)
        a_1, a_2, a_3 = torch.chunk(x, 3, dim=1)
        a_1, a_2, a_3 = self.MCA1(a_1, a_2, a_3)
        a_1, a_2, a_3 = self.MCA2(a_1, a_2, a_3)
        a_1, a_2, a_3 = self.MCA3(a_1, a_2, a_3)
        a = torch.cat([a_1, a_2, a_3], dim=1)
        x, _ = self.biGRU(a)
        x = self.flatten(x)
        x = self.fc(x)
        return x


if __name__ == '__main__':
    
    n_feats = 6  # Must be divisible by 3
    net = Our(n_feats)

    sensor_accel = torch.randn(1, 3, 300)
    sensor_gyro = torch.randn(1, 3, 300)

    y_pred = net(sensor_accel, sensor_gyro)
    # print(y_pred)
    print(y_pred.shape)

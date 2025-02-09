# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import unittest

import torch
import torch.nn.functional as F

from nni.compression.pytorch.pruning import L1NormPruner
from nni.compression.pytorch.speedup import ModelSpeedup
from nni.compression.pytorch.utils import (
    compute_sparsity_compact2origin,
    compute_sparsity_mask2compact
)

class CondModel(torch.nn.Module):
    """
    test for:
        prim::If
    """
    the_cond: bool
    def __init__(self):
        super().__init__()
        self.the_cond = True

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.the_cond:
            x = x + 0.00001
        else:
            x = x - 0.00001
        self.the_cond = not self.the_cond
        return x

class ASubModel(torch.nn.Module):
    """
    test for:
        sub model
    """
    def __init__(self):
        super().__init__()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + 0.00001
        return x

class TorchModel1(torch.nn.Module):
    """
    test for:
        add, sub, mul, div, exp, matmul,
        relu, gelu, tanh, silu, sigmod, softmax, leaky_relu,
        size, unsqueeze, flatten, cat, slice, reshape, transpose, t, select, permute, constant_pad_nd, split
        mean, avg_pool2d, max_pool2d, sum, adaptive_avg_pool2d,
        to, Int, view,
        type_as, expand_as, contiguous,

    notes:
        'floor_divide' have no backward, then not be tested
    """
    def __init__(self):
        super().__init__()
        self.conv1 = torch.nn.Conv2d(1, 6, 5, 1)
        self.conv2 = torch.nn.Conv2d(6, 16, 5, 1)
        self.fccond = torch.nn.Linear(16 * 4 * 4, 16 * 4 * 4)
        self.fc1 = torch.nn.Linear(16 * 4 * 4, 120)
        self.fc2 = torch.nn.Linear(120, 84)
        self.fc3 = torch.nn.Linear(84, 10)
        self.pool1 = torch.nn.MaxPool2d((2, 2))
        self.pool2 = torch.nn.MaxPool2d((2, 2))
        self.cond = torch.jit.script(CondModel())
        self.asub = ASubModel()

    def forward(self, x: torch.Tensor):
        x = torch.ops.aten.to(x, dtype=6, layout=0)
        y1 = torch.ones_like(x)
        y2 = torch.zeros_like(x)
        x = x - y1 + y2

        y1 = torch.rand([8, 1, 28, 28], device=x.device)
        y2 = torch.rand_like(x)
        x = x + y1 + y2

        y1 = torch.randn([8, 1, 28, 28], device=x.device)
        y2 = torch.randn_like(x)
        x = x + y1 + y2

        y1 = torch.randint(0, 100, [8, 1, 28, 28], device=x.device)
        y2 = torch.randint_like(x, 0, 100)
        y = y1 + y2
        y = y.to(torch.float32) / 100
        x = x + y

        x = x.contiguous(memory_format=torch.channels_last)
        x = torch._C._nn.upsample_bilinear2d(x, (28, 28), False)
        x = torch._C._nn.upsample_nearest2d(x, (28, 28))
        x = F.adaptive_avg_pool2d(x, (28, 28))

        x = torch.exp(x)
        x = torch.sigmoid(x)

        x = torch.transpose(x, 1, 2)
        x = torch.transpose(x, 1, 2)

        x = F.avg_pool2d(x, 3, 1, padding=1)
        x = F.max_pool2d(x, 3, 1, padding=1)

        x = x.to(torch.float32)

        x = self.conv1(x)
        y1 = self.pool1(F.relu(x))
        y2 = self.pool1(F.gelu(x))
        y3 = self.pool1(F.leaky_relu(x))

        x = y1 + y2 + y3

        x = x + 0.00001

        x = x * 1.00001

        x = self.conv2(x)
        y1 = self.pool2(F.silu(x))
        y2 = self.pool2(torch.tanh(x))

        x = y1 - y2

        x = x - 0.00001

        x = x / 1.00001

        x = torch.permute(x, (0, 2, 3, 1))
        x = torch.permute(x, (0, 2, 3, 1))
        x = torch.permute(x, (0, 2, 3, 1))
        x = torch.unsqueeze(x, dim=1)
        x = torch.select(x, dim=1, index=0)
        x = torch.unsqueeze(x, dim=1)
        x = torch.mean(x, dim=1)
        x = torch.unsqueeze(x, dim=1)
        x = torch.sum(x, dim=1, dtype=torch.float32)
        x = torch.unsqueeze(x, dim=1)
        x = torch.squeeze(x, dim=1)
        x = torch.flatten(x, 1)
        x = x.reshape(x.shape)
        x = x.view(-1, x.size(1))


        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = F.softmax(self.fc3(x), dim=1)

        y1 = x[:,0:int(x.size(1)/2)]
        y2 = x[:,int(x.size(1)/2):x.size(1)]
        x = torch.cat((y1, y2), dim=1)

        x = x.type_as(x)
        dim = x.dim()
        y = torch.sum(x, dim=dim-1, keepdim=True)
        z = y.expand_as(x)
        x = x / z
        x = torch.matmul(x, x.t())
        
        # TODO: should be available in #5107
        # x = torch.split(x, 1, dim=1)
        # x = torch.cat(x, dim=1)
        
        # x = self.cond(x) # condition is not support now
        # TODO: the asub execution is bad.
        # reason: the graph_utils assumes modules with no sub_module are leaf_modules.
        #         so the asub will be treated as a leaf_module.
        # x = self.asub(x)
        x = torch.constant_pad_nd(x, (1,1,1,1), 3.14159)

        return x

class AutoConvTestCase(unittest.TestCase):
    def l1norm_pruner(self, device):
        model = TorchModel1().to(device)
        dummy_input = torch.rand(8, 1, 28, 28).to(device)
        config_list = [{'op_types': ['Conv2d'], 'sparsity': 0.5}]
        pruner = L1NormPruner(model=model, config_list=config_list)
        pruned_model, masks = pruner.compress()
        pruner._unwrap_model()
        sparsity_list = compute_sparsity_mask2compact(pruned_model, masks, config_list)
        print('dummy_input.shape:', dummy_input.shape)
        ModelSpeedup(model, dummy_input, masks).speedup_model()
        real_sparsity_list = compute_sparsity_compact2origin(TorchModel1().to(device), model, config_list)

        print('sparsity_list:', sparsity_list)
        assert 0.45 < sparsity_list[0]['total_sparsity'] < 0.55

        print('real_sparsity_list:', real_sparsity_list)
        assert 0.45 < real_sparsity_list[0]['total_sparsity'] < 0.75

        print('the shape of output of the infer:', model(dummy_input).shape)
        assert model(dummy_input).shape == torch.Size((10, 10))

    def test_l1norm_pruner_cpu(self):
        return self.l1norm_pruner(torch.device('cpu'))

    def test_l1norm_pruner_cuda(self):
        return self.l1norm_pruner(torch.device('cuda'))

if __name__ == '__main__':
    unittest.main()

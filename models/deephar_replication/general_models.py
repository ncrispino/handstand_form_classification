"""
Holds models shared by different parts of the network.
"""
import torch
import torch.nn as nn

class ConvBlock(nn.Module):
    """
    Convolution with batch normalization and relu.
    """
    def __init__(self, input_dim, output_dim, kernel_size, stride=1, padding=0):
        super().__init__()
        self.conv = nn.Conv2d(input_dim, output_dim, kernel_size=kernel_size, stride=stride, padding=padding)
        self.batch_norm = nn.BatchNorm2d(output_dim)
        self.relu = nn.ReLU()

    def forward(self, x):
        return self.relu(self.batch_norm(self.conv(x)))

class SCBlock(nn.Module):
    """
    Depth-wise separable convolution.
    include_batch_relu is a boolean that represents if a batch norm and relu are used after the convolution.
    """
    def __init__(self, n_in, n_out, s, include_batch_relu=True):
        super().__init__()
        self.n_in = n_in
        self.n_out = n_out     
        self.include_batch_relu = include_batch_relu 
        # use n_in separate kernels of dim 1 x kernel_size x kernel_size, each for one channel, and concatenate together using pointwise conv
        self.depthwise_conv = nn.Conv2d(n_in, n_in, kernel_size=s, groups=n_in, padding='same') # as input and output both have H x W, as seen in Figure 10 of paper
        # use a 1x1 conv to increase output dim
        self.pointwise_conv = nn.Conv2d(n_in, n_out, kernel_size=1)
        self.batch_norm = nn.BatchNorm2d(n_out)
        self.relu = nn.ReLU()
    
    def forward(self, x):
        out = self.depthwise_conv(x)
        out = self.pointwise_conv(out)
        if self.include_batch_relu:
            out = self.relu(self.batch_norm(out))
        return out
        

class SRBlock(nn.Module):
    """
    Separable residual module.
    Uses depthwise separable convolutions, meaning it combines a depthwise convolution with a pointwise convolution.
    Not using Relu between depthwise and pointwise, as recommended in this paper: https://arxiv.org/abs/1610.02357.
    include_batch_relu is a boolean that represents if a batch norm and relu are used before the end of the block.
    """
    def __init__(self, n_in, n_out, s, include_batch_relu=True):
        super().__init__()
        self.n_in = n_in
        self.n_out = n_out     
        self.include_batch_relu = include_batch_relu   
        self.conv = nn.Conv2d(n_in, n_out, kernel_size=1)
        self.sc = SCBlock(n_in, n_out, s, include_batch_relu=True)
        self.batch_norm = nn.BatchNorm2d(n_out)
        self.relu = nn.ReLU()
    
    def forward(self, x):
        out1 = self.conv(x)
        if self.n_in == self.n_out:
            out = x + out1
            if self.include_batch_relu:
                out = self.batch_norm(out)
                out = self.relu(out1)
            return out
        out2 = self.sc(x)
        out = out1 + out2
        if self.include_batch_relu:
            out = self.batch_norm(out)
            out = self.relu(out)
        return out

def spacial_softmax(x):
    """
    Apply softmax to each H x W (spacial softmax)
    """
    H = x.shape[2]
    W = x.shape[3]
    softmax = nn.Softmax(2)
    x_collapsed = x.view(x.shape[0], x.shape[1], -1) # collapse height and width to apply softmax
    x_prob = softmax(x_collapsed).view(-1, x.shape[1], H, W)  
    return x_prob    

class SoftArgMax(nn.Module):
    """    
    Soft-argmax operation. Returns B x C x 2 if 2D, B x C x 1 if 1D.
    Should provide equivalent output to the softargmax model in the paper.
    apply_softmax -- whether to apply spacial softmax before calculating the rest
    """
    def forward(self, x, apply_softmax=True):
        super().__init__()
        dim1 = False
        if len(x.shape) == 3: # adds dimension to end if 1D
            dim1 = True
            x = x.unsqueeze(-1)            
        H = x.shape[2]
        W = x.shape[3]
        # softmax
        if apply_softmax:
            x_prob = spacial_softmax(x)
        else:
            x_prob = x
        # Create tensor with weights to multiply
        height_values = torch.arange(0, H).unsqueeze(1)
        width_values = torch.arange(0, W).unsqueeze(0)
        height_tensor = torch.tile(height_values, (1, W))/(H - 1) # each row has row idx/num rows
        width_tensor = torch.tile(width_values, (H, 1))/(W - 1) # each col has col idx/num cols

        # multiply prob maps times weight tensors and sum over H x W        
        height_out = (x_prob * height_tensor).sum((2, 3))
        width_out = (x_prob * width_tensor).sum((2, 3))
        out = torch.cat((width_out.unsqueeze(-1), height_out.unsqueeze(-1)), dim=-1) # returns (x, y), which corresponds to (W, H)        
        if dim1:            
            return out[:, :, 1].unsqueeze(-1) # return only height_out, as width was only dim 1
        return out

class MaxPlusMinPooling(nn.Module):
    """
    MaxPlusMin pooling implemented according to max_min_pooling in deephar/layers.py from the paper's code.
    """
    def __init__(self, kernel_size, stride=2, padding=0):
        super().__init__()
        self.kernel_size = kernel_size
        self.stride = stride
        self.maxpool = nn.MaxPool2d(kernel_size, stride, padding=padding)
        self.minpool = nn.MaxPool2d(kernel_size, stride, padding=padding)

    def forward(self, x):
        return self.maxpool(x) - self.minpool(-x)

class GlobalMaxPlusMinPooling(nn.Module):
    """
    GlobalMaxPlusMin pooling implemented according to max_min_pooling in deephar/layers.py from the paper's code. This is 2D pooling.
    Global max pooling takes the max value for each channel. For more, see https://peltarion.com/knowledge-center/documentation/modeling-view/build-an-ai-model/blocks/global-max-pooling-2d.
    Input: C x H x W
    Output: C
    """
    def __init__(self):
        super().__init__()

    def forward(self, x):
        max_pool = torch.amax(x, dim=(2, 3)) 
        min_pool = torch.amax(-x, dim=(2, 3))    
        return max_pool - min_pool

def kronecker_prod(a, b):
    """
    Multiplies a and b by channel. Returns B x C1 x C2 x H x W tensor
    a -- B * T x C1 x H x W tensor
    b -- B * T x C2 x H x W tensor
    """
    C1 = a.shape[-3]
    C2 = b.shape[-3]
    a = a.unsqueeze(-3)
    b = b.unsqueeze(-4)
    a = a.tile(1, 1, C2, 1, 1)
    b = b.tile(1, C1, 1, 1, 1)
    return a * b
import torch
import torch.nn as nn
import numpy as np
from util import batch_episym

class AttentiveContextNorm(nn.Module):
    def __init__(self, channels, local_or_global=True, head=1):
        nn.Module.__init__(self)
        self.att = nn.Conv2d(channels, head, kernel_size=1)
        self.local_or_global = local_or_global
    def forward(self, x):
        assert x.dim() == 4 and x.shape[3]==1
        w = self.att(x)
        if self.local_or_global:
            #print('w max '+str(w.max()))
            w = torch.sigmoid(w)
            w = w / w.sum(dim=2, keepdim=True).clamp(min=1e-10)
            #print('w max '+str(w.max())+' w min '+str(w.min()))
        else:
            w = torch.softmax(w, dim=2)
        w = w.mean(dim=1, keepdim=True)
        x_w = w*x
        mean_w = x_w.mean(dim=2, keepdim=True)
        #print('mean w min '+str(mean_w.min())+'mean w max '+str(mean_w.max()))
        # clamp before sqrt to avoid nan in backward
        var_w = (((x_w - mean_w)**2).mean(dim=2,keepdim=True)).clamp(min=1e-20).sqrt()
        #print('var w min'+str(var_w.min()))
        x = (x-mean_w) / var_w.clamp(min=1e-10)
        #print('x max '+str(x.max()))
        return x


class PointCN(nn.Module):
    def __init__(self, channels, out_channels=None, use_att=False, use_gn=False, local_or_global=True, head=1):
        nn.Module.__init__(self)
        if not out_channels:
           out_channels = channels
        self.shot_cut = None
        if out_channels != channels:
            self.shot_cut = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.conv = nn.Sequential(
                nn.InstanceNorm2d(channels, eps=1e-3) if not use_att else AttentiveContextNorm(channels, local_or_global, head),
                nn.BatchNorm2d(channels) if not use_gn else nn.GroupNorm(num_groups=32, num_channels=channels),
                nn.ReLU(),
                nn.Conv2d(channels, out_channels, kernel_size=1),
                nn.InstanceNorm2d(out_channels, eps=1e-3) if not use_att else AttentiveContextNorm(out_channels, local_or_global, head),
                nn.BatchNorm2d(out_channels) if not use_gn else nn.GroupNorm(num_groups=32, num_channels=out_channels),
                nn.ReLU(),
                nn.Conv2d(out_channels, out_channels, kernel_size=1)
                )
    def forward(self, x):
        out = self.conv(x)
        if self.shot_cut:
            out = out + self.shot_cut(x)
        else:
            out = out + x
        return out
class trans(nn.Module):
    def __init__(self, dim1, dim2):
        nn.Module.__init__(self)
        self.dim1 = dim1
        self.dim2 = dim2

    def forward(self, x):
        return x.transpose(self.dim1, self.dim2)

class OAFilter(nn.Module):
    def __init__(self, channels, points, out_channels=None):
        nn.Module.__init__(self)
        if not out_channels:
           out_channels = channels
        self.shot_cut = None
        if out_channels != channels:
            self.shot_cut = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.conv1 = nn.Sequential(
                nn.InstanceNorm2d(channels, eps=1e-3),
                nn.BatchNorm2d(channels),
                nn.ReLU(),
                nn.Conv2d(channels, out_channels, kernel_size=1),#b*c*n*1
                trans(1,2))
        # Spatial Correlation Layer
        self.conv2 = nn.Sequential(
                nn.BatchNorm2d(points),
                nn.ReLU(),
                nn.Conv2d(points, points, kernel_size=1)
                )
        self.conv3 = nn.Sequential(        
                trans(1,2),
                #nn.InstanceNorm2d(out_channels, eps=1e-3),
                #nn.BatchNorm2d(out_channels),
                #nn.ReLU(),
                #nn.Conv2d(out_channels, out_channels, kernel_size=1)
                )
    def forward(self, x):
        out = self.conv1(x)
        out = out + self.conv2(out)
        out = self.conv3(out)
        if self.shot_cut:
            out = out + self.shot_cut(x)
        else:
            out = out + x
        return out

# you can use this bottleneck block to prevent from overfiting when your dataset is small
class OAFilterBottleneck(nn.Module):
    def __init__(self, channels, points1, points2, out_channels=None):
        nn.Module.__init__(self)
        if not out_channels:
           out_channels = channels
        self.shot_cut = None
        if out_channels != channels:
            self.shot_cut = nn.Conv2d(channels, out_channels, kernel_size=1)
        self.conv1 = nn.Sequential(
                nn.InstanceNorm2d(channels, eps=1e-3),
                nn.BatchNorm2d(channels),
                nn.ReLU(),
                nn.Conv2d(channels, out_channels, kernel_size=1),#b*c*n*1
                trans(1,2))
        self.conv2 = nn.Sequential(
                nn.BatchNorm2d(points1),
                nn.ReLU(),
                nn.Conv2d(points1, points2, kernel_size=1),
                nn.BatchNorm2d(points2),
                nn.ReLU(),
                nn.Conv2d(points2, points1, kernel_size=1)
                )
        self.conv3 = nn.Sequential(        
                trans(1,2),
                nn.InstanceNorm2d(out_channels, eps=1e-3),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(),
                nn.Conv2d(out_channels, out_channels, kernel_size=1)
                )
    def forward(self, x):
        out = self.conv1(x)
        out = out + self.conv2(out)
        out = self.conv3(out)
        if self.shot_cut:
            out = out + self.shot_cut(x)
        else:
            out = out + x
        return out

class diff_pool(nn.Module):
    def __init__(self, in_channel, output_points, softmax_scale=False):
        nn.Module.__init__(self)
        self.output_points = output_points
        self.conv = nn.Sequential(
                nn.InstanceNorm2d(in_channel, eps=1e-3),
                nn.BatchNorm2d(in_channel),
                nn.ReLU(),
                nn.Conv2d(in_channel, output_points, kernel_size=1))
        if softmax_scale:
            temp = torch.nn.Parameter(torch.tensor(1.))
            self.register_parameter('temp', temp)
        else:
            self.temp = 1.
        
    def forward(self, x):
        embed = self.conv(x)# b*k*n*1
        S = torch.softmax(embed*self.temp, dim=2).squeeze(3)
        out = torch.matmul(x.squeeze(3), S.transpose(1,2)).unsqueeze(3)
        return out

class diff_unpool(nn.Module):
    def __init__(self, in_channel, output_points, softmax_scale=False):
        nn.Module.__init__(self)
        self.output_points = output_points
        self.conv = nn.Sequential(
                nn.InstanceNorm2d(in_channel, eps=1e-3),
                nn.BatchNorm2d(in_channel),
                nn.ReLU(),
                nn.Conv2d(in_channel, output_points, kernel_size=1))
        if softmax_scale:
            temp = torch.nn.Parameter(torch.tensor(1.))
            self.register_parameter('temp', temp)
        else:
            self.temp = 1.
    def forward(self, x_up, x_down):
        #x_up: b*c*n*1
        #x_down: b*c*k*1
        embed = self.conv(x_up)# b*k*n*1
        S = torch.softmax(embed*self.temp, dim=1).squeeze(3)# b*k*n
        out = torch.matmul(x_down.squeeze(3), S).unsqueeze(3)
        return out
'''
class diff_unpool2(nn.Module):
    def __init__(self):
        nn.Module.__init__(self)
    def forward(self, x_up, x_down):
        #x_up: b*c*n*1
        #x_down: b*c*k*1
        #embed = self.conv(x_up)# b*k*n*1
        #S = torch.softmax(embed, dim=1).squeeze(3)# b*k*n
        #out = torch.matmul(x_down.squeeze(3), S).unsqueeze(3)
        x_up = 
        S = torch.softmax(torch.matmul(x_up.squeeze(3).transpose(1,2), x_down.squeeze(3)), dim=1)
        out = torch.matmul(S.transpose())
        return out
'''


class OANBlock(nn.Module):
    def __init__(self, net_channels, input_channel, depth, clusters, bottleneck=-1):
        nn.Module.__init__(self)
        channels = net_channels
        self.layer_num = depth
        print('channels:'+str(channels)+', layer_num:'+str(self.layer_num))
        self.conv1 = nn.Conv2d(input_channel, channels, kernel_size=1)

        l2_nums = clusters

        self.l1_1 = []
        for _ in range(self.layer_num//2):
            self.l1_1.append(PointCN(channels))

        self.down1 = diff_pool(channels, l2_nums)

        self.l2 = []
        for _ in range(self.layer_num//2):
            if bottleneck == -1:
                self.l2.append(OAFilter(channels, l2_nums))
            else:
                self.l2.append(OAFilterBottleneck(channels, l2_nums, bottleneck))

        self.up1 = diff_unpool(channels, l2_nums)

        self.l1_2 = []
        self.l1_2.append(PointCN(2*channels, channels))
        for _ in range(self.layer_num//2-1):
            self.l1_2.append(PointCN(channels))

        self.l1_1 = nn.Sequential(*self.l1_1)
        self.l1_2 = nn.Sequential(*self.l1_2)
        self.l2 = nn.Sequential(*self.l2)

        self.output = nn.Conv2d(channels, 1, kernel_size=1)


    def forward(self, data, xs):
        #data: b*c*n*1
        batch_size, num_pts = data.shape[0], data.shape[2]
        x1_1 = self.conv1(data)
        x1_1 = self.l1_1(x1_1)
        x_down = self.down1(x1_1)
        x2 = self.l2(x_down)
        x_up = self.up1(x1_1, x2)
        out = self.l1_2( torch.cat([x1_1,x_up], dim=1))

        logits = torch.squeeze(torch.squeeze(self.output(out),3),1)
        e_hat = weighted_8points(xs, logits)

        x1, x2 = xs[:,0,:,:2], xs[:,0,:,2:4]
        e_hat_norm = e_hat
        residual = batch_episym(x1, x2, e_hat_norm).reshape(batch_size, 1, num_pts, 1)

        return logits, e_hat, residual
class position_encoding(nn.Module):
    def __init__(self, L=10):
        nn.Module.__init__(self)
        self.L = L
    def forward(self, x):
        #x: b*c*n*1
        assert x.dim()==4 and x.shape[3] == 1, str(x.shape)
        # freq_bands = torch.arange(self.L).to(x.device).reshape(1,-1,1,1)
        embed = []
        for i in range(self.L):
            for fun in [torch.sin, torch.cos]:
                embed.append(fun((2**i)*x*np.pi))
        embed = torch.cat(embed,dim=1)
        return embed


class OANHourglass(nn.Module):
    def __init__(self, channels, input_channel, depths, clusters, bottleneck=-1, use_att1=False, use_att2=False, use_gn=False, local_or_global=True, head=1, cat=True, softmax_scale=False):
        nn.Module.__init__(self)
        self.conv1 = nn.Conv2d(input_channel, channels, kernel_size=1)
        self.flats = []
        self.downs = []
        self.flats2 = []
        self.ups = []
        self.cat = cat
        for idx in range(len(depths)-1):
            print('layer num '+str(depths[idx]))
            cur_flat = []
            if idx == 0:
                cur_flat.append(PointCN(channels, use_att=use_att1, use_gn=use_gn, local_or_global=local_or_global, head=head))
            else:
                cur_flat.append(PointCN(2*channels if cat else channels,channels, use_att=use_att1, use_gn=use_gn, local_or_global=local_or_global, head=head))
            for _ in range(1, depths[idx]):
                cur_flat.append(PointCN(channels, use_att=use_att1, use_gn=use_gn, local_or_global=local_or_global, head=head))
            self.flats.append(nn.Sequential(*cur_flat))
            self.downs.append(diff_pool(channels, clusters, softmax_scale=softmax_scale))
            cur_flat2 = []
            for _ in range(max(1, depths[idx]//2)):
                cur_flat2.append(OAFilter(channels, clusters) if bottleneck == -1 else OAFilterBottleneck(channels, clusters, bottleneck))
            self.flats2.append(nn.Sequential(*cur_flat2))
            self.ups.append(diff_unpool(channels, clusters, softmax_scale=softmax_scale))
        self.endconv = [PointCN(2*channels if cat else channels,channels, use_att=use_att1, use_gn=use_gn, local_or_global=local_or_global, head=head)]
        for _ in range(1, depths[-1]):
            self.endconv.append(PointCN(channels, use_att=use_att1, use_gn=use_gn, local_or_global=local_or_global, head=head))
        print('layer num '+str(depths[-1]))
        self.endconv = nn.Sequential(*self.endconv)
        self.flats, self.downs, self.flat2, self.ups = nn.ModuleList(self.flats), nn.ModuleList(self.downs), nn.ModuleList(self.flats2), nn.ModuleList(self.ups)
        

        self.output = nn.Conv2d(channels, 1, kernel_size=1)


    def forward(self, data, xs):
        #data: b*c*n*1
        batch_size, num_pts = data.shape[0], data.shape[2]
        x = self.conv1(data)
        for idx in range(len(self.flats)):
            x1 = self.flats[idx](x)
            x_down = self.downs[idx](x1)
            x2 = self.flat2[idx](x_down)
            x_up = self.ups[idx](x1, x2)
            x = torch.cat([x1, x_up], dim=1) if self.cat else x1+x_up
        out = self.endconv(x)

        logits = torch.squeeze(torch.squeeze(self.output(out),3),1)
        e_hat = weighted_8points(xs, logits)

        x1, x2 = xs[:,0,:,:2], xs[:,0,:,2:4]
        e_hat_norm = e_hat
        residual = batch_episym(x1, x2, e_hat_norm).reshape(batch_size, 1, num_pts, 1)

        return logits, e_hat, residual


class OANet(nn.Module):
    def __init__(self, config):
        nn.Module.__init__(self)
        self.iter_num = config.iter_num
        #depth_each_stage = config.net_depth//(config.iter_num+1)
        #depth_each_stage = 6
        
        self.pos_enc = position_encoding(config.pos_enc) if config.pos_enc > 0 else None
        self.side_channel = (config.use_ratio==2) + (config.use_mutual==2)
        if config.bottleneck==-1: # hardcode for compatibility with previous models
            self.weights_init = OANBlock(config.net_channels, 4+self.side_channel, 6, config.clusters, config.bottleneck)
            self.weights_iter = [OANBlock(config.net_channels, 6+self.side_channel, 6, config.clusters, config.bottleneck) for _ in range(config.iter_num)]
        else:
            self.weights_init = OANHourglass(config.net_channels, 4+self.side_channel+8*config.pos_enc, config.net_depth, config.clusters, config.bottleneck, config.use_att1, config.use_att2, config.use_gn, config.lg, config.head, config.cat, config.softmax_scale)
            self.weights_iter = [OANHourglass(config.net_channels, 6+self.side_channel+8*config.pos_enc, config.net_depth, config.clusters, config.bottleneck, config.use_att1, config.use_att2, config.use_gn, config.lg, config.head, config.cat, config.softmax_scale) for _ in range(config.iter_num)]
        
        self.weights_iter = nn.ModuleList(self.weights_iter)
        

    def forward(self, data):
        assert data['xs'].dim() == 4 and data['xs'].shape[1] == 1
        batch_size, num_pts = data['xs'].shape[0], data['xs'].shape[2]
        #data: b*1*n*c
        input = [data['xs'].transpose(1,3)]
        if self.pos_enc is not None:
            input.append(self.pos_enc(input[0]))
        if self.side_channel > 0:
            sides = data['sides'].transpose(1,2).unsqueeze(3)
            input.append(sides)
        

        input = torch.cat(input, dim=1)
    
        res_logits, res_e_hat = [], []
        #import pdb;pdb.set_trace()
        logits, e_hat, residual = self.weights_init(input, data['xs'])
        res_logits.append(logits), res_e_hat.append(e_hat)
        for i in range(self.iter_num):
            logits, e_hat, residual = self.weights_iter[i](
                torch.cat([input, residual.detach(), torch.relu(torch.tanh(logits)).reshape(residual.shape).detach()], dim=1),
                data['xs'])
            res_logits.append(logits), res_e_hat.append(e_hat)
        return res_logits, res_e_hat  


        
def batch_symeig(X):
    # it is much faster to run symeig on CPU
    device = X.device
    X = X.cpu()
    b, d, _ = X.size()
    bv = X.new(b,d,d)
    for batch_idx in range(X.shape[0]):
        e,v = torch.symeig(X[batch_idx,:,:].squeeze(), True)
        #print(e)
        #print(v)
        bv[batch_idx,:,:] = v
    bv = bv.to(device)
    return bv


def weighted_8points(x_in, logits):
    # x_in: batch * 1 * N * 4
    x_shp = x_in.shape
    # Turn into weights for each sample
    weights = torch.relu(torch.tanh(logits))
    x_in = x_in.squeeze(1)
    
    # Make input data (num_img_pair x num_corr x 4)
    xx = torch.reshape(x_in, (x_shp[0], x_shp[2], 4)).permute(0, 2, 1)

    # Create the matrix to be used for the eight-point algorithm
    X = torch.stack([
        xx[:, 2] * xx[:, 0], xx[:, 2] * xx[:, 1], xx[:, 2],
        xx[:, 3] * xx[:, 0], xx[:, 3] * xx[:, 1], xx[:, 3],
        xx[:, 0], xx[:, 1], torch.ones_like(xx[:, 0])
    ], dim=1).permute(0, 2, 1)
    wX = torch.reshape(weights, (x_shp[0], x_shp[2], 1)) * X
    XwX = torch.matmul(X.permute(0, 2, 1), wX)
    

    # Recover essential matrix from self-adjoing eigen
    #print(XwX)
    v = batch_symeig(XwX)
    e_hat = torch.reshape(v[:, :, 0], (x_shp[0], 9))

    # Make unit norm just in case
    e_hat = e_hat / torch.norm(e_hat, dim=1, keepdim=True)
    return e_hat


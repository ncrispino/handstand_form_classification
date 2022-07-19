"""
Contains model for action recognition, which contains the pose-based recognition model and the appearance-based recognition model.
The same general architecture is used for both, as seen in Figure 13.
"""
from general_models import *

class ActionStart(nn.Module):
    """
    Takes in B x N_f x T x N_J, where N_f is the number of dimensions (the number of coordinates for each point, T is the number of frames (temporal), and N_J is the number of joints
    pose_rec -- true if doing pose recognition (each conv does half the features that appearance recognition does)
    """
    def __init__(self, pose_rec):
        super().__init__()
        self.pose_rec = pose_rec
        self.dim = 3 if pose_rec else 576
        if self.pose_rec: # half of what's shown in Figure 13
            self.conv_left1 = ConvBlock(self.dim, 6, (3, 1), padding='same')
            self.conv_middle1 = ConvBlock(self.dim, 12, 3, padding='same')
            self.conv_right1 = ConvBlock(self.dim, 18, (3, 5), padding='same')
            self.conv_left2 = ConvBlock(36, 56, 3, padding='same')
            self.conv_right2 = nn.Sequential(
                ConvBlock(36, 32, 1, padding='same'),
                ConvBlock(32, 56, 3, padding='same')
            )
        else:
            self.conv_left1 = ConvBlock(self.dim, 12, (3, 1), padding='same')
            self.conv_middle1 = ConvBlock(self.dim, 24, 3, padding='same')
            self.conv_right1 = ConvBlock(self.dim, 36, (3, 5), padding='same')
            self.conv_left2 = ConvBlock(72, 112, 3, padding='same')
            self.conv_right2 = nn.Sequential(
                ConvBlock(72, 64, 1, padding='same'),
                ConvBlock(64, 112, 3, padding='same')
            )
        self.maxplusmin = MaxPlusMinPooling(2, padding=0)
    
    def forward(self, x):
        print("x: " + str(x.shape))
        out_left = self.conv_left1(x)
        out_middle = self.conv_middle1(x)
        out_right = self.conv_right1(x)        
        out = torch.cat((out_left, out_middle, out_right), dim=1)
        out_left1 = self.conv_left2(out)
        out_right1 = self.conv_right2(out)
        out1 = torch.cat((out_left1, out_right1), dim=1)
        print("out1: " + str(out1.shape))
        out1 = self.maxplusmin(out1)
        print("out1post: " + str(out1.shape))
        return out1

class ActionBlock(nn.Module):
    """
    Action prediction block.
    Takes in input of B x N_f x T x N_J
    Outputs softmax from action heat maps and a tensor of shape B x 224 x T/2 x N_J/2.
    pose_rec -- true if doing pose recognition (each conv does half the features that appearance recognition does)
    N_a -- number of actions
    """
    def __init__(self, pose_rec, N_a):
        super().__init__()
        self.N_a = N_a
        if pose_rec:
            dim = 112
        else:
            dim = 224
        self.conv1 = ConvBlock(dim, dim//2, 1, padding='same')
        self.conv2 = ConvBlock(dim//2, dim, 3, padding='same')
        self.conv3 = ConvBlock(dim, dim, 3, padding='same')
        self.maxplusmin = MaxPlusMinPooling(2, padding=0) # padding should be 'same' in tf
        self.conv4 = ConvBlock(dim, N_a, 3, padding='same')
        self.upsample = nn.Upsample(scale_factor=2, mode='nearest')
        self.conv5 = ConvBlock(N_a, dim, 3, padding='same')
        self.global_maxplusmin = GlobalMaxPlusMinPooling() # input: N_a x H x W. output: N_a
        self.softmax = nn.Softmax()
    
    def forward(self, x):
        out = self.conv2(self.conv1(x))
        out = x + out
        out1 = self.conv3(out)
        print(out1.shape)
        out2 = self.maxplusmin(out1)
        print(out2.shape)
        heatmaps = self.conv4(out2)
        print(heatmaps.shape)
        out2 = self.upsample(heatmaps)
        print(out2.shape)
        out2 = self.conv5(out2)
        print(out2.shape)
        out2 = out2 + out1
        out = out + out2
        actions = self.global_maxplusmin(heatmaps)
        actions = self.softmax(actions)
        return actions, out

class ActionCombined(nn.Module):
    """
    Pose/appearance recognition using K action recognition blocks -- a 'stacked architecture with intermediate supervision'
    See Figure 5 for more.
    pose_rec -- True if doing pose recognition
    """    
    def __init__(self, pose_rec, N_a, K):
        super().__init__()
        self.N_a = N_a
        self.K = K
        self.action_start = ActionStart(pose_rec)
        self.action_blocks = [ActionBlock(pose_rec=pose_rec, N_a=self.N_a) for i in range(K)]        
    
    def forward(self, x):
        all_actions = torch.zeros((self.K, self.N_a)) # holds a scalar for each action from each prediction block
        # keep track of output of previous block and add to input of next block
        out = self.action_start(x)
        prev_out = 0   
        for k, block in enumerate(self.action_blocks):      
            actions, new_out = block(out + prev_out)
            prev_out = out
            out = new_out           
            all_actions[k] = actions            
        return all_actions, out   

def appearance_extract(entry_input, prob_maps):
    """
    Extracts localized appearance features to be fed into action blocks.
    entry_input -- 576 x H x W output from global entry flow (multitask stem based on Inception-V4)
    prob_maps -- N_J x H x W probability maps obtained at the end of pose estimation part (softmax applied to heatmaps)
    """
    out = kronecker_prod(entry_input, prob_maps) # N_f x T x N_J x H x W
    print("kronout: " + str(out.shape))
    out = torch.sum(out, dim=(-2, -1)) # N_f x T x N_J
    print("kronout: " + str(out.shape))
    return out

class ActionRecognition(nn.Module):
    """
    Combines pose-based recognition and appearance-based recognition using a fully-connected layer with Softmax activation.
    Uses only the action predicted in the last block.
    """
    def __init__(self, N_a, K=4):
        super().__init__()
        self.N_a = N_a
        self.K = K        
        self.pose_rec = ActionCombined(True, N_a, K)
        self.action_rec = ActionCombined(False, N_a, K)
        self.fc = nn.Linear(2 * N_a, N_a)
        self.softmax = nn.Softmax()
    
    def forward(self, pose_input, entry_input, prob_maps):
        appearance_input = appearance_extract(entry_input, prob_maps)
        pose_actions, pose_out = self.pose_rec(pose_input)
        appearance_actions, appearance_out = self.action_rec(appearance_input)
        fc_input = torch.cat((pose_actions[-1], appearance_actions[-1]), dim=0) # isolate actions in last block
        out = self.softmax(self.fc(fc_input))
        return out
        

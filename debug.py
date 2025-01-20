import torch
pretrained = "./pretrained_models/dino_deitsmall16_pretrain.pth"
checkpoint = torch.load(pretrained, map_location="cpu")
print(checkpoint.keys())
